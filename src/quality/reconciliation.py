"""
Reconciliation: prove the warehouse agrees with the publisher, within tolerance.

What "reconciliation" actually means here
-----------------------------------------
Not "the numbers look plausible". Every check compares a figure the WAREHOUSE
computes by aggregating leaf rows against a figure the PUBLISHER printed
independently, and fails if they disagree by more than the tolerance.

That is only meaningful when the control total is genuinely independent. Two
kinds of check qualify:

  * CROSS-FOOT   The publisher printed both the parts and the total.
                 Example: CBA prints per-region arrears AND a CANADA row.
                 Summing our region rows must reproduce their CANADA row.

  * ROLL-UP      The publisher printed an aggregate row beside the leaves in the
                 same table (StatCan does this constantly - 'Canada' sits next
                 to the provinces). Our leaf sum must reproduce it.

A check that recomputes a number from the rows it came from proves nothing; none
of these do that.

Two tolerance types, and why that matters
-----------------------------------------
The first version of this module applied a 1% RELATIVE tolerance to every check,
including the arrears rate. That produced 12 false alarms: Ontario's derived rate
of 0.0645% against a published 0.06% is a 7.5% relative gap but only 0.0045
PERCENTAGE POINTS - pure display rounding by the publisher.

Relative tolerance is meaningless for a measure that is itself a small
percentage, because the denominator approaches zero. So:

  * VOLUME measures  (unit counts, dollars) -> RELATIVE tolerance, 1%
  * RATIO measures   (rates, indexes)       -> ABSOLUTE tolerance, percentage points

Getting this wrong in either direction is bad: too loose and the gate never
fires, too tight and it fires every month until people stop reading it.

Known publisher anomalies
-------------------------
Some breaches are real, and they are the publisher's. Those are listed
explicitly in KNOWN_ANOMALIES with a reason, rather than being hidden by
loosening a threshold. They still appear in the report; they just do not fail
the pipeline. Anything NOT on that list failing is a genuine regression.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable

import duckdb
import pandas as pd

from src.common.logging_setup import get_logger
from src.common.paths import config, duckdb_path

log = get_logger(__name__)

RELATIVE = "relative_pct"
ABSOLUTE = "absolute_pp"


@dataclass(frozen=True)
class Check:
    name: str
    fn: Callable[[duckdb.DuckDBPyConnection], pd.DataFrame]
    tolerance_type: str
    tolerance: float
    rationale: str


# Breaches confirmed to originate with the publisher, not this pipeline.
# Each entry needs evidence - see docs/reconciliation.md for the workings.
KNOWN_ANOMALIES: dict[tuple[str, str], str] = {
    ("arrears_cross_foot_total_mortgages", "1999-04"):
        "CBA's own CANADA row spikes to 2,870,113 then falls back to 2,824,255 the "
        "next month while the 8 regional rows move smoothly (2,794,209 -> 2,804,713 "
        "-> 2,824,255). The national row is the outlier, not our sum of the parts.",
    ("housing_starts_provinces_sum_to_canada", "1995-03 / Centres 50,000 and over"):
        "CMHC's published Canada total (4,563) exceeds the sum of its own 10 "
        "provincial rows (4,516) by 47 units. 1 breach in 1,314 comparisons; no "
        "province row is suppressed, so the gap is publisher-side.",
    ("arrears_rate_matches_published", "2013-03 / Quebec"):
        "CBA printed 0.30% where its own counts (2,731 / 824,269) give 0.3313%. "
        "Transcription error in the source PDF.",
    ("arrears_rate_matches_published", "1999-07 / Atlantic"):
        "Same 1999 restatement episode as the cross-foot anomaly above. Atlantic "
        "total_mortgages spikes to 233,386 (221,181 the month before, 224,470 the "
        "month after); the printed 0.50% is consistent with the pre-restatement "
        "denominator. Every adjacent month rounds correctly (0.5236->0.52, "
        "0.5168->0.52, 0.5083->0.51), so the parser is fine.",
}


# --------------------------------------------------------------------------- #
# Individual checks
# --------------------------------------------------------------------------- #

def check_arrears_cross_foot(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """
    CBA prints 8 regional rows and a CANADA row; they must cross-foot.

    The strongest check in the suite, because it validates the PDF parser end to
    end. A misaligned column or a dropped row breaks the sum immediately.
    """
    return con.sql("""
        WITH parts AS (
            SELECT f.date_key, SUM(f.total_mortgages) AS wh_total
            FROM dw.fact_mortgage_arrears f
            JOIN dw.dim_arrears_region ar ON ar.arrears_region_key = f.arrears_region_key
            WHERE NOT ar.is_national
            GROUP BY 1
        ),
        control AS (
            SELECT f.date_key, f.total_mortgages AS ctl_total
            FROM dw.fact_mortgage_arrears f
            JOIN dw.dim_arrears_region ar ON ar.arrears_region_key = f.arrears_region_key
            WHERE ar.is_national
        )
        SELECT 'arrears_cross_foot_total_mortgages' AS check_name,
               d.year_month                         AS check_grain,
               p.wh_total                           AS warehouse_value,
               c.ctl_total                          AS control_value
        FROM parts p
        JOIN control c     ON c.date_key = p.date_key
        JOIN dw.dim_date d ON d.date_key = p.date_key
        WHERE c.ctl_total IS NOT NULL AND c.ctl_total <> 0
    """).df()


def check_arrears_rate_derivation(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """
    Our recomputed arrears rate must match the rate CBA printed.

    Validates that the two count columns landed in the right positions: a column
    swap in the PDF parser would still cross-foot but would wreck this check.
    Measured in percentage points, since the values are themselves percentages
    close to zero.
    """
    return con.sql("""
        SELECT 'arrears_rate_matches_published' AS check_name,
               d.year_month || ' / ' || ar.region_name AS check_grain,
               100.0 * f.mortgages_in_arrears / NULLIF(f.total_mortgages, 0)
                                                   AS warehouse_value,
               f.arrears_rate_pct                  AS control_value
        FROM dw.fact_mortgage_arrears f
        JOIN dw.dim_date d            ON d.date_key           = f.date_key
        JOIN dw.dim_arrears_region ar ON ar.arrears_region_key = f.arrears_region_key
        WHERE f.arrears_rate_pct IS NOT NULL
          AND f.mortgages_in_arrears IS NOT NULL
    """).df()


def check_housing_starts_roll_up(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """
    Provinces must sum to the publisher's own 'Canada' row, within each coverage.

    This is the check that fails loudly if dim_geography.is_aggregate is wrong.
    Were 'Atlantic provinces' treated as a leaf, the provincial sum would
    overshoot Canada by roughly the entire Atlantic total - a ~5% error, not a
    subtle one.
    """
    return con.sql("""
        WITH leaves AS (
            SELECT f.date_key, f.coverage_key, SUM(f.units) AS wh_units
            FROM dw.fact_housing_activity f
            JOIN dw.dim_geography          g  ON g.geography_key      = f.geography_key
            JOIN dw.dim_construction_stage s  ON s.stage_key          = f.stage_key
            JOIN dw.dim_dwelling_type      dt ON dt.dwelling_type_key = f.dwelling_type_key
            WHERE g.geo_level    = 'Province'
              AND g.is_aggregate = FALSE
              AND s.stage_name   = 'Housing starts'
              AND dt.dwelling_type_name = 'Total units'
            GROUP BY 1, 2
        ),
        control AS (
            SELECT f.date_key, f.coverage_key, SUM(f.units) AS ctl_units
            FROM dw.fact_housing_activity f
            JOIN dw.dim_geography          g  ON g.geography_key      = f.geography_key
            JOIN dw.dim_construction_stage s  ON s.stage_key          = f.stage_key
            JOIN dw.dim_dwelling_type      dt ON dt.dwelling_type_key = f.dwelling_type_key
            WHERE g.geo_name   = 'Canada'
              AND s.stage_name = 'Housing starts'
              AND dt.dwelling_type_name = 'Total units'
            GROUP BY 1, 2
        )
        SELECT 'housing_starts_provinces_sum_to_canada'  AS check_name,
               d.year_month || ' / ' || cv.coverage_name AS check_grain,
               l.wh_units  AS warehouse_value,
               c.ctl_units AS control_value
        FROM leaves l
        JOIN control c          ON c.date_key = l.date_key AND c.coverage_key = l.coverage_key
        JOIN dw.dim_date d      ON d.date_key = l.date_key
        JOIN dw.dim_coverage cv ON cv.coverage_key = l.coverage_key
        WHERE c.ctl_units IS NOT NULL AND c.ctl_units <> 0
    """).df()


def check_dwelling_types_sum_to_total(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """
    Single + semi + row + apartment must equal the publisher's 'Total units'.

    Catches a whole class of dimension bugs at once: a mis-mapped dwelling label,
    or a roll-up member ('Multiples') wrongly treated as a component.
    """
    return con.sql("""
        WITH components AS (
            SELECT f.date_key, f.geography_key, f.coverage_key, f.stage_key,
                   SUM(f.units) AS wh_units
            FROM dw.fact_housing_activity f
            JOIN dw.dim_dwelling_type dt ON dt.dwelling_type_key = f.dwelling_type_key
            WHERE dt.dwelling_category IN
                  ('Single-detached','Semi-detached','Row','Apartment & other')
            GROUP BY 1, 2, 3, 4
        ),
        control AS (
            SELECT f.date_key, f.geography_key, f.coverage_key, f.stage_key,
                   SUM(f.units) AS ctl_units
            FROM dw.fact_housing_activity f
            JOIN dw.dim_dwelling_type dt ON dt.dwelling_type_key = f.dwelling_type_key
            WHERE dt.is_total
            GROUP BY 1, 2, 3, 4
        )
        SELECT 'dwelling_components_sum_to_total' AS check_name,
               d.year_month || ' / ' || g.geo_name || ' / ' || s.stage_short AS check_grain,
               k.wh_units  AS warehouse_value,
               c.ctl_units AS control_value
        FROM components k
        JOIN control c ON  c.date_key      = k.date_key
                       AND c.geography_key = k.geography_key
                       AND c.coverage_key  = k.coverage_key
                       AND c.stage_key     = k.stage_key
        JOIN dw.dim_date d                ON d.date_key      = k.date_key
        JOIN dw.dim_geography g           ON g.geography_key = k.geography_key
        JOIN dw.dim_construction_stage s  ON s.stage_key     = k.stage_key
        WHERE c.ctl_units IS NOT NULL AND c.ctl_units <> 0
          AND d.year >= 2015          -- component detail is sparse in early years
    """).df()


def check_unknown_key_rate(con: duckdb.DuckDBPyConnection) -> pd.DataFrame:
    """
    Conservation check: fact rows must resolve to real dimension members.

    The loaders LEFT JOIN and COALESCE to -1 rather than INNER JOIN, so a broken
    dimension mapping shows up here as unknown keys instead of silently vanishing
    rows. Control value is the total row count; warehouse value is the resolved
    count. Tolerance is 0.1%.
    """
    return con.sql("""
        SELECT 'facts_resolved_to_known_dimensions' AS check_name,
               'fact_housing_activity'              AS check_grain,
               CAST(SUM(CASE WHEN geography_key <> -1
                              AND dwelling_type_key <> -1
                              AND stage_key <> -1
                              AND coverage_key <> -1
                             THEN 1 ELSE 0 END) AS DOUBLE) AS warehouse_value,
               CAST(count(*) AS DOUBLE)                    AS control_value
        FROM dw.fact_housing_activity
        UNION ALL
        SELECT 'facts_resolved_to_known_dimensions',
               'fact_household_credit',
               CAST(SUM(CASE WHEN credit_product_key <> -1 THEN 1 ELSE 0 END) AS DOUBLE),
               CAST(count(*) AS DOUBLE)
        FROM dw.fact_household_credit
        UNION ALL
        SELECT 'facts_resolved_to_known_dimensions',
               'fact_mortgage_originations',
               CAST(SUM(CASE WHEN credit_product_key <> -1 THEN 1 ELSE 0 END) AS DOUBLE),
               CAST(count(*) AS DOUBLE)
        FROM dw.fact_mortgage_originations
        UNION ALL
        SELECT 'facts_resolved_to_known_dimensions',
               'fact_price_index',
               CAST(SUM(CASE WHEN geography_key <> -1 AND price_component_key <> -1
                             THEN 1 ELSE 0 END) AS DOUBLE),
               CAST(count(*) AS DOUBLE)
        FROM dw.fact_price_index
    """).df()


CHECKS: list[Check] = [
    Check(
        "arrears_cross_foot_total_mortgages", check_arrears_cross_foot,
        RELATIVE, 1.0,
        "Volume measure (mortgage counts): relative tolerance is appropriate.",
    ),
    Check(
        "arrears_rate_matches_published", check_arrears_rate_derivation,
        ABSOLUTE, 0.02,
        "Ratio measure. CBA rounds to 2dp, so up to 0.005pp is pure display "
        "rounding; 0.02pp allows for their counts being restated independently "
        "of the printed rate.",
    ),
    Check(
        "housing_starts_provinces_sum_to_canada", check_housing_starts_roll_up,
        RELATIVE, 1.0,
        "Volume measure (dwelling units).",
    ),
    Check(
        "dwelling_components_sum_to_total", check_dwelling_types_sum_to_total,
        RELATIVE, 1.0,
        "Volume measure (dwelling units).",
    ),
    Check(
        "facts_resolved_to_known_dimensions", check_unknown_key_rate,
        RELATIVE, 0.1,
        "Row conservation. Tighter than the reporting gate because unknown keys "
        "indicate a broken mapping, not publisher noise.",
    ),
]


# --------------------------------------------------------------------------- #
# Runner
# --------------------------------------------------------------------------- #
def run(run_id: int | None = None, persist: bool = True) -> pd.DataFrame:
    con = duckdb.connect(str(duckdb_path()))

    if run_id is None:
        row = con.sql("SELECT COALESCE(max(run_id), 0) FROM ops.etl_run").fetchone()
        run_id = row[0] if row else 0

    frames = []
    for check in CHECKS:
        df = check.fn(con)
        if df.empty:
            log.warning("%s produced no comparisons - it is not exercising anything",
                        check.name)
            continue
        df["tolerance_type"] = check.tolerance_type
        df["threshold_pct"] = check.tolerance
        frames.append(df)

    results = pd.concat(frames, ignore_index=True)
    results["warehouse_value"] = pd.to_numeric(results.warehouse_value, errors="coerce")
    results["control_value"] = pd.to_numeric(results.control_value, errors="coerce")
    results["variance_abs"] = (results.warehouse_value - results.control_value).abs()

    # Relative checks are scored in percent of control; absolute checks are
    # scored in the measure's own units (percentage points).
    rel = results.tolerance_type == RELATIVE
    results["variance_pct"] = pd.NA
    results.loc[rel, "variance_pct"] = (
        100.0 * results.loc[rel, "variance_abs"]
        / results.loc[rel, "control_value"].abs().replace(0, pd.NA)
    )
    results.loc[~rel, "variance_pct"] = results.loc[~rel, "variance_abs"]
    results["variance_pct"] = pd.to_numeric(results.variance_pct, errors="coerce")

    results["within_tolerance"] = (
        results.variance_pct.fillna(0) <= results.threshold_pct
    )
    results["is_known_anomaly"] = [
        (n, g) in KNOWN_ANOMALIES for n, g in zip(results.check_name, results.check_grain)
    ]
    # A documented publisher anomaly does not fail the pipeline, but it is still
    # reported so it can never be forgotten about.
    results["passed"] = results.within_tolerance | results.is_known_anomaly
    results["run_id"] = run_id
    results["checked_utc"] = datetime.now(timezone.utc)

    if persist:
        # Re-running reconciliation for one run_id replaces only that run's rows.
        con.execute("DELETE FROM ops.reconciliation_result WHERE run_id = ?", [run_id])

        # result_id must be unique across ALL runs, not restart at 1 each time -
        # ops.reconciliation_result accumulates history so the dashboard can
        # trend data quality over months. Seeding from max() after the DELETE is
        # what keeps that history intact.
        next_id = con.sql(
            "SELECT COALESCE(max(result_id), 0) + 1 FROM ops.reconciliation_result"
        ).fetchone()[0]

        out = results.copy()
        out.insert(0, "result_id", range(next_id, next_id + len(out)))
        cols = ["result_id", "run_id", "check_name", "check_grain", "warehouse_value",
                "control_value", "variance_abs", "variance_pct", "threshold_pct",
                "passed", "checked_utc"]
        con.register("_recon", out[cols])
        con.execute("INSERT INTO ops.reconciliation_result SELECT * FROM _recon")
        con.unregister("_recon")

    _report(results)
    con.close()
    return results


def _report(results: pd.DataFrame) -> None:
    by_name = {c.name: c for c in CHECKS}
    log.info("=" * 84)
    log.info("RECONCILIATION")
    log.info("=" * 84)

    for name, grp in results.groupby("check_name"):
        spec = by_name[name]
        unit = "%" if spec.tolerance_type == RELATIVE else "pp"
        real_breaches = grp[~grp.within_tolerance & ~grp.is_known_anomaly]
        known = grp[~grp.within_tolerance & grp.is_known_anomaly]
        worst = grp.variance_pct.max()
        status = "PASS" if real_breaches.empty else "FAIL"

        log.info("  [%s] %-40s %6d checks | tol %.3g%s | max %.4f%s | %d known | %d NEW",
                 status, name, len(grp), spec.tolerance, unit,
                 0.0 if pd.isna(worst) else worst, unit,
                 len(known), len(real_breaches))

        for _, row in known.iterrows():
            log.info("        known publisher anomaly: %s (%.4f%s)",
                     row.check_grain, row.variance_pct, unit)
        for _, row in real_breaches.head(5).iterrows():
            log.info("        NEW BREACH: %s  warehouse=%s control=%s (%.4f%s)",
                     row.check_grain, row.warehouse_value, row.control_value,
                     row.variance_pct, unit)

    total = len(results)
    new_breaches = int((~results.within_tolerance & ~results.is_known_anomaly).sum())
    known_n = int((~results.within_tolerance & results.is_known_anomaly).sum())
    clean = total - new_breaches - known_n

    log.info("-" * 84)
    log.info("TOTAL: %d comparisons | %d within tolerance (%.4f%%) | %d known publisher "
             "anomalies | %d NEW breaches",
             total, clean, 100.0 * clean / total, known_n, new_breaches)


if __name__ == "__main__":
    import sys

    res = run()
    new = int((~res.within_tolerance & ~res.is_known_anomaly).sum())
    if new and bool(config()["quality"]["fail_pipeline_on_breach"]):
        log.error("%d NEW reconciliation breaches - failing the pipeline", new)
        sys.exit(1)
