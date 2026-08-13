-- Saved queries for the two questions this project is built to answer.
--
-- A view is just a SELECT statement with a name. Writing the tricky
-- calculations here means everyone gets the same answer, instead of each
-- person rebuilding "months of backlog" slightly differently in Excel.
--
-- All three views leave out rows where is_aggregate is TRUE, except the
-- Canada row, so you can add things up without double-counting.


-- ============================================================================
-- QUESTION 1: "Where are the bottlenecks?"
-- ============================================================================
-- Puts the three construction stages side by side and works out two things.
--
--   completion_ratio  finished divided by started, over the last 12 months.
--                     Under 1 means more homes are being started than finished.
--
--   backlog_months    how many months it would take to finish everything
--                     currently under construction, at the recent finishing
--                     rate. This is the headline number. If it keeps rising
--                     while completions stay flat, building has stalled.
--
-- We use 12-month totals rather than single months because housing is very
-- seasonal - almost nothing gets built in a Canadian January, so comparing
-- January to June mostly measures the weather.

CREATE OR REPLACE VIEW pipeline_health AS
WITH by_stage AS (
    SELECT
        f.date_key,
        f.geography_key,
        f.coverage_key,
        f.dwelling_type_key,
        SUM(CASE WHEN s.stage_short = 'Starts'             THEN f.units END) AS starts,
        SUM(CASE WHEN s.stage_short = 'Under construction' THEN f.units END) AS under_construction,
        SUM(CASE WHEN s.stage_short = 'Completions'        THEN f.units END) AS completions
    FROM fact_housing_activity f
    JOIN dim_construction_stage s ON s.stage_key = f.stage_key
    GROUP BY 1, 2, 3, 4
)
SELECT
    d.date_key,
    d.date,
    d.year_month,
    d.year,
    g.geo_name,
    g.geo_level,
    g.province_name,
    g.arrears_region,
    c.coverage_name,
    t.dwelling_type_name,
    t.dwelling_category,
    b.starts,
    b.under_construction,
    b.completions,

    SUM(b.starts) OVER (
        PARTITION BY b.geography_key, b.coverage_key, b.dwelling_type_key
        ORDER BY d.date_key ROWS BETWEEN 11 PRECEDING AND CURRENT ROW
    ) AS starts_last_12_months,

    SUM(b.completions) OVER (
        PARTITION BY b.geography_key, b.coverage_key, b.dwelling_type_key
        ORDER BY d.date_key ROWS BETWEEN 11 PRECEDING AND CURRENT ROW
    ) AS completions_last_12_months,

    SUM(b.completions) OVER (
        PARTITION BY b.geography_key, b.coverage_key, b.dwelling_type_key
        ORDER BY d.date_key ROWS BETWEEN 11 PRECEDING AND CURRENT ROW
    ) / NULLIF(SUM(b.starts) OVER (
        PARTITION BY b.geography_key, b.coverage_key, b.dwelling_type_key
        ORDER BY d.date_key ROWS BETWEEN 11 PRECEDING AND CURRENT ROW
    ), 0) AS completion_ratio,

    b.under_construction / NULLIF(SUM(b.completions) OVER (
        PARTITION BY b.geography_key, b.coverage_key, b.dwelling_type_key
        ORDER BY d.date_key ROWS BETWEEN 11 PRECEDING AND CURRENT ROW
    ) / 12.0, 0) AS backlog_months

FROM by_stage b
JOIN dim_date              d ON d.date_key = b.date_key
JOIN dim_geography         g ON g.geography_key = b.geography_key
JOIN dim_coverage          c ON c.coverage_key = b.coverage_key
JOIN dim_dwelling_type     t ON t.dwelling_type_key = b.dwelling_type_key
WHERE g.is_aggregate = FALSE OR g.geo_level = 'Country';


-- ============================================================================
-- QUESTION 2: "What changed?"
-- ============================================================================
-- Compares each place to the same month a year ago, then works out how much of
-- the national change it accounts for.
--
-- Ranking by percentage growth always puts the smallest places on top - a 100%
-- rise in Charlottetown is 29 houses. Ranking by contribution answers what
-- people actually want to know, which is who moved the national number.

CREATE OR REPLACE VIEW what_changed AS
WITH monthly_starts AS (
    SELECT
        f.date_key,
        f.geography_key,
        f.coverage_key,
        SUM(f.units) AS units
    FROM fact_housing_activity f
    JOIN dim_construction_stage s ON s.stage_key = f.stage_key
    JOIN dim_dwelling_type      t ON t.dwelling_type_key = f.dwelling_type_key
    JOIN dim_geography          g ON g.geography_key = f.geography_key
    WHERE s.stage_short = 'Starts'
      AND t.is_total = TRUE
      AND (g.is_aggregate = FALSE OR g.geo_level = 'Country')
    GROUP BY 1, 2, 3
),
with_last_year AS (
    SELECT
        m.*,
        LAG(m.units, 12) OVER (
            PARTITION BY m.geography_key, m.coverage_key ORDER BY m.date_key
        ) AS units_last_year
    FROM monthly_starts m
)
SELECT
    d.date_key,
    d.year_month,
    d.year,
    g.geo_name,
    g.geo_level,
    g.province_name,
    c.coverage_name,
    w.units,
    w.units_last_year,
    w.units - w.units_last_year AS change_vs_last_year,

    100.0 * (w.units - w.units_last_year)
        / NULLIF(w.units_last_year, 0) AS percent_change,

    -- Each place's share of the total movement. We use the sum of ABSOLUTE
    -- changes on the bottom, so that a big rise and a big fall cancelling out
    -- doesn't leave us dividing by nearly zero.
    100.0 * (w.units - w.units_last_year) / NULLIF(SUM(abs(w.units - w.units_last_year))
        OVER (PARTITION BY w.date_key, w.coverage_key, g.geo_level), 0)
        AS share_of_national_change

FROM with_last_year w
JOIN dim_date      d ON d.date_key = w.date_key
JOIN dim_geography g ON g.geography_key = w.geography_key
JOIN dim_coverage  c ON c.coverage_key = w.coverage_key
WHERE w.units_last_year IS NOT NULL;


-- ============================================================================
-- Mortgage delinquency over time
-- ============================================================================
-- Arrears on their own don't tell you much. Putting the mortgage rate from 18
-- months ago next to them helps: most Canadian mortgages renew every few
-- years, so if rates jumped and arrears rise a year and a half later, that
-- points at people struggling with a higher payment after renewing.

CREATE OR REPLACE VIEW arrears_trend AS
SELECT
    d.date_key,
    d.date,
    d.year_month,
    d.year,
    r.region_name,
    r.covers_provinces,
    r.is_national,
    f.total_mortgages,
    f.mortgages_in_arrears,
    f.arrears_rate,
    f.is_hidden,

    LAG(f.arrears_rate, 12) OVER (
        PARTITION BY f.arrears_region_key ORDER BY d.date_key
    ) AS arrears_rate_last_year,

    f.arrears_rate - LAG(f.arrears_rate, 12) OVER (
        PARTITION BY f.arrears_region_key ORDER BY d.date_key
    ) AS change_vs_last_year,

    m.rate_5year AS mortgage_rate_now,

    LAG(m.rate_5year, 18) OVER (ORDER BY d.date_key) AS mortgage_rate_18_months_ago

FROM fact_mortgage_arrears f
JOIN dim_date           d ON d.date_key = f.date_key
JOIN dim_arrears_region r ON r.arrears_region_key = f.arrears_region_key
LEFT JOIN fact_mortgage_rate m ON m.date_key = f.date_key
