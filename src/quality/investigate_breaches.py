"""Drill into reconciliation breaches to decide: our bug, or publisher quirk?

This distinction matters. A pipeline bug must be fixed. A publisher
inconsistency must be *documented and tolerated*, or the gate cries wolf every
month and people stop reading it.
"""

import duckdb

from src.common.paths import duckdb_path

con = duckdb.connect(str(duckdb_path()), read_only=True)

print("=== 1. Arrears cross-foot breach: 1999-04 ===")
print(con.sql("""
    SELECT g.geo_name, g.cba_region, f.total_mortgages, f.mortgages_in_arrears,
           f.arrears_rate_pct
    FROM dw.fact_mortgage_arrears f
    JOIN dw.dim_geography g ON g.geography_key = f.geography_key
    WHERE f.date_key = 19990401
    ORDER BY g.cba_region
""").df().to_string(index=False))

print("\n--- neighbouring months, parts vs CANADA ---")
print(con.sql("""
    WITH parts AS (
        SELECT f.date_key, SUM(f.total_mortgages) AS parts_total
        FROM dw.fact_mortgage_arrears f
        JOIN dw.dim_geography g ON g.geography_key = f.geography_key
        WHERE g.cba_region <> 'CANADA' GROUP BY 1
    ), ca AS (
        SELECT f.date_key, f.total_mortgages AS canada_total
        FROM dw.fact_mortgage_arrears f
        JOIN dw.dim_geography g ON g.geography_key = f.geography_key
        WHERE g.cba_region = 'CANADA'
    )
    SELECT d.year_month, p.parts_total, c.canada_total,
           p.parts_total - c.canada_total AS diff,
           ROUND(100.0*(p.parts_total-c.canada_total)/c.canada_total, 4) AS pct
    FROM parts p JOIN ca c ON c.date_key=p.date_key
    JOIN dw.dim_date d ON d.date_key=p.date_key
    WHERE d.year_month BETWEEN '1999-01' AND '1999-08'
    ORDER BY 1
""").df().to_string(index=False))

print("\n=== 2. How many months breach the cross-foot at all? ===")
print(con.sql("""
    WITH parts AS (
        SELECT f.date_key, SUM(f.total_mortgages) AS parts_total
        FROM dw.fact_mortgage_arrears f
        JOIN dw.dim_geography g ON g.geography_key = f.geography_key
        WHERE g.cba_region <> 'CANADA' GROUP BY 1
    ), ca AS (
        SELECT f.date_key, f.total_mortgages AS canada_total
        FROM dw.fact_mortgage_arrears f
        JOIN dw.dim_geography g ON g.geography_key = f.geography_key
        WHERE g.cba_region = 'CANADA'
    )
    SELECT CASE WHEN abs(p.parts_total-c.canada_total)=0 THEN 'exact'
                WHEN abs(100.0*(p.parts_total-c.canada_total)/c.canada_total) < 0.5 THEN 'under 0.5%'
                WHEN abs(100.0*(p.parts_total-c.canada_total)/c.canada_total) < 1.0 THEN '0.5-1%'
                ELSE 'over 1%' END AS bucket,
           count(*) AS months
    FROM parts p JOIN ca c ON c.date_key=p.date_key
    GROUP BY 1 ORDER BY 2 DESC
""").df().to_string(index=False))

print("\n=== 3. Arrears rate breaches: absolute vs relative ===")
print(con.sql("""
    SELECT d.year_month, g.geo_name,
           f.mortgages_in_arrears, f.total_mortgages,
           ROUND(100.0*f.mortgages_in_arrears/f.total_mortgages, 4) AS derived,
           f.arrears_rate_pct AS published,
           ROUND(abs(100.0*f.mortgages_in_arrears/f.total_mortgages - f.arrears_rate_pct), 4)
               AS abs_pp_diff,
           ROUND(100.0*abs(100.0*f.mortgages_in_arrears/f.total_mortgages - f.arrears_rate_pct)
                 / f.arrears_rate_pct, 3) AS rel_pct
    FROM dw.fact_mortgage_arrears f
    JOIN dw.dim_date d ON d.date_key=f.date_key
    JOIN dw.dim_geography g ON g.geography_key=f.geography_key
    WHERE f.arrears_rate_pct IS NOT NULL AND f.mortgages_in_arrears IS NOT NULL
      AND 100.0*abs(100.0*f.mortgages_in_arrears/f.total_mortgages - f.arrears_rate_pct)
          / f.arrears_rate_pct > 1.0
    ORDER BY rel_pct DESC LIMIT 15
""").df().to_string(index=False))

print("\n--- distribution of ABSOLUTE percentage-point difference ---")
print(con.sql("""
    SELECT CASE WHEN abs(100.0*mortgages_in_arrears/total_mortgages - arrears_rate_pct) <= 0.005
                THEN 'within CBA rounding (<=0.005pp)'
                WHEN abs(100.0*mortgages_in_arrears/total_mortgages - arrears_rate_pct) <= 0.02
                THEN '0.005-0.02pp'
                ELSE 'over 0.02pp' END AS bucket,
           count(*) AS n
    FROM dw.fact_mortgage_arrears
    WHERE arrears_rate_pct IS NOT NULL AND mortgages_in_arrears IS NOT NULL
    GROUP BY 1 ORDER BY 2 DESC
""").df().to_string(index=False))

print("\n=== 4. Housing starts roll-up breach: 1995-03, centres 50k+ ===")
print(con.sql("""
    SELECT g.geo_name, g.geo_level, g.is_aggregate, f.units
    FROM dw.fact_housing_activity f
    JOIN dw.dim_geography g ON g.geography_key=f.geography_key
    JOIN dw.dim_coverage cv ON cv.coverage_key=f.coverage_key
    JOIN dw.dim_construction_stage s ON s.stage_key=f.stage_key
    JOIN dw.dim_dwelling_type dt ON dt.dwelling_type_key=f.dwelling_type_key
    WHERE f.date_key=19950301 AND cv.coverage_name='Centres 50,000 and over'
      AND s.stage_name='Housing starts' AND dt.dwelling_type_name='Total units'
    ORDER BY g.geo_level, g.geo_name
""").df().to_string(index=False))
