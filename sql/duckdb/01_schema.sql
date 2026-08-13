/* =============================================================================
   DuckDB deployment of the same star schema defined in sql/azure/.

   Why two dialects rather than one portable script:

     * The Azure scripts carry the things that only matter in a server engine -
       CLUSTERED COLUMNSTORE, IDENTITY, filegroup-free PK/FK layout, NVARCHAR.
     * DuckDB is already a columnar engine, so the storage hints are noise here;
       it needs sequences instead of IDENTITY and BOOLEAN instead of BIT.

   What is deliberately IDENTICAL between the two: table names, column names,
   column order, grain, nullability and the semantics of every flag. That is what
   makes the Power BI model portable - repointing it from the local DuckDB export
   to Azure SQL is a connection-string change, not a remodel.

   Idempotent - safe to re-run.
   ============================================================================= */

CREATE SCHEMA IF NOT EXISTS stg;
CREATE SCHEMA IF NOT EXISTS dw;
CREATE SCHEMA IF NOT EXISTS ops;

/* ============================ DIMENSIONS =================================== */

CREATE TABLE IF NOT EXISTS dw.dim_date (
    date_key          INTEGER   PRIMARY KEY,   -- YYYYMM01
    date              DATE      NOT NULL,
    year_month        VARCHAR   NOT NULL,      -- '2026-05'
    year              SMALLINT  NOT NULL,
    quarter           TINYINT   NOT NULL,
    quarter_name      VARCHAR   NOT NULL,      -- '2026 Q2'
    month             TINYINT   NOT NULL,
    month_name        VARCHAR   NOT NULL,
    month_abbr        VARCHAR   NOT NULL,
    month_end_date    DATE      NOT NULL,
    days_in_month     TINYINT   NOT NULL,
    fiscal_year       SMALLINT  NOT NULL,      -- Canadian federal FY: Apr-Mar
    fiscal_quarter    TINYINT   NOT NULL,
    is_current_month  BOOLEAN   NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS dw.dim_geography (
    geography_key  INTEGER  PRIMARY KEY,
    geo_name       VARCHAR  NOT NULL,
    geo_level      VARCHAR  NOT NULL,   -- Country|Province|CMA|Region|CBA Region|Unknown
    dguid          VARCHAR,
    province_code  VARCHAR,
    province_name  VARCHAR,
    cba_region     VARCHAR,             -- roll-up target for the arrears fact
    is_aggregate   BOOLEAN  NOT NULL DEFAULT FALSE,
    sort_order     SMALLINT NOT NULL DEFAULT 999,
    UNIQUE (geo_name, geo_level)
);

-- Arrears are published for 8 CBA regions, a DIFFERENT grain from the housing
-- geography. An earlier version stored these as rows inside dim_geography,
-- which put two members named 'Ontario' in one dimension - a province and a
-- CBA region - and would have shown a business user two identical entries in a
-- single geography slicer. Different grain, different dimension.
-- dim_geography.cba_region remains the bridge attribute for cross-filtering.
CREATE TABLE IF NOT EXISTS dw.dim_arrears_region (
    arrears_region_key INTEGER PRIMARY KEY,
    region_name        VARCHAR NOT NULL UNIQUE,   -- 'Ontario', 'Atlantic', ...
    region_code        VARCHAR NOT NULL,          -- CBA's own label, uppercase
    covers_provinces   VARCHAR,                   -- 'NL, PE, NS, NB'
    is_national        BOOLEAN NOT NULL DEFAULT FALSE,
    sort_order         SMALLINT NOT NULL DEFAULT 99
);

CREATE TABLE IF NOT EXISTS dw.dim_coverage (
    coverage_key      INTEGER PRIMARY KEY,
    coverage_name     VARCHAR NOT NULL UNIQUE,
    coverage_desc     VARCHAR,
    is_seasonally_adj BOOLEAN NOT NULL DEFAULT FALSE,
    is_annualised     BOOLEAN NOT NULL DEFAULT FALSE,
    is_default        BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS dw.dim_dwelling_type (
    dwelling_type_key  INTEGER  PRIMARY KEY,
    dwelling_type_name VARCHAR  NOT NULL UNIQUE,
    dwelling_category  VARCHAR  NOT NULL,
    is_total           BOOLEAN  NOT NULL DEFAULT FALSE,
    sort_order         SMALLINT NOT NULL DEFAULT 99
);

CREATE TABLE IF NOT EXISTS dw.dim_construction_stage (
    stage_key   INTEGER PRIMARY KEY,
    stage_name  VARCHAR NOT NULL UNIQUE,
    stage_short VARCHAR NOT NULL,
    stage_order TINYINT NOT NULL,
    is_flow     BOOLEAN NOT NULL   -- flow (starts, completions) vs stock (under construction)
);

-- Keyed on (source_alias, member_id), NOT on component_name.
-- StatCan flattens hierarchical dimensions to the leaf label, so 'Non-banks'
-- appears six times in 36-10-0639 under different parents. The label is not a
-- key; the COORDINATE member ID is. See src/transform/statcan_metadata.py.
CREATE TABLE IF NOT EXISTS dw.dim_credit_product (
    credit_product_key INTEGER PRIMARY KEY,
    source_alias       VARCHAR NOT NULL,
    member_id          INTEGER NOT NULL,
    component_name     VARCHAR NOT NULL,   -- leaf label: deliberately NOT unique
    hierarchy_path     VARCHAR,            -- 'Mortgage loans > Residential mortgages > Non-banks'
    parent_name        VARCHAR,
    root_name          VARCHAR,
    depth              TINYINT,
    is_leaf            BOOLEAN NOT NULL DEFAULT TRUE,  -- FALSE = publisher subtotal, exclude from sums
    product_family     VARCHAR,
    rate_type          VARCHAR,
    insurance_status   VARCHAR,
    term_band          VARCHAR,
    lending_stage      VARCHAR,
    lender_type        VARCHAR,
    UNIQUE (source_alias, member_id)
);

CREATE TABLE IF NOT EXISTS dw.dim_price_component (
    price_component_key INTEGER PRIMARY KEY,
    component_name      VARCHAR NOT NULL UNIQUE,
    sort_order          TINYINT NOT NULL DEFAULT 9
);

CREATE TABLE IF NOT EXISTS dw.dim_source (
    source_key   INTEGER PRIMARY KEY,
    source_alias VARCHAR NOT NULL UNIQUE,
    source_table VARCHAR,
    publisher    VARCHAR,
    source_url   VARCHAR,
    licence      VARCHAR,
    sha256       VARCHAR,
    extracted_utc TIMESTAMP
);

/* ============================== FACTS ====================================== */

CREATE TABLE IF NOT EXISTS dw.fact_housing_activity (
    date_key          INTEGER NOT NULL,
    geography_key     INTEGER NOT NULL,
    dwelling_type_key INTEGER NOT NULL,
    stage_key         INTEGER NOT NULL,
    coverage_key      INTEGER NOT NULL,
    source_key        INTEGER NOT NULL,
    units             DECIMAL(18,2),   -- NULL = suppressed, semantically distinct from 0
    is_estimated      BOOLEAN NOT NULL DEFAULT FALSE
);

CREATE TABLE IF NOT EXISTS dw.fact_market_absorption (
    date_key             INTEGER NOT NULL,
    geography_key        INTEGER NOT NULL,
    dwelling_type_key    INTEGER NOT NULL,
    source_key           INTEGER NOT NULL,
    absorptions          DECIMAL(18,2),
    unabsorbed_inventory DECIMAL(18,2),
    unoccupied_units     DECIMAL(18,2)
);

CREATE TABLE IF NOT EXISTS dw.fact_mortgage_arrears (
    date_key             INTEGER NOT NULL,
    arrears_region_key   INTEGER NOT NULL,
    source_key           INTEGER NOT NULL,
    total_mortgages      BIGINT,
    mortgages_in_arrears BIGINT,          -- NULL where CBA suppresses small counts
    arrears_rate_pct     DECIMAL(9,4),
    is_suppressed        BOOLEAN NOT NULL DEFAULT FALSE,
    PRIMARY KEY (date_key, arrears_region_key)
);

CREATE TABLE IF NOT EXISTS dw.fact_mortgage_originations (
    date_key            INTEGER NOT NULL,
    credit_product_key  INTEGER NOT NULL,
    source_key          INTEGER NOT NULL,
    funds_advanced      DECIMAL(20,2),   -- flow: additive over time
    outstanding_balance DECIMAL(20,2),   -- stock: semi-additive, period-end only
    effective_rate      DECIMAL(9,4),    -- weighted average: never additive
    PRIMARY KEY (date_key, credit_product_key)
);

CREATE TABLE IF NOT EXISTS dw.fact_price_index (
    date_key            INTEGER NOT NULL,
    geography_key       INTEGER NOT NULL,
    price_component_key INTEGER NOT NULL,
    source_key          INTEGER NOT NULL,
    index_value         DECIMAL(12,4),   -- non-additive across geography or time
    PRIMARY KEY (date_key, geography_key, price_component_key)
);

CREATE TABLE IF NOT EXISTS dw.fact_household_credit (
    date_key           INTEGER NOT NULL,
    credit_product_key INTEGER NOT NULL,
    seasonality        VARCHAR NOT NULL,
    source_key         INTEGER NOT NULL,
    balance_dollars    DECIMAL(20,2),
    PRIMARY KEY (date_key, credit_product_key, seasonality)
);

CREATE TABLE IF NOT EXISTS dw.fact_rate_environment (
    date_key              INTEGER PRIMARY KEY,
    source_key            INTEGER NOT NULL,
    conventional_5yr_rate DECIMAL(9,4)
);

/* ============================ RUN CONTROL ================================== */

CREATE TABLE IF NOT EXISTS ops.etl_run (
    run_id          BIGINT PRIMARY KEY,
    run_started_utc TIMESTAMP NOT NULL,
    run_ended_utc   TIMESTAMP,
    status          VARCHAR   NOT NULL DEFAULT 'RUNNING',
    triggered_by    VARCHAR,
    rows_loaded     BIGINT,
    notes           VARCHAR
);

CREATE TABLE IF NOT EXISTS ops.reconciliation_result (
    result_id       BIGINT PRIMARY KEY,
    run_id          BIGINT  NOT NULL,
    check_name      VARCHAR NOT NULL,
    check_grain     VARCHAR,
    warehouse_value DECIMAL(20,4),
    control_value   DECIMAL(20,4),
    variance_abs    DECIMAL(20,4),
    variance_pct    DECIMAL(12,6),
    threshold_pct   DECIMAL(12,6) NOT NULL DEFAULT 1.0,
    passed          BOOLEAN NOT NULL,
    checked_utc     TIMESTAMP NOT NULL
);
