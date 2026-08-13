"""
STEP 3: Check our numbers against the published ones.

Run:  python check.py

This is the part that makes the project trustworthy, so it's worth explaining
what it does and doesn't do.

It is NOT "do the numbers look about right". Every check compares a number WE
calculated by adding up detail rows against a number the PUBLISHER printed
separately. If we get a different answer, one of us is wrong.

Two kinds of comparison work for this:

  Cross-check    The publisher printed both the parts and the total.
                 The CBA prints arrears for 8 regions AND a Canada row, so
                 adding up our 8 regions has to give their Canada number.

  Roll-up        The publisher put a total in the same table as the pieces.
                 Statistics Canada does this constantly - "Canada" sits right
                 next to the provinces. Our provinces added up must match it.

A check that recalculates a number from the same rows it came from proves
nothing, so neither of those is that.
"""

from datetime import datetime

import duckdb
import pandas as pd

from config import MAX_PERCENT_DIFFERENCE, MAX_RATE_DIFFERENCE, WAREHOUSE_FILE


# =============================================================================
# Known problems in the source data
# =============================================================================
# Four of our comparisons fail, and after digging into each one, all four turn
# out to be mistakes by the publisher rather than by us. They're listed here so
# they show up in the report every time but don't fail the whole run.
#
# Anything failing that ISN'T on this list means we broke something.
#
# The temptation is to just widen the allowed difference until nothing fails.
# Don't - then the check never catches anything real either.

KNOWN_SOURCE_PROBLEMS = {
    ("arrears add up to Canada", "1999-04"):
        "The CBA's own Canada row jumps to 2,870,113 this month and drops back "
        "to 2,824,255 the next, while the 8 regions move smoothly. Their "
        "national row is the odd one out, not our addition.",

    ("arrears rate matches published", "1999-07 / Atlantic"):
        "Same 1999 glitch. Atlantic's total jumps to 233,386 (from 221,181, "
        "back to 224,470), and the printed 0.50% matches the OLD total. Every "
        "month either side rounds correctly.",

    ("arrears rate matches published", "2013-03 / Quebec"):
        "CBA printed 0.30% but their own numbers (2,731 out of 824,269) work "
        "out to 0.3313%. Looks like a typo in the PDF.",
}


# =============================================================================
# The checks
# =============================================================================

def check_arrears_add_up(db):
    """The 8 CBA regions must add up to their published Canada row.

    This is the strongest check, because it tests the whole PDF reader. If a
    column got misread or a row was skipped, the total wouldn't match.
    """
    return db.sql("""
        WITH regions AS (
            SELECT f.date_key, SUM(f.total_mortgages) AS our_total
            FROM fact_mortgage_arrears f
            JOIN dim_arrears_region r ON r.arrears_region_key = f.arrears_region_key
            WHERE r.is_national = FALSE
            GROUP BY 1
        ),
        canada AS (
            SELECT f.date_key, f.total_mortgages AS published_total
            FROM fact_mortgage_arrears f
            JOIN dim_arrears_region r ON r.arrears_region_key = f.arrears_region_key
            WHERE r.is_national = TRUE
        )
        SELECT 'arrears add up to Canada' AS check_name,
               d.year_month               AS detail,
               regions.our_total          AS our_value,
               canada.published_total     AS published_value
        FROM regions
        JOIN canada   ON canada.date_key = regions.date_key
        JOIN dim_date d ON d.date_key = regions.date_key
        WHERE canada.published_total > 0
    """).df()


def check_arrears_rate(db):
    """Our calculated arrears rate must match the one CBA printed.

    This catches a different kind of mistake: if the PDF reader put the two
    number columns the wrong way round, the totals above would still add up
    fine, but this would go badly wrong.
    """
    return db.sql("""
        SELECT 'arrears rate matches published'      AS check_name,
               d.year_month || ' / ' || r.region_name AS detail,
               100.0 * f.mortgages_in_arrears / NULLIF(f.total_mortgages, 0) AS our_value,
               f.arrears_rate                         AS published_value
        FROM fact_mortgage_arrears f
        JOIN dim_date           d ON d.date_key = f.date_key
        JOIN dim_arrears_region r ON r.arrears_region_key = f.arrears_region_key
        WHERE f.arrears_rate IS NOT NULL
          AND f.mortgages_in_arrears IS NOT NULL
    """).df()


def check_provinces_add_up(db):
    """The provinces must add up to the Canada row in the same table.

    If geography.py wrongly treated something like "Atlantic provinces" as a
    real province, this would fail immediately - the provinces would add up to
    far more than Canada.
    """
    return db.sql("""
        WITH provinces AS (
            SELECT f.date_key, f.coverage_key, SUM(f.units) AS our_total
            FROM fact_housing_activity f
            JOIN dim_geography          g ON g.geography_key = f.geography_key
            JOIN dim_construction_stage s ON s.stage_key = f.stage_key
            JOIN dim_dwelling_type      t ON t.dwelling_type_key = f.dwelling_type_key
            WHERE g.geo_level = 'Province'
              AND g.is_aggregate = FALSE
              AND s.stage_short = 'Starts'
              AND t.is_total = TRUE
            GROUP BY 1, 2
        ),
        canada AS (
            SELECT f.date_key, f.coverage_key, SUM(f.units) AS published_total
            FROM fact_housing_activity f
            JOIN dim_geography          g ON g.geography_key = f.geography_key
            JOIN dim_construction_stage s ON s.stage_key = f.stage_key
            JOIN dim_dwelling_type      t ON t.dwelling_type_key = f.dwelling_type_key
            WHERE g.geo_name = 'Canada'
              AND s.stage_short = 'Starts'
              AND t.is_total = TRUE
            GROUP BY 1, 2
        )
        SELECT 'provinces add up to Canada'             AS check_name,
               d.year_month || ' / ' || c.coverage_name AS detail,
               provinces.our_total                      AS our_value,
               canada.published_total                   AS published_value
        FROM provinces
        JOIN canada      ON canada.date_key = provinces.date_key
                        AND canada.coverage_key = provinces.coverage_key
        JOIN dim_date     d ON d.date_key = provinces.date_key
        JOIN dim_coverage c ON c.coverage_key = provinces.coverage_key
        WHERE canada.published_total > 0
    """).df()


def check_house_types_add_up(db):
    """Single + semi + row + apartment must equal the published "All types" row.

    Catches mistakes in dim_dwelling_type - for example if "Multiples" (which
    is itself a total of row houses and apartments) got treated as its own
    separate category.
    """
    return db.sql("""
        WITH parts AS (
            SELECT f.date_key, f.geography_key, f.coverage_key, f.stage_key,
                   SUM(f.units) AS our_total
            FROM fact_housing_activity f
            JOIN dim_dwelling_type t ON t.dwelling_type_key = f.dwelling_type_key
            WHERE t.dwelling_category IN
                  ('Single-detached', 'Semi-detached', 'Row houses', 'Apartments & other')
            GROUP BY 1, 2, 3, 4
        ),
        all_types AS (
            SELECT f.date_key, f.geography_key, f.coverage_key, f.stage_key,
                   SUM(f.units) AS published_total
            FROM fact_housing_activity f
            JOIN dim_dwelling_type t ON t.dwelling_type_key = f.dwelling_type_key
            WHERE t.is_total = TRUE
            GROUP BY 1, 2, 3, 4
        )
        SELECT 'house types add up to total'                    AS check_name,
               d.year_month || ' / ' || g.geo_name || ' / ' || s.stage_short AS detail,
               parts.our_total                                  AS our_value,
               all_types.published_total                        AS published_value
        FROM parts
        JOIN all_types ON all_types.date_key      = parts.date_key
                      AND all_types.geography_key = parts.geography_key
                      AND all_types.coverage_key  = parts.coverage_key
                      AND all_types.stage_key     = parts.stage_key
        JOIN dim_date              d ON d.date_key = parts.date_key
        JOIN dim_geography         g ON g.geography_key = parts.geography_key
        JOIN dim_construction_stage s ON s.stage_key = parts.stage_key
        WHERE all_types.published_total > 0
          AND d.year >= 2015
    """).df()


def check_nothing_got_lost(db):
    """No fact row should have ended up pointing at an "Unknown" dimension.

    Because we used LEFT JOIN when building the facts, a place name we didn't
    recognise ends up on the Unknown row (key -1) instead of disappearing. So
    this counts how many landed there. It should be zero.
    """
    return db.sql("""
        SELECT 'every row matched a real place' AS check_name,
               'fact_housing_activity'          AS detail,
               CAST(SUM(CASE WHEN geography_key <> -1 AND dwelling_type_key <> -1
                              AND stage_key <> -1 AND coverage_key <> -1
                             THEN 1 ELSE 0 END) AS DOUBLE) AS our_value,
               CAST(count(*) AS DOUBLE)                    AS published_value
        FROM fact_housing_activity
    """).df()


# Each check, and how we measure "close enough".
#
# The two kinds matter. We nearly got this wrong: using a percentage difference
# on the arrears RATE produced 12 fake failures. Ontario's rate works out to
# 0.0645% and CBA printed 0.06% - that's a 7.5% difference in percentage terms,
# which sounds terrible, but it's really 0.0045 of a percentage point, which is
# just rounding. When a number is already a small percentage, comparing
# percentage-of-a-percentage is meaningless.
CHECKS = [
    (check_arrears_add_up,     "percent"),
    (check_arrears_rate,       "rate"),
    (check_provinces_add_up,   "percent"),
    (check_house_types_add_up, "percent"),
    (check_nothing_got_lost,   "percent"),
]


def run_all_checks(save=True):
    db = duckdb.connect(str(WAREHOUSE_FILE))

    results = []
    for check_function, comparison_kind in CHECKS:
        table = check_function(db)
        if table.empty:
            print(f"  WARNING: {check_function.__name__} compared nothing")
            continue

        table["our_value"] = pd.to_numeric(table.our_value, errors="coerce")
        table["published_value"] = pd.to_numeric(table.published_value, errors="coerce")
        gap = (table.our_value - table.published_value).abs()

        if comparison_kind == "percent":
            # How far apart, as a percentage of the published number.
            table["difference"] = 100.0 * gap / table.published_value.abs()
            table["allowed"] = MAX_PERCENT_DIFFERENCE
            table["units"] = "%"
        else:
            # How far apart in plain percentage points.
            table["difference"] = gap
            table["allowed"] = MAX_RATE_DIFFERENCE
            table["units"] = "pp"

        table["close_enough"] = table.difference.fillna(0) <= table.allowed
        table["known_problem"] = [
            (name, detail) in KNOWN_SOURCE_PROBLEMS
            for name, detail in zip(table.check_name, table.detail)
        ]
        table["passed"] = table.close_enough | table.known_problem
        results.append(table)

    all_results = pd.concat(results, ignore_index=True)
    print_report(all_results)

    if save:
        to_save = all_results[["check_name", "detail", "our_value",
                               "published_value", "difference", "passed"]].copy()
        to_save["checked_at"] = datetime.now()
        db.register("temp_results", to_save)
        db.execute("DELETE FROM check_results")
        db.execute("INSERT INTO check_results SELECT * FROM temp_results")
        db.unregister("temp_results")

    db.close()
    return all_results


def print_report(results):
    print("=" * 78)
    print("STEP 3: CHECKING OUR NUMBERS AGAINST THE PUBLISHED ONES")
    print("=" * 78)

    for name, group in results.groupby("check_name"):
        real_failures = group[~group.close_enough & ~group.known_problem]
        known = group[~group.close_enough & group.known_problem]
        units = group.units.iloc[0]
        status = "OK  " if real_failures.empty else "FAIL"

        print(f"  [{status}] {name:<34} {len(group):>6,} comparisons   "
              f"worst gap {group.difference.max():.4f}{units}")

        for _, row in known.iterrows():
            print(f"          known source problem: {row.detail} "
                  f"({row.difference:.4f}{units})")
        for _, row in real_failures.head(5).iterrows():
            print(f"          FAILED: {row.detail}  "
                  f"we got {row.our_value}, published {row.published_value}")

    total = len(results)
    failures = int((~results.close_enough & ~results.known_problem).sum())
    known_count = int((~results.close_enough & results.known_problem).sum())
    matched = total - failures - known_count

    print("-" * 78)
    print(f"  {total:,} comparisons | {matched:,} matched ({100.0 * matched / total:.4f}%) "
          f"| {known_count} known source problems | {failures} real failures")

    return failures


def main():
    results = run_all_checks()
    failures = int((~results.close_enough & ~results.known_problem).sum())
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
