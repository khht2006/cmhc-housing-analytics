-- The star schema.
--
-- A star schema splits data into two kinds of table:
--
--   DIMENSION tables  the things you filter and group by - dates, places,
--                     house types. Small, lots of descriptive columns.
--
--   FACT tables       the numbers you add up. Big, mostly just ID columns
--                     pointing at dimensions, plus the measures.
--
-- It's called a "star" because if you draw a fact table in the middle with its
-- dimensions around it, it looks like one. We have 6 fact tables sharing the
-- same dimensions, which means one date filter can control all of them at once.
--
-- Every dimension has a row with key = -1 meaning "Unknown". If a fact row
-- points at something we couldn't identify, we send it there instead of
-- throwing the row away, so we can count how often it happens.


-- ============================================================================
-- DIMENSIONS
-- ============================================================================

-- One row per month. Having a real date table (instead of just storing a date
-- on each fact) is what lets Power BI do "same month last year" comparisons.
CREATE TABLE IF NOT EXISTS dim_date (
    date_key      INTEGER PRIMARY KEY,  -- 20260501 for May 2026
    date          DATE    NOT NULL,
    year_month    VARCHAR NOT NULL,     -- '2026-05'
    year          INTEGER NOT NULL,
    quarter       INTEGER NOT NULL,
    month         INTEGER NOT NULL,
    month_name    VARCHAR NOT NULL,
    quarter_name  VARCHAR NOT NULL      -- '2026 Q2'
);

-- Every place name, sorted out by geography.py.
-- is_aggregate is the important column: TRUE means "this is a total of other
-- rows", so leave it out when adding things up.
CREATE TABLE IF NOT EXISTS dim_geography (
    geography_key  INTEGER PRIMARY KEY,
    geo_name       VARCHAR NOT NULL,
    geo_level      VARCHAR NOT NULL,    -- Country / Province / CMA / Region
    province_name  VARCHAR,
    province_code  VARCHAR,
    arrears_region VARCHAR,             -- links to the CBA delinquency data
    is_aggregate   BOOLEAN NOT NULL,
    sort_order     INTEGER NOT NULL
);

-- The 8 regions the Canadian Bankers Association reports arrears for.
-- These are NOT the same as provinces (Atlantic is 4 provinces in one), which
-- is why they get their own table instead of being mixed into dim_geography.
CREATE TABLE IF NOT EXISTS dim_arrears_region (
    arrears_region_key INTEGER PRIMARY KEY,
    region_name        VARCHAR NOT NULL,
    region_code        VARCHAR NOT NULL,  -- how it's spelled in the PDF
    covers_provinces   VARCHAR,           -- so the dashboard can explain itself
    is_national        BOOLEAN NOT NULL,
    sort_order         INTEGER NOT NULL
);

-- CMHC publishes housing starts for two different groups of places:
-- towns of 10,000+, and the 37 biggest metro areas. The metro areas are
-- INSIDE the towns figure, so adding both together counts the same houses
-- twice. Keeping coverage on the fact table stops that happening by accident.
CREATE TABLE IF NOT EXISTS dim_coverage (
    coverage_key  INTEGER PRIMARY KEY,
    coverage_name VARCHAR NOT NULL,
    description   VARCHAR,
    is_default    BOOLEAN NOT NULL
);

-- Single-detached, semi-detached, row houses, apartments, and totals.
CREATE TABLE IF NOT EXISTS dim_dwelling_type (
    dwelling_type_key  INTEGER PRIMARY KEY,
    dwelling_type_name VARCHAR NOT NULL,  -- exactly as CMHC writes it
    dwelling_category  VARCHAR NOT NULL,  -- tidied up for grouping
    is_total           BOOLEAN NOT NULL,
    sort_order         INTEGER NOT NULL
);

-- The three stages a house goes through. stage_order matters: it's what lets
-- us compare "how many started" against "how many finished".
CREATE TABLE IF NOT EXISTS dim_construction_stage (
    stage_key   INTEGER PRIMARY KEY,
    stage_name  VARCHAR NOT NULL,   -- 'Housing starts'
    stage_short VARCHAR NOT NULL,   -- 'Starts'
    stage_order INTEGER NOT NULL    -- 1, 2, 3
);

-- The types of mortgage lending the Bank of Canada reports.
-- is_total flags rows like "Total, funds advanced, residential mortgages,
-- insured", which are subtotals of the rows underneath them - same
-- double-counting trap as with places.
CREATE TABLE IF NOT EXISTS dim_credit_product (
    credit_product_key INTEGER PRIMARY KEY,
    product_name       VARCHAR NOT NULL,
    rate_type          VARCHAR,   -- Fixed / Variable
    insurance_status   VARCHAR,   -- Insured / Uninsured
    is_total           BOOLEAN NOT NULL
);

-- The price index is split into the house, the land, or both.
CREATE TABLE IF NOT EXISTS dim_price_component (
    price_component_key INTEGER PRIMARY KEY,
    component_name      VARCHAR NOT NULL
);


-- ============================================================================
-- FACTS
-- ============================================================================

-- The main one. One row per month + place + house type + stage + coverage.
-- "units" can be NULL, which means CMHC didn't publish a number - that's
-- different from publishing a zero, so we keep them apart.
CREATE TABLE IF NOT EXISTS fact_housing_activity (
    date_key          INTEGER NOT NULL,
    geography_key     INTEGER NOT NULL,
    dwelling_type_key INTEGER NOT NULL,
    stage_key         INTEGER NOT NULL,
    coverage_key      INTEGER NOT NULL,
    units             DECIMAL(18, 2)
);

-- Finished homes: how many were sold or rented (absorptions), and how many
-- are finished but still sitting empty (unabsorbed_inventory).
CREATE TABLE IF NOT EXISTS fact_market_absorption (
    date_key             INTEGER NOT NULL,
    geography_key        INTEGER NOT NULL,
    dwelling_type_key    INTEGER NOT NULL,
    absorptions          DECIMAL(18, 2),
    unabsorbed_inventory DECIMAL(18, 2)
);

-- Homes that are finished but nobody has moved into yet. This is a separate
-- table from the one above because CMHC publishes it for a longer list of
-- towns, so the two don't line up row for row.
CREATE TABLE IF NOT EXISTS fact_unoccupied_housing (
    date_key          INTEGER NOT NULL,
    geography_key     INTEGER NOT NULL,
    dwelling_type_key INTEGER NOT NULL,
    unoccupied_units  DECIMAL(18, 2)
);

-- Mortgages 90+ days behind on payments.
-- mortgages_in_arrears is NULL for the Territories because the CBA hides
-- small numbers for privacy. We store NULL and a flag, never 0 - a zero would
-- look like nobody there is behind on payments, which isn't what it means.
CREATE TABLE IF NOT EXISTS fact_mortgage_arrears (
    date_key             INTEGER NOT NULL,
    arrears_region_key   INTEGER NOT NULL,
    total_mortgages      BIGINT,
    mortgages_in_arrears BIGINT,
    arrears_rate         DECIMAL(9, 4),
    is_hidden            BOOLEAN NOT NULL,
    PRIMARY KEY (date_key, arrears_region_key)
);

-- New mortgage money lent out each month, and the interest rate on it.
CREATE TABLE IF NOT EXISTS fact_mortgage_originations (
    date_key           INTEGER NOT NULL,
    credit_product_key INTEGER NOT NULL,
    funds_advanced     DECIMAL(20, 2),   -- dollars lent this month
    interest_rate      DECIMAL(9, 4),    -- never add these up, only average
    PRIMARY KEY (date_key, credit_product_key)
);

CREATE TABLE IF NOT EXISTS fact_price_index (
    date_key            INTEGER NOT NULL,
    geography_key       INTEGER NOT NULL,
    price_component_key INTEGER NOT NULL,
    index_value         DECIMAL(12, 4),
    PRIMARY KEY (date_key, geography_key, price_component_key)
);

CREATE TABLE IF NOT EXISTS fact_mortgage_rate (
    date_key   INTEGER PRIMARY KEY,
    rate_5year DECIMAL(9, 4)
);


-- ============================================================================
-- A place to record the results of our number checks (see check.py)
-- ============================================================================
CREATE TABLE IF NOT EXISTS check_results (
    check_name      VARCHAR,
    detail          VARCHAR,
    our_value       DECIMAL(20, 4),
    published_value DECIMAL(20, 4),
    difference      DECIMAL(20, 4),
    passed          BOOLEAN,
    checked_at      TIMESTAMP
)
