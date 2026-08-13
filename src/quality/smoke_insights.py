"""
Sanity-check that the views produce answers a human would recognise as correct.

A view can compile, return rows, and still be nonsense. These queries are the
ones a business user would actually ask, run against the real warehouse. If the
national arrears rate came back as 12% or Toronto disappeared from the movers
list, that would show up here long before anyone opened Power BI.
"""

from __future__ import annotations

import duckdb
import pandas as pd

from src.common.paths import duckdb_path

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 40)

con = duckdb.connect(str(duckdb_path()), read_only=True)


def show(title: str, sql: str) -> None:
    print(f"\n{'=' * 100}\n{title}\n{'=' * 100}")
    print(con.sql(sql).df().to_string(index=False))


show(
    "Q1. WHAT CHANGED? Biggest provincial movers in housing starts, latest month (YoY)",
    """
    SELECT year_month, geo_name, units, units_ly, yoy_change,
           ROUND(yoy_pct, 1)                AS yoy_pct,
           ROUND(contribution_share_pct, 1) AS contribution_pct
    FROM dw.vw_what_changed
    WHERE geo_level = 'Province'
      AND coverage_name = 'Centres 10,000 and over'
      AND date_key = (SELECT max(date_key) FROM dw.vw_what_changed)
    ORDER BY abs(yoy_change) DESC
    LIMIT 10
    """,
)

# NOTE ON COVERAGE: CMHC stopped publishing completions and under-construction
# at province level (34-10-0143 / 34-10-0151) after 2022-12, but continues to
# publish all three stages for census metropolitan areas. Any current bottleneck
# analysis must therefore run on CMA coverage. See docs/data-availability.md.
show(
    "Q2. WHERE ARE THE BOTTLENECKS? CMAs with the largest construction backlog",
    """
    SELECT year_month, geo_name,
           starts_12m, completions_12m,
           ROUND(completion_ratio_12m, 3) AS completion_ratio,
           under_construction,
           ROUND(backlog_months, 1)       AS backlog_months
    FROM dw.vw_construction_pipeline
    WHERE coverage_name = 'Census metropolitan areas'
      AND dwelling_type_name = 'Total units'
      AND date_key = (SELECT max(date_key) FROM dw.vw_construction_pipeline)
      AND backlog_months IS NOT NULL
    ORDER BY backlog_months DESC
    LIMIT 12
    """,
)

show(
    "Q3. Has the pipeline stalled? All-CMA backlog months, one reading per year",
    """
    SELECT year_month,
           SUM(starts_12m)         AS starts_12m,
           SUM(completions_12m)    AS completions_12m,
           ROUND(SUM(completions_12m) / NULLIF(SUM(starts_12m), 0), 3)
                                   AS completion_ratio,
           SUM(under_construction) AS under_construction,
           ROUND(SUM(under_construction) / NULLIF(SUM(completions_12m) / 12.0, 0), 1)
                                   AS backlog_months
    FROM dw.vw_construction_pipeline
    WHERE coverage_name = 'Census metropolitan areas'
      AND dwelling_type_name = 'Total units'
      AND month(date) = 6 AND year >= 2015
    GROUP BY year_month
    ORDER BY year_month
    """,
)

show(
    "Q4. DELINQUENCY: current arrears by region, with year-over-year move",
    """
    SELECT year_month, region_name,
           total_mortgages, mortgages_in_arrears,
           arrears_rate_pct,
           ROUND(arrears_rate_prev_year, 2) AS rate_last_year,
           ROUND(arrears_rate_yoy_pp, 3)    AS yoy_change_pp,
           ROUND(conventional_5yr_rate, 2)  AS rate_5yr_now,
           ROUND(rate_lag_18m, 2)           AS rate_5yr_18m_ago
    FROM dw.vw_arrears_trend
    WHERE date_key = (SELECT max(date_key) FROM dw.vw_arrears_trend)
    ORDER BY arrears_rate_pct DESC NULLS LAST
    """,
)

show(
    "Q5. Does the renewal-shock story hold? National arrears vs the 5-yr rate 18m earlier",
    """
    SELECT year_month,
           arrears_rate_pct,
           ROUND(conventional_5yr_rate, 2) AS rate_now,
           ROUND(rate_lag_18m, 2)          AS rate_18m_ago
    FROM dw.vw_arrears_trend
    WHERE cba_region = 'CANADA' AND month(date) = 6 AND year >= 2016
    ORDER BY year_month
    """,
)

show(
    "Q6. Built but not sold: CMAs with the most months of unabsorbed inventory",
    """
    SELECT year_month, geo_name, dwelling_type_name,
           absorptions_12m, unabsorbed_inventory,
           ROUND(100 * unabsorbed_share, 1) AS unabsorbed_pct,
           ROUND(months_of_inventory, 1)    AS months_of_inventory
    FROM dw.vw_absorption_health
    WHERE geo_level = 'CMA'
      AND dwelling_type_name = 'Total units'
      AND date_key = (SELECT max(date_key) FROM dw.vw_absorption_health
                      WHERE unabsorbed_inventory IS NOT NULL)
      AND months_of_inventory IS NOT NULL
    ORDER BY months_of_inventory DESC
    LIMIT 10
    """,
)
