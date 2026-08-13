"""
Build the fact tables.

Every loader follows the same shape:

    read raw CSV  ->  project to (keys..., measures)  ->  LEFT JOIN dimensions
                  ->  COALESCE unresolved keys to -1   ->  INSERT into dw.fact_*

Two rules are enforced everywhere and are worth stating up front:

1. LEFT JOIN, never INNER JOIN, onto dimensions. An INNER JOIN silently drops
   fact rows whose dimension member is missing, and the loss shows up months
   later as unexplained reconciliation variance. LEFT JOIN + COALESCE to the
   Unknown member (-1) keeps row counts conserved and makes the problem
   measurable - src/quality/checks.py fails the run if unknown keys exceed 0.1%.

2. TRY_CAST, never CAST, on VALUE. StatCan writes empty strings and footnote
   markers into numeric columns for suppressed cells. TRY_CAST yields NULL,
   which is semantically correct: "not published" is not "zero". A hard CAST
   would abort the whole load on one suppressed cell.
"""

from __future__ import annotations

import duckdb

from src.common.logging_setup import get_logger
from src.common.paths import RAW_DIR, config

log = get_logger(__name__)

# StatCan REF_DATE is 'YYYY-MM'; the date key is 'YYYYMM01' as an integer.
DATE_KEY = "TRY_CAST(replace(REF_DATE, '-', '') || '01' AS INTEGER)"


def _raw(alias: str) -> str:
    """DuckDB read_csv() call for a raw source, by alias."""
    spec = next(s for s in config()["statcan"]["tables"] if s["alias"] == alias)
    path = (RAW_DIR / alias / f"{spec['pid']}.csv").as_posix()
    return f"read_csv('{path}', header=true, all_varchar=true, sample_size=-1)"


def _source_key(con: duckdb.DuckDBPyConnection, alias: str) -> int:
    row = con.sql(
        f"SELECT source_key FROM dw.dim_source WHERE source_alias = '{alias}'"
    ).fetchone()
    return row[0] if row else -1


# --------------------------------------------------------------------------- #
# fact_housing_activity
# --------------------------------------------------------------------------- #
# Each source maps to one coverage. The SAAR table has no dwelling-type or stage
# column at all, so those are pinned to constants - which is itself a modelling
# statement: that series is total housing starts only.
HOUSING_SOURCES = [
    {
        "alias": "housing_activity_centres_10k",
        "coverage": "Centres 10,000 and over",
        "stage_col": '"Housing estimates"',
        "unit_col": '"Type of unit"',
    },
    {
        "alias": "housing_activity_centres_50k",
        "coverage": "Centres 50,000 and over",
        "stage_col": '"Housing estimates"',
        "unit_col": '"Type of unit"',
    },
    {
        "alias": "housing_activity_cma",
        "coverage": "Census metropolitan areas",
        "stage_col": '"Housing estimates"',
        "unit_col": '"Type of unit"',
    },
    {
        "alias": "housing_starts_provincial_saar",
        "coverage": "All areas (SAAR)",
        "stage_col": "'Housing starts'",
        "unit_col": "'Total units'",
    },
]


def build_fact_housing_activity(con: duckdb.DuckDBPyConnection) -> int:
    con.execute("DELETE FROM dw.fact_housing_activity")
    total = 0

    for src in HOUSING_SOURCES:
        skey = _source_key(con, src["alias"])
        sql = f"""
        INSERT INTO dw.fact_housing_activity
              (date_key, geography_key, dwelling_type_key, stage_key,
               coverage_key, source_key, units, is_estimated)
        SELECT
              {DATE_KEY}                          AS date_key,
              COALESCE(g.geography_key, -1)       AS geography_key,
              COALESCE(d.dwelling_type_key, -1)   AS dwelling_type_key,
              COALESCE(s.stage_key, -1)           AS stage_key,
              COALESCE(c.coverage_key, -1)        AS coverage_key,
              {skey}                              AS source_key,
              TRY_CAST(r.VALUE AS DECIMAL(18,2))  AS units,
              -- StatCan STATUS 'E' marks an estimate; carry it so the dashboard
              -- can visually flag figures that are not final.
              COALESCE(r.STATUS, '') = 'E'        AS is_estimated
        FROM {_raw(src['alias'])} r
        LEFT JOIN dw.dim_geography        g ON g.geo_name           = r.GEO
                                           AND g.geo_level         <> 'CBA Region'
        LEFT JOIN dw.dim_dwelling_type    d ON d.dwelling_type_name = {src['unit_col']}
        LEFT JOIN dw.dim_construction_stage s ON s.stage_name       = {src['stage_col']}
        LEFT JOIN dw.dim_coverage         c ON c.coverage_name      = '{src['coverage']}'
        LEFT JOIN dw.dim_date             dd ON dd.date_key         = {DATE_KEY}
        WHERE dd.date_key IS NOT NULL
        """
        con.execute(sql)
        n = con.sql("SELECT count(*) FROM dw.fact_housing_activity").fetchone()[0] - total
        total += n
        log.info("  fact_housing_activity += %7d rows from %s [%s]",
                 n, src["alias"], src["coverage"])

    log.info("fact_housing_activity: %d rows", total)
    return total


# --------------------------------------------------------------------------- #
# fact_market_absorption  - the bottleneck fact
# --------------------------------------------------------------------------- #
def build_fact_market_absorption(con: duckdb.DuckDBPyConnection) -> int:
    """
    Two sources, three measures, one grain.

    34-10-0149 carries absorptions and unabsorbed inventory as MEMBERS of a
    'Completed dwelling units' dimension rather than as columns. That is a
    classic long-to-wide pivot: the warehouse wants them side by side so a
    single row answers "how much completed, how much of it cleared?".

    34-10-0162 (unoccupied units) is a separate table at a compatible grain, so
    it is FULL OUTER JOINed in - not UNIONed. A UNION would create two rows per
    (month, place, type) with half the measures NULL in each.
    """
    con.execute("DELETE FROM dw.fact_market_absorption")

    skey_abs = _source_key(con, "absorptions_unabsorbed")
    skey_unocc = _source_key(con, "unoccupied_new_housing")

    con.execute(f"""
    INSERT INTO dw.fact_market_absorption
          (date_key, geography_key, dwelling_type_key, source_key,
           absorptions, unabsorbed_inventory, unoccupied_units)
    WITH absorption AS (
        SELECT {DATE_KEY} AS date_key,
               r.GEO      AS geo_name,
               r."Type of dwelling unit" AS unit,
               -- pivot the long 'Completed dwelling units' member into columns
               SUM(CASE WHEN r."Completed dwelling units" = 'Absorptions'
                        THEN TRY_CAST(r.VALUE AS DECIMAL(18,2)) END) AS absorptions,
               SUM(CASE WHEN r."Completed dwelling units" = 'Unabsorbed inventory'
                        THEN TRY_CAST(r.VALUE AS DECIMAL(18,2)) END) AS unabsorbed_inventory
        FROM {_raw('absorptions_unabsorbed')} r
        GROUP BY 1, 2, 3
    ),
    unoccupied AS (
        SELECT {DATE_KEY} AS date_key,
               r.GEO      AS geo_name,
               r."Type of unit" AS unit,
               SUM(TRY_CAST(r.VALUE AS DECIMAL(18,2))) AS unoccupied_units
        FROM {_raw('unoccupied_new_housing')} r
        GROUP BY 1, 2, 3
    ),
    combined AS (
        SELECT COALESCE(a.date_key, u.date_key) AS date_key,
               COALESCE(a.geo_name, u.geo_name) AS geo_name,
               COALESCE(a.unit,     u.unit)     AS unit,
               a.absorptions,
               a.unabsorbed_inventory,
               u.unoccupied_units
        FROM absorption a
        FULL OUTER JOIN unoccupied u
          ON  a.date_key = u.date_key
          AND a.geo_name = u.geo_name
          AND a.unit     = u.unit
    )
    SELECT k.date_key,
           COALESCE(g.geography_key, -1),
           COALESCE(d.dwelling_type_key, -1),
           CASE WHEN k.absorptions IS NOT NULL OR k.unabsorbed_inventory IS NOT NULL
                THEN {skey_abs} ELSE {skey_unocc} END,
           k.absorptions,
           k.unabsorbed_inventory,
           k.unoccupied_units
    FROM combined k
    LEFT JOIN dw.dim_geography     g ON g.geo_name = k.geo_name AND g.geo_level <> 'CBA Region'
    LEFT JOIN dw.dim_dwelling_type d ON d.dwelling_type_name = k.unit
    INNER JOIN dw.dim_date        dd ON dd.date_key = k.date_key
    """)

    n = con.sql("SELECT count(*) FROM dw.fact_market_absorption").fetchone()[0]
    log.info("fact_market_absorption: %d rows", n)
    return n


# --------------------------------------------------------------------------- #
# fact_mortgage_arrears
# --------------------------------------------------------------------------- #
def build_fact_mortgage_arrears(con: duckdb.DuckDBPyConnection) -> int:
    """
    CBA arrears, joined to the CBA Region members of dim_geography.

    is_suppressed is set where CBA withholds the arrears count for confidentiality
    (small territories). That is recorded as an explicit flag rather than a zero,
    because a zero would drag a national average down and look like an
    improvement in credit quality that never happened.
    """
    con.execute("DELETE FROM dw.fact_mortgage_arrears")
    csv_path = (RAW_DIR / "cba_arrears" / "cba_arrears.csv").as_posix()
    skey = _source_key(con, "cba_arrears")

    con.execute(f"""
    INSERT INTO dw.fact_mortgage_arrears
          (date_key, arrears_region_key, source_key, total_mortgages,
           mortgages_in_arrears, arrears_rate_pct, is_suppressed)
    SELECT TRY_CAST(replace(r.period, '-', '') || '01' AS INTEGER),
           COALESCE(ar.arrears_region_key, -1),
           {skey},
           TRY_CAST(r.total_mortgages AS BIGINT),
           TRY_CAST(r.mortgages_in_arrears AS BIGINT),
           TRY_CAST(r.arrears_rate_pct AS DECIMAL(9,4)),
           r.mortgages_in_arrears IS NULL
    FROM read_csv('{csv_path}', header=true, all_varchar=true, sample_size=-1) r
    LEFT JOIN dw.dim_arrears_region ar ON ar.region_code = r.region
    INNER JOIN dw.dim_date dd
           ON dd.date_key = TRY_CAST(replace(r.period, '-', '') || '01' AS INTEGER)
    """)

    n = con.sql("SELECT count(*) FROM dw.fact_mortgage_arrears").fetchone()[0]
    log.info("fact_mortgage_arrears: %d rows", n)
    return n


# --------------------------------------------------------------------------- #
# fact_mortgage_originations
# --------------------------------------------------------------------------- #
def build_fact_mortgage_originations(con: duckdb.DuckDBPyConnection) -> int:
    """
    Bank of Canada lending table.

    The publisher stacks dollars and interest rates in one VALUE column,
    discriminated by 'Unit of measure'. Splitting them into typed columns is the
    whole point of the warehouse: funds_advanced is additive, effective_rate
    never is, and leaving them stacked guarantees somebody eventually sums a
    column of interest rates.
    """
    con.execute("DELETE FROM dw.fact_mortgage_originations")
    skey = _source_key(con, "boc_lending")

    # COORDINATE is 'geo.component.unit'; element 2 is the Components member ID.
    # Joining on that instead of the label is what keeps the grain honest.
    con.execute(f"""
    INSERT INTO dw.fact_mortgage_originations
          (date_key, credit_product_key, source_key,
           funds_advanced, outstanding_balance, effective_rate)
    WITH pivoted AS (
        SELECT {DATE_KEY} AS date_key,
               TRY_CAST(split_part(r.COORDINATE, '.', 2) AS INTEGER) AS member_id,
               SUM(CASE WHEN r."Unit of measure" = 'Dollars'
                         AND lower(r."Components") LIKE '%funds advanced%'
                        THEN TRY_CAST(r.VALUE AS DECIMAL(20,2)) END) AS funds_advanced,
               SUM(CASE WHEN r."Unit of measure" = 'Dollars'
                         AND lower(r."Components") LIKE '%outstanding%'
                        THEN TRY_CAST(r.VALUE AS DECIMAL(20,2)) END) AS outstanding_balance,
               -- Rates are averaged, never summed. A component/period pair can
               -- carry both a dollar and a rate row; AVG over the rate rows is
               -- the only defensible collapse.
               AVG(CASE WHEN r."Unit of measure" = 'Interest rate'
                        THEN TRY_CAST(r.VALUE AS DECIMAL(9,4)) END)   AS effective_rate
        FROM {_raw('boc_lending')} r
        GROUP BY 1, 2
    )
    SELECT p.date_key,
           COALESCE(cp.credit_product_key, -1),
           {skey},
           p.funds_advanced,
           p.outstanding_balance,
           p.effective_rate
    FROM pivoted p
    LEFT JOIN dw.dim_credit_product cp
           ON cp.source_alias = 'boc_lending'
          AND cp.member_id    = p.member_id
    INNER JOIN dw.dim_date dd ON dd.date_key = p.date_key
    """)

    n = con.sql("SELECT count(*) FROM dw.fact_mortgage_originations").fetchone()[0]
    log.info("fact_mortgage_originations: %d rows", n)
    return n


# --------------------------------------------------------------------------- #
# fact_price_index / fact_household_credit / fact_rate_environment
# --------------------------------------------------------------------------- #
def build_fact_price_index(con: duckdb.DuckDBPyConnection) -> int:
    con.execute("DELETE FROM dw.fact_price_index")
    skey = _source_key(con, "new_housing_price_index")

    con.execute(f"""
    INSERT INTO dw.fact_price_index
          (date_key, geography_key, price_component_key, source_key, index_value)
    SELECT {DATE_KEY},
           COALESCE(g.geography_key, -1),
           COALESCE(pc.price_component_key, -1),
           {skey},
           TRY_CAST(r.VALUE AS DECIMAL(12,4))
    FROM {_raw('new_housing_price_index')} r
    LEFT JOIN dw.dim_geography       g  ON g.geo_name = r.GEO AND g.geo_level <> 'CBA Region'
    LEFT JOIN dw.dim_price_component pc ON pc.component_name = r."New housing price indexes"
    INNER JOIN dw.dim_date           dd ON dd.date_key = {DATE_KEY}
    """)

    n = con.sql("SELECT count(*) FROM dw.fact_price_index").fetchone()[0]
    log.info("fact_price_index: %d rows", n)
    return n


def build_fact_household_credit(con: duckdb.DuckDBPyConnection) -> int:
    con.execute("DELETE FROM dw.fact_household_credit")
    skey = _source_key(con, "household_credit_liabilities")

    # COORDINATE is 'geo.seasonality.credit'; element 3 is the credit member ID.
    # Joining on the LABEL here is what originally broke this fact's primary key:
    # 'Non-banks' resolves to six different series.
    con.execute(f"""
    INSERT INTO dw.fact_household_credit
          (date_key, credit_product_key, seasonality, source_key, balance_dollars)
    SELECT {DATE_KEY},
           COALESCE(cp.credit_product_key, -1),
           r."Seasonality",
           {skey},
           TRY_CAST(r.VALUE AS DECIMAL(20,2))
    FROM {_raw('household_credit_liabilities')} r
    LEFT JOIN dw.dim_credit_product cp
           ON cp.source_alias = 'household_credit_liabilities'
          AND cp.member_id    = TRY_CAST(split_part(r.COORDINATE, '.', 3) AS INTEGER)
    INNER JOIN dw.dim_date dd ON dd.date_key = {DATE_KEY}
    """)

    n = con.sql("SELECT count(*) FROM dw.fact_household_credit").fetchone()[0]
    log.info("fact_household_credit: %d rows", n)
    return n


def build_fact_rate_environment(con: duckdb.DuckDBPyConnection) -> int:
    con.execute("DELETE FROM dw.fact_rate_environment")
    skey = _source_key(con, "mortgage_rate_5yr")

    con.execute(f"""
    INSERT INTO dw.fact_rate_environment (date_key, source_key, conventional_5yr_rate)
    SELECT {DATE_KEY}, {skey}, AVG(TRY_CAST(r.VALUE AS DECIMAL(9,4)))
    FROM {_raw('mortgage_rate_5yr')} r
    INNER JOIN dw.dim_date dd ON dd.date_key = {DATE_KEY}
    GROUP BY 1, 2
    """)

    n = con.sql("SELECT count(*) FROM dw.fact_rate_environment").fetchone()[0]
    log.info("fact_rate_environment: %d rows", n)
    return n
