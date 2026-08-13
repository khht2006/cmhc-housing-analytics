/* =============================================================================
   Analytical views.

   These exist to answer the two questions the dashboard is built around:

       "what changed?"            -> vw_what_changed, vw_arrears_trend
       "where are the bottlenecks?" -> vw_construction_pipeline, vw_absorption_health

   Design rule: a view here encodes a DEFINITION that must not vary between
   users - what "backlog months" means, which rows count as leaves, which
   coverage is authoritative. Presentation logic (formatting, colour, what a
   given page filters to) stays in Power BI. If a number needs to be identical
   in a report, an email and an ad-hoc query, it belongs in a view.

   Every view filters is_aggregate = FALSE by default, so a careless SUM over a
   view cannot double-count publisher roll-ups.
   ============================================================================= */

/* ---------------------------------------------------------------------------
   vw_construction_pipeline - the bottleneck view.

   Pivots the three construction stages side by side so one row tells the whole
   supply story for a place and month, then derives two diagnostics:

     completion_ratio  completions / starts, 12-month rolling.
                       Below 1 means the pipeline is filling faster than it
                       empties.

     backlog_months    units under construction / trailing 12-month average
                       monthly completions. This is the headline bottleneck
                       metric: "at the current completion rate, how many months
                       to clear what is already started?" Rising backlog with
                       flat completions is a stalled pipeline, which looks very
                       different from a demand slowdown even though both show up
                       as falling completions.
   --------------------------------------------------------------------------- */
CREATE OR REPLACE VIEW dw.vw_construction_pipeline AS
WITH staged AS (
    SELECT
        f.date_key,
        f.geography_key,
        f.coverage_key,
        f.dwelling_type_key,
        SUM(CASE WHEN s.stage_short = 'Starts'             THEN f.units END) AS starts,
        SUM(CASE WHEN s.stage_short = 'Under construction'  THEN f.units END) AS under_construction,
        SUM(CASE WHEN s.stage_short = 'Completions'         THEN f.units END) AS completions
    FROM dw.fact_housing_activity f
    JOIN dw.dim_construction_stage s ON s.stage_key = f.stage_key
    GROUP BY 1, 2, 3, 4
)
SELECT
    d.date_key,
    d.date,
    d.year_month,
    d.year,
    d.quarter_name,
    g.geo_name,
    g.geo_level,
    g.province_code,
    g.province_name,
    g.cba_region,
    cv.coverage_name,
    dt.dwelling_type_name,
    dt.dwelling_category,
    st.starts,
    st.under_construction,
    st.completions,

    -- Trailing 12-month totals: monthly housing figures are violently seasonal,
    -- so a raw month-over-month read is mostly noise about the weather.
    SUM(st.starts) OVER w12      AS starts_12m,
    SUM(st.completions) OVER w12 AS completions_12m,

    CASE WHEN SUM(st.starts) OVER w12 > 0
         THEN SUM(st.completions) OVER w12 / SUM(st.starts) OVER w12
    END AS completion_ratio_12m,

    CASE WHEN SUM(st.completions) OVER w12 > 0
         THEN st.under_construction / (SUM(st.completions) OVER w12 / 12.0)
    END AS backlog_months,

    -- Year-over-year on the same calendar month: the correct seasonal control.
    LAG(st.starts, 12) OVER wpart      AS starts_ly,
    LAG(st.completions, 12) OVER wpart AS completions_ly
FROM staged st
JOIN dw.dim_date          d  ON d.date_key          = st.date_key
JOIN dw.dim_geography     g  ON g.geography_key     = st.geography_key
JOIN dw.dim_coverage      cv ON cv.coverage_key     = st.coverage_key
JOIN dw.dim_dwelling_type dt ON dt.dwelling_type_key = st.dwelling_type_key
-- Keep leaves, plus the national row. Within any single geo_level this is then
-- safe to SUM: the excluded rows are the ones that overlap their siblings
-- ('Atlantic provinces', 'BC excluding Vancouver', the combined
-- 'Ottawa-Gatineau' that sits beside its own two 'part' rows).
WHERE g.is_aggregate = FALSE OR g.geo_level = 'Country'
WINDOW
    wpart AS (PARTITION BY st.geography_key, st.coverage_key, st.dwelling_type_key
              ORDER BY d.date_key),
    w12   AS (PARTITION BY st.geography_key, st.coverage_key, st.dwelling_type_key
              ORDER BY d.date_key ROWS BETWEEN 11 PRECEDING AND CURRENT ROW);


/* ---------------------------------------------------------------------------
   vw_absorption_health - completed supply that has not cleared.

   unabsorbed_share is the share of recently completed dwellings still sitting
   empty. Distinguishes "we are not building" from "we built and nobody bought",
   which have opposite policy and business implications.
   --------------------------------------------------------------------------- */
CREATE OR REPLACE VIEW dw.vw_absorption_health AS
SELECT
    d.date_key,
    d.date,
    d.year_month,
    d.year,
    g.geo_name,
    g.geo_level,
    g.province_code,
    g.cba_region,
    dt.dwelling_type_name,
    dt.dwelling_category,
    f.absorptions,
    f.unabsorbed_inventory,
    f.unoccupied_units,

    SUM(f.absorptions) OVER w12 AS absorptions_12m,

    CASE WHEN COALESCE(f.absorptions, 0) + COALESCE(f.unabsorbed_inventory, 0) > 0
         THEN f.unabsorbed_inventory
              / (COALESCE(f.absorptions, 0) + COALESCE(f.unabsorbed_inventory, 0))
    END AS unabsorbed_share,

    -- Months of unsold inventory at the trailing absorption rate.
    CASE WHEN SUM(f.absorptions) OVER w12 > 0
         THEN f.unabsorbed_inventory / (SUM(f.absorptions) OVER w12 / 12.0)
    END AS months_of_inventory
FROM dw.fact_market_absorption f
JOIN dw.dim_date          d  ON d.date_key          = f.date_key
JOIN dw.dim_geography     g  ON g.geography_key     = f.geography_key
JOIN dw.dim_dwelling_type dt ON dt.dwelling_type_key = f.dwelling_type_key
WHERE g.is_aggregate = FALSE OR g.geo_level = 'Country'
WINDOW w12 AS (PARTITION BY f.geography_key, f.dwelling_type_key
               ORDER BY d.date_key ROWS BETWEEN 11 PRECEDING AND CURRENT ROW);


/* ---------------------------------------------------------------------------
   vw_arrears_trend - delinquency with the context needed to read it.

   An arrears rate in isolation says little. Joined to the rate environment it
   becomes interpretable: arrears rising while the 5-year rate is flat is a
   labour-market story, whereas arrears rising 18-24 months after a rate spike
   is a renewal-shock story. rate_lag_18m makes that comparison a column rather
   than an argument.
   --------------------------------------------------------------------------- */
CREATE OR REPLACE VIEW dw.vw_arrears_trend AS
SELECT
    d.date_key,
    d.date,
    d.year_month,
    d.year,
    ar.region_name,
    ar.region_code,
    ar.covers_provinces,
    ar.is_national,
    f.total_mortgages,
    f.mortgages_in_arrears,
    f.arrears_rate_pct,
    f.is_suppressed,

    LAG(f.arrears_rate_pct, 1)  OVER wreg AS arrears_rate_prev_month,
    LAG(f.arrears_rate_pct, 12) OVER wreg AS arrears_rate_prev_year,
    f.arrears_rate_pct - LAG(f.arrears_rate_pct, 12) OVER wreg AS arrears_rate_yoy_pp,

    -- Smoothed level; monthly arrears counts are small enough to be jumpy.
    AVG(f.arrears_rate_pct) OVER w3 AS arrears_rate_3m_avg,

    r.conventional_5yr_rate,
    -- The 5-year rate 18 months ago: roughly when a renewing borrower's
    -- payment shock was set.
    LAG(r.conventional_5yr_rate, 18) OVER (ORDER BY d.date_key) AS rate_lag_18m
FROM dw.fact_mortgage_arrears f
JOIN dw.dim_date           d  ON d.date_key           = f.date_key
JOIN dw.dim_arrears_region ar ON ar.arrears_region_key = f.arrears_region_key
LEFT JOIN dw.fact_rate_environment r ON r.date_key = f.date_key
WINDOW
    wreg AS (PARTITION BY f.arrears_region_key ORDER BY d.date_key),
    w3   AS (PARTITION BY f.arrears_region_key ORDER BY d.date_key
             ROWS BETWEEN 2 PRECEDING AND CURRENT ROW);


/* ---------------------------------------------------------------------------
   vw_what_changed - contribution analysis for the headline question.

   Ranks places by how much they CONTRIBUTED to the national year-over-year
   change, not by their own growth rate. A 60% jump in Charlottetown and a 4%
   jump in Toronto are not equally interesting, and a percentage-change ranking
   always surfaces the smallest places. contribution_share answers "who actually
   moved the national number?"
   --------------------------------------------------------------------------- */
CREATE OR REPLACE VIEW dw.vw_what_changed AS
WITH monthly AS (
    SELECT
        f.date_key,
        f.geography_key,
        f.coverage_key,
        SUM(f.units) AS units
    FROM dw.fact_housing_activity f
    JOIN dw.dim_construction_stage s  ON s.stage_key          = f.stage_key
    JOIN dw.dim_dwelling_type      dt ON dt.dwelling_type_key = f.dwelling_type_key
    JOIN dw.dim_geography          g  ON g.geography_key      = f.geography_key
    WHERE s.stage_short = 'Starts'
      AND dt.is_total
      AND g.is_aggregate = FALSE
    GROUP BY 1, 2, 3
),
with_lag AS (
    SELECT m.*,
           LAG(m.units, 12) OVER (PARTITION BY m.geography_key, m.coverage_key
                                  ORDER BY m.date_key) AS units_ly
    FROM monthly m
),
delta AS (
    SELECT w.*, w.units - w.units_ly AS yoy_change
    FROM with_lag w
    WHERE w.units_ly IS NOT NULL
)
SELECT
    d.date_key,
    d.year_month,
    d.year,
    g.geo_name,
    g.geo_level,
    g.province_code,
    cv.coverage_name,
    dl.units,
    dl.units_ly,
    dl.yoy_change,
    CASE WHEN dl.units_ly > 0 THEN 100.0 * dl.yoy_change / dl.units_ly END AS yoy_pct,

    -- Share of the total national movement in the same month and coverage,
    -- using absolute movement as the denominator so offsetting swings do not
    -- produce a tiny base and nonsense shares.
    SUM(dl.yoy_change) OVER wnat      AS national_yoy_change,
    CASE WHEN SUM(abs(dl.yoy_change)) OVER wnat > 0
         THEN 100.0 * dl.yoy_change / SUM(abs(dl.yoy_change)) OVER wnat
    END AS contribution_share_pct,

    RANK() OVER (PARTITION BY dl.date_key, dl.coverage_key, g.geo_level
                 ORDER BY abs(dl.yoy_change) DESC) AS movement_rank
FROM delta dl
JOIN dw.dim_date      d  ON d.date_key      = dl.date_key
JOIN dw.dim_geography g  ON g.geography_key = dl.geography_key
JOIN dw.dim_coverage  cv ON cv.coverage_key = dl.coverage_key
WINDOW wnat AS (PARTITION BY dl.date_key, dl.coverage_key, g.geo_level);


/* ---------------------------------------------------------------------------
   ops.vw_reconciliation_summary - feeds the dashboard's Data Quality page.

   Publishing the quality record next to the numbers is the point: a user who
   can see that 25,000+ comparisons were run and which four are known publisher
   errors will trust the figures far more than one who is simply asked to.
   --------------------------------------------------------------------------- */
CREATE OR REPLACE VIEW ops.vw_reconciliation_summary AS
SELECT
    r.run_id,
    e.run_started_utc,
    r.check_name,
    count(*)                                        AS comparisons,
    SUM(CASE WHEN r.passed THEN 1 ELSE 0 END)       AS passed,
    SUM(CASE WHEN r.passed THEN 0 ELSE 1 END)       AS breaches,
    ROUND(100.0 * SUM(CASE WHEN r.passed THEN 1 ELSE 0 END) / count(*), 4)
                                                    AS pass_rate_pct,
    MAX(r.variance_pct)                             AS max_variance,
    MAX(r.threshold_pct)                            AS threshold
FROM ops.reconciliation_result r
JOIN ops.etl_run e ON e.run_id = r.run_id
GROUP BY 1, 2, 3;


/* ---------------------------------------------------------------------------
   dw.vw_data_dictionary - the model describing itself.

   Surfaced on a dashboard page so a business user can answer "what is this
   column and where did it come from?" without opening a document that will
   inevitably go stale.
   --------------------------------------------------------------------------- */
CREATE OR REPLACE VIEW dw.vw_data_dictionary AS
SELECT
    t.table_schema,
    t.table_name,
    c.column_name,
    c.data_type,
    c.is_nullable,
    CASE
        WHEN t.table_name LIKE 'fact_%' THEN 'Fact'
        WHEN t.table_name LIKE 'dim_%'  THEN 'Dimension'
        ELSE 'Other'
    END AS table_role
FROM information_schema.tables t
JOIN information_schema.columns c
  ON c.table_schema = t.table_schema AND c.table_name = t.table_name
WHERE t.table_schema IN ('dw', 'ops');
