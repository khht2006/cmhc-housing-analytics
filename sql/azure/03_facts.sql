/* =============================================================================
   03: fact tables
   -----------------------------------------------------------------------------
   Indexing strategy
   -----------------
   The large facts get a CLUSTERED COLUMNSTORE INDEX. This is the right call for
   this workload and worth stating explicitly:

     * Access pattern is analytical - Power BI issues wide scans with aggregates
       and few point lookups. Columnstore reads only the referenced columns.
     * The facts are narrow integer keys plus a numeric measure, which compresses
       roughly 8-10x. fact_housing_activity drops from ~40 MB rowstore to ~5 MB.
     * The load pattern is truncate-and-reload monthly, not trickle insert, so
       the usual columnstore delta-store fragmentation problem does not arise.

   The small facts (arrears, rate environment) stay rowstore - a columnstore
   rowgroup needs ~102,400 rows before it compresses well, and these have
   thousands. Using columnstore there would be cargo-culting.

   Foreign keys are declared and TRUSTED. In a warehouse people often skip them
   for load speed; here they are cheap (the loader inserts pre-resolved keys) and
   they let the optimiser eliminate joins Power BI does not actually need.
   ============================================================================= */

/* ---------------------------------------------------------------------------
   fact_housing_activity - the core fact. Starts / under construction /
   completions by place, dwelling type and statistical universe.

   GRAIN: one row per month x geography x dwelling type x construction stage
          x coverage.
   --------------------------------------------------------------------------- */
IF OBJECT_ID('dw.fact_housing_activity') IS NULL
CREATE TABLE dw.fact_housing_activity
(
    date_key          INT      NOT NULL,
    geography_key     INT      NOT NULL,
    dwelling_type_key INT      NOT NULL,
    stage_key         INT      NOT NULL,
    coverage_key      INT      NOT NULL,
    source_key        INT      NOT NULL,
    units             DECIMAL(18,2) NULL,   -- NULL = suppressed/not available, distinct from 0
    is_estimated      BIT      NOT NULL CONSTRAINT DF_fha_est DEFAULT 0,
    CONSTRAINT FK_fha_date     FOREIGN KEY (date_key)          REFERENCES dw.dim_date(date_key),
    CONSTRAINT FK_fha_geo      FOREIGN KEY (geography_key)     REFERENCES dw.dim_geography(geography_key),
    CONSTRAINT FK_fha_dwelling FOREIGN KEY (dwelling_type_key) REFERENCES dw.dim_dwelling_type(dwelling_type_key),
    CONSTRAINT FK_fha_stage    FOREIGN KEY (stage_key)         REFERENCES dw.dim_construction_stage(stage_key),
    CONSTRAINT FK_fha_coverage FOREIGN KEY (coverage_key)      REFERENCES dw.dim_coverage(coverage_key),
    CONSTRAINT FK_fha_source   FOREIGN KEY (source_key)        REFERENCES dw.dim_source(source_key)
);
GO

CREATE CLUSTERED COLUMNSTORE INDEX CCI_fact_housing_activity
    ON dw.fact_housing_activity;
GO

/* A supporting rowstore index for the date-range seeks Power BI issues when a
   report page is filtered to a narrow window. Columnstore alone scans all
   rowgroups; this lets segment elimination do its job. */
CREATE NONCLUSTERED INDEX IX_fha_date_geo
    ON dw.fact_housing_activity (date_key, geography_key);
GO

/* ---------------------------------------------------------------------------
   fact_market_absorption - the bottleneck fact.

   Absorptions = completed units sold/rented. Unabsorbed inventory = completed
   but still empty. Unoccupied = newly completed and unoccupied. Together these
   answer "supply was built - did it clear?"

   GRAIN: one row per month x geography x dwelling type.
   --------------------------------------------------------------------------- */
IF OBJECT_ID('dw.fact_market_absorption') IS NULL
CREATE TABLE dw.fact_market_absorption
(
    date_key             INT      NOT NULL,
    geography_key        INT      NOT NULL,
    dwelling_type_key    INT      NOT NULL,
    source_key           INT      NOT NULL,
    absorptions          DECIMAL(18,2) NULL,
    unabsorbed_inventory DECIMAL(18,2) NULL,
    unoccupied_units     DECIMAL(18,2) NULL,
    CONSTRAINT FK_fma_date     FOREIGN KEY (date_key)          REFERENCES dw.dim_date(date_key),
    CONSTRAINT FK_fma_geo      FOREIGN KEY (geography_key)     REFERENCES dw.dim_geography(geography_key),
    CONSTRAINT FK_fma_dwelling FOREIGN KEY (dwelling_type_key) REFERENCES dw.dim_dwelling_type(dwelling_type_key),
    CONSTRAINT FK_fma_source   FOREIGN KEY (source_key)        REFERENCES dw.dim_source(source_key)
);
GO

CREATE CLUSTERED COLUMNSTORE INDEX CCI_fact_market_absorption
    ON dw.fact_market_absorption;
GO

/* ---------------------------------------------------------------------------
   fact_mortgage_arrears - the delinquency fact.

   GRAIN: one row per month x CBA region.

   arrears_rate_pct is stored as published rather than derived, because CBA
   rounds it to 2 decimals and a recomputed ratio would not tie to their
   printed figure. Both are kept: the derived ratio is available in the view
   layer for precision, the published one for reconciliation.
   --------------------------------------------------------------------------- */
IF OBJECT_ID('dw.fact_mortgage_arrears') IS NULL
CREATE TABLE dw.fact_mortgage_arrears
(
    date_key             INT      NOT NULL,
    arrears_region_key   INT      NOT NULL,
    source_key           INT      NOT NULL,
    total_mortgages      BIGINT   NULL,
    mortgages_in_arrears BIGINT   NULL,   -- NULL where CBA suppresses small counts
    arrears_rate_pct     DECIMAL(9,4) NULL,
    is_suppressed        BIT      NOT NULL CONSTRAINT DF_fmar_supp DEFAULT 0,
    CONSTRAINT PK_fact_mortgage_arrears PRIMARY KEY CLUSTERED (date_key, arrears_region_key),
    CONSTRAINT FK_fmar_date   FOREIGN KEY (date_key)           REFERENCES dw.dim_date(date_key),
    CONSTRAINT FK_fmar_region FOREIGN KEY (arrears_region_key) REFERENCES dw.dim_arrears_region(arrears_region_key),
    CONSTRAINT FK_fmar_source FOREIGN KEY (source_key)         REFERENCES dw.dim_source(source_key)
);
GO

/* ---------------------------------------------------------------------------
   fact_mortgage_originations - Bank of Canada funds advanced and balances.

   GRAIN: one row per month x credit product.

   Note the mixed additivity: funds_advanced is a flow (sums over time),
   outstanding_balance is a stock (must be averaged or taken at period end),
   effective_rate is a weighted average (never summed). The DAX layer enforces
   this; see powerbi/measures.dax.
   --------------------------------------------------------------------------- */
IF OBJECT_ID('dw.fact_mortgage_originations') IS NULL
CREATE TABLE dw.fact_mortgage_originations
(
    date_key           INT      NOT NULL,
    credit_product_key INT      NOT NULL,
    source_key         INT      NOT NULL,
    funds_advanced     DECIMAL(20,2) NULL,   -- flow, additive
    outstanding_balance DECIMAL(20,2) NULL,  -- stock, semi-additive
    effective_rate     DECIMAL(9,4)  NULL,   -- weighted average, non-additive
    CONSTRAINT PK_fact_mortgage_originations PRIMARY KEY CLUSTERED (date_key, credit_product_key),
    CONSTRAINT FK_fmo_date    FOREIGN KEY (date_key)           REFERENCES dw.dim_date(date_key),
    CONSTRAINT FK_fmo_product FOREIGN KEY (credit_product_key) REFERENCES dw.dim_credit_product(credit_product_key),
    CONSTRAINT FK_fmo_source  FOREIGN KEY (source_key)         REFERENCES dw.dim_source(source_key)
);
GO

/* ---------------------------------------------------------------------------
   fact_price_index - New Housing Price Index (201612 = 100).
   GRAIN: one row per month x geography x price component.
   Index values are NON-ADDITIVE across geography or time.
   --------------------------------------------------------------------------- */
IF OBJECT_ID('dw.fact_price_index') IS NULL
CREATE TABLE dw.fact_price_index
(
    date_key            INT      NOT NULL,
    geography_key       INT      NOT NULL,
    price_component_key INT      NOT NULL,
    source_key          INT      NOT NULL,
    index_value         DECIMAL(12,4) NULL,
    CONSTRAINT PK_fact_price_index PRIMARY KEY CLUSTERED (date_key, geography_key, price_component_key),
    CONSTRAINT FK_fpi_date      FOREIGN KEY (date_key)            REFERENCES dw.dim_date(date_key),
    CONSTRAINT FK_fpi_geo       FOREIGN KEY (geography_key)       REFERENCES dw.dim_geography(geography_key),
    CONSTRAINT FK_fpi_component FOREIGN KEY (price_component_key) REFERENCES dw.dim_price_component(price_component_key),
    CONSTRAINT FK_fpi_source    FOREIGN KEY (source_key)          REFERENCES dw.dim_source(source_key)
);
GO

/* ---------------------------------------------------------------------------
   fact_household_credit - credit liabilities of households by lender/instrument.
   GRAIN: one row per month x credit product x seasonality.
   --------------------------------------------------------------------------- */
IF OBJECT_ID('dw.fact_household_credit') IS NULL
CREATE TABLE dw.fact_household_credit
(
    date_key           INT           NOT NULL,
    credit_product_key INT           NOT NULL,
    seasonality        VARCHAR(30)   NOT NULL,   -- Raw data | Seasonally adjusted data
    source_key         INT           NOT NULL,
    balance_dollars    DECIMAL(20,2) NULL,
    CONSTRAINT PK_fact_household_credit PRIMARY KEY CLUSTERED (date_key, credit_product_key, seasonality),
    CONSTRAINT FK_fhc_date    FOREIGN KEY (date_key)           REFERENCES dw.dim_date(date_key),
    CONSTRAINT FK_fhc_product FOREIGN KEY (credit_product_key) REFERENCES dw.dim_credit_product(credit_product_key),
    CONSTRAINT FK_fhc_source  FOREIGN KEY (source_key)         REFERENCES dw.dim_source(source_key)
);
GO

/* ---------------------------------------------------------------------------
   fact_rate_environment - one national rate series per month.
   Degenerate single-dimension fact; kept separate rather than folded into
   dim_date so that adding more rate series later does not alter the date
   dimension's grain.
   --------------------------------------------------------------------------- */
IF OBJECT_ID('dw.fact_rate_environment') IS NULL
CREATE TABLE dw.fact_rate_environment
(
    date_key              INT NOT NULL,
    source_key            INT NOT NULL,
    conventional_5yr_rate DECIMAL(9,4) NULL,
    CONSTRAINT PK_fact_rate_environment PRIMARY KEY CLUSTERED (date_key),
    CONSTRAINT FK_fre_date   FOREIGN KEY (date_key)   REFERENCES dw.dim_date(date_key),
    CONSTRAINT FK_fre_source FOREIGN KEY (source_key) REFERENCES dw.dim_source(source_key)
);
GO
