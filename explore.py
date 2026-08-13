"""
Ask the warehouse some real questions.

Run:  python explore.py

This isn't part of the pipeline - it's here to show what the data actually
says, and to sanity-check that the views make sense. A query can run perfectly
and still return nonsense, so it's worth looking at real answers with your own
eyes before trusting a dashboard built on top of them.

It's also the best place to start if you want to poke at the data yourself.
Copy a query, change it, run it again.
"""

import duckdb
import pandas as pd

from config import WAREHOUSE_FILE

pd.set_option("display.width", 200)
pd.set_option("display.max_columns", 30)

db = duckdb.connect(str(WAREHOUSE_FILE), read_only=True)


def ask(question, query):
    print(f"\n{'=' * 95}\n{question}\n{'=' * 95}")
    print(db.sql(query).df().to_string(index=False))


ask(
    "1. WHAT CHANGED? Provinces that moved housing starts most, latest month vs a year ago",
    """
    SELECT year_month, geo_name,
           units, units_last_year, change_vs_last_year,
           ROUND(percent_change, 1)           AS percent_change,
           ROUND(share_of_national_change, 1) AS share_of_change
    FROM what_changed
    WHERE geo_level = 'Province'
      AND coverage_name = 'Towns of 10,000 and over'
      AND date_key = (SELECT max(date_key) FROM what_changed)
    ORDER BY abs(change_vs_last_year) DESC
    LIMIT 10
    """,
)

ask(
    "2. WHERE ARE THE BOTTLENECKS? Metro areas with the biggest construction backlog",
    """
    SELECT year_month, geo_name,
           starts_last_12_months, completions_last_12_months,
           ROUND(completion_ratio, 2) AS completion_ratio,
           under_construction,
           ROUND(backlog_months, 1)   AS backlog_months
    FROM pipeline_health
    WHERE coverage_name = 'Big metro areas only'
      AND dwelling_type_name = 'Total units'
      AND date_key = (SELECT max(date_key) FROM pipeline_health)
      AND backlog_months IS NOT NULL
    ORDER BY backlog_months DESC
    LIMIT 10
    """,
)

ask(
    "3. Is building getting slower? All metro areas, one reading each June",
    """
    SELECT year_month,
           SUM(starts_last_12_months)      AS started_last_12_months,
           SUM(completions_last_12_months) AS finished_last_12_months,
           SUM(under_construction)         AS still_under_construction,
           ROUND(SUM(under_construction)
                 / NULLIF(SUM(completions_last_12_months) / 12.0, 0), 1) AS backlog_months
    FROM pipeline_health
    WHERE coverage_name = 'Big metro areas only'
      AND dwelling_type_name = 'Total units'
      AND month(date) = 6 AND year >= 2015
    GROUP BY year_month
    ORDER BY year_month
    """,
)

ask(
    "4. DELINQUENCY: who is behind on mortgage payments, and is it getting worse?",
    """
    SELECT year_month, region_name, covers_provinces,
           total_mortgages, mortgages_in_arrears,
           arrears_rate,
           ROUND(arrears_rate_last_year, 2) AS rate_a_year_ago,
           ROUND(change_vs_last_year, 3)    AS change
    FROM arrears_trend
    WHERE date_key = (SELECT max(date_key) FROM arrears_trend)
    ORDER BY arrears_rate DESC NULLS LAST
    """,
)

ask(
    "5. Do arrears follow mortgage rates? National view, one reading each June",
    """
    SELECT year_month,
           arrears_rate,
           ROUND(mortgage_rate_now, 2)           AS rate_now,
           ROUND(mortgage_rate_18_months_ago, 2) AS rate_18_months_ago
    FROM arrears_trend
    WHERE is_national = TRUE AND month(date) = 6 AND year >= 2016
    ORDER BY year_month
    """,
)

ask(
    "6. Built but not sold: metro areas with the most finished, empty homes",
    """
    SELECT d.year_month, g.geo_name,
           f.absorptions, f.unabsorbed_inventory,
           ROUND(100.0 * f.unabsorbed_inventory
                 / NULLIF(f.absorptions + f.unabsorbed_inventory, 0), 1) AS percent_unsold
    FROM fact_market_absorption f
    JOIN dim_date          d ON d.date_key = f.date_key
    JOIN dim_geography     g ON g.geography_key = f.geography_key
    JOIN dim_dwelling_type t ON t.dwelling_type_key = f.dwelling_type_key
    WHERE t.is_total = TRUE
      AND g.is_aggregate = FALSE
      AND f.unabsorbed_inventory IS NOT NULL
      AND d.date_key = (SELECT max(date_key) FROM fact_market_absorption)
    ORDER BY f.unabsorbed_inventory DESC
    LIMIT 10
    """,
)

db.close()
print("\nWant to try your own? Copy any query above, change it, and run again.")
