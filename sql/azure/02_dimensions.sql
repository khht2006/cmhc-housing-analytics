/* =============================================================================
   02: conformed dimensions
   -----------------------------------------------------------------------------
   Conventions used throughout:

   * Integer surrogate keys everywhere. Publisher label text is unstable across
     vintages, so facts never bind to a natural key.
   * Every dimension carries an explicit key = -1 'Unknown' member, inserted
     with IDENTITY_INSERT. The loader maps unresolvable fact rows there instead
     of dropping them, so row counts stay conserved and the quality suite can
     see the problem.
   * Dimensions are CLUSTERED on the surrogate key and small enough to be
     rowstore; only the facts get columnstore.
   ============================================================================= */

/* ---------------------------------------------------------------------------
   dim_date - month grain.
   Smart key (20260501) rather than a meaningless identity: date keys are the
   one place a readable surrogate pays for itself, and the value is immutable.
   --------------------------------------------------------------------------- */
IF OBJECT_ID('dw.dim_date') IS NULL
CREATE TABLE dw.dim_date
(
    date_key           INT          NOT NULL,   -- YYYYMM01
    [date]             DATE         NOT NULL,
    year_month         CHAR(7)      NOT NULL,   -- '2026-05', matches StatCan REF_DATE
    [year]             SMALLINT     NOT NULL,
    [quarter]          TINYINT      NOT NULL,
    quarter_name       CHAR(7)      NOT NULL,   -- '2026 Q2'
    [month]            TINYINT      NOT NULL,
    month_name         VARCHAR(12)  NOT NULL,
    month_abbr         CHAR(3)      NOT NULL,
    month_end_date     DATE         NOT NULL,
    days_in_month      TINYINT      NOT NULL,
    -- Canadian federal fiscal year runs 1 April - 31 March.
    fiscal_year        SMALLINT     NOT NULL,
    fiscal_quarter     TINYINT      NOT NULL,
    is_current_month   BIT          NOT NULL CONSTRAINT DF_dim_date_curr DEFAULT 0,
    CONSTRAINT PK_dim_date PRIMARY KEY CLUSTERED (date_key),
    CONSTRAINT UQ_dim_date_ym UNIQUE (year_month)
);
GO

/* ---------------------------------------------------------------------------
   dim_geography - the workhorse dimension.

   geo_level distinguishes the incompatible universes described in
   docs/star-schema.md; is_aggregate marks publisher roll-up rows that must be
   excluded from leaf sums; cba_region is the roll-up attribute that lets the
   coarse-grained arrears fact coexist with fine-grained housing facts.
   --------------------------------------------------------------------------- */
IF OBJECT_ID('dw.dim_geography') IS NULL
CREATE TABLE dw.dim_geography
(
    geography_key      INT           IDENTITY(1,1) NOT NULL,
    geo_name           NVARCHAR(160) NOT NULL,   -- exactly as published
    geo_level          VARCHAR(20)   NOT NULL,   -- Country|Province|CMA|Region|CBA Region
    dguid              VARCHAR(30)   NULL,       -- StatCan standard geo identifier
    province_code      CHAR(2)       NULL,       -- AB, BC, ON ...
    province_name      NVARCHAR(60)  NULL,
    cba_region         VARCHAR(30)   NULL,       -- roll-up target for arrears
    is_aggregate       BIT           NOT NULL CONSTRAINT DF_geo_agg DEFAULT 0,
    sort_order         SMALLINT      NOT NULL CONSTRAINT DF_geo_sort DEFAULT 999,
    CONSTRAINT PK_dim_geography PRIMARY KEY CLUSTERED (geography_key),
    CONSTRAINT UQ_dim_geography UNIQUE (geo_name, geo_level),
    CONSTRAINT CK_geo_level CHECK (geo_level IN
        ('Country','Province','CMA','Region','CBA Region','Unknown'))
);
GO

CREATE NONCLUSTERED INDEX IX_geo_level_agg
    ON dw.dim_geography (geo_level, is_aggregate)
    INCLUDE (geo_name, province_code, cba_region);
GO

/* ---------------------------------------------------------------------------
   dim_arrears_region - the CBA delinquency grain.

   Arrears are published for 8 CBA regions, which is NOT the housing geography.
   An earlier version of this model stored them as rows inside dim_geography;
   that put two members named 'Ontario' in one dimension (a province and a CBA
   region) and would have shown a business user two identical entries in a
   single geography slicer. Different grain, different dimension.
   dim_geography.cba_region remains the bridge attribute for cross-filtering.
   --------------------------------------------------------------------------- */
IF OBJECT_ID('dw.dim_arrears_region') IS NULL
CREATE TABLE dw.dim_arrears_region
(
    arrears_region_key INT           IDENTITY(1,1) NOT NULL,
    region_name        NVARCHAR(60)  NOT NULL,   -- 'Ontario', 'Atlantic', ...
    region_code        VARCHAR(30)   NOT NULL,   -- CBA's own label, uppercase
    covers_provinces   NVARCHAR(120) NULL,       -- 'NL, PE, NS, NB'
    is_national        BIT           NOT NULL CONSTRAINT DF_arr_nat DEFAULT 0,
    sort_order         SMALLINT      NOT NULL CONSTRAINT DF_arr_sort DEFAULT 99,
    CONSTRAINT PK_dim_arrears_region PRIMARY KEY CLUSTERED (arrears_region_key),
    CONSTRAINT UQ_dim_arrears_region UNIQUE (region_name)
);
GO

/* ---------------------------------------------------------------------------
   dim_coverage - which statistical universe a housing figure belongs to.
   Part of fact_housing_activity's grain. Without it, summing across sources
   triple-counts (see docs/star-schema.md #1).
   --------------------------------------------------------------------------- */
IF OBJECT_ID('dw.dim_coverage') IS NULL
CREATE TABLE dw.dim_coverage
(
    coverage_key       INT           IDENTITY(1,1) NOT NULL,
    coverage_name      NVARCHAR(80)  NOT NULL,
    coverage_desc      NVARCHAR(400) NULL,
    is_seasonally_adj  BIT           NOT NULL CONSTRAINT DF_cov_sa DEFAULT 0,
    is_annualised      BIT           NOT NULL CONSTRAINT DF_cov_ann DEFAULT 0,
    is_default         BIT           NOT NULL CONSTRAINT DF_cov_def DEFAULT 0,
    CONSTRAINT PK_dim_coverage PRIMARY KEY CLUSTERED (coverage_key),
    CONSTRAINT UQ_dim_coverage UNIQUE (coverage_name)
);
GO

/* ---------------------------------------------------------------------------
   dim_dwelling_type - single/semi/row/apartment plus publisher roll-ups.
   dwelling_category collapses the sibling label variants that appear across
   CMHC tables ('Apartment and other units' vs 'Apartment and other unit types')
   into one analysable grouping.
   --------------------------------------------------------------------------- */
IF OBJECT_ID('dw.dim_dwelling_type') IS NULL
CREATE TABLE dw.dim_dwelling_type
(
    dwelling_type_key  INT           IDENTITY(1,1) NOT NULL,
    dwelling_type_name NVARCHAR(80)  NOT NULL,   -- as published
    dwelling_category  NVARCHAR(40)  NOT NULL,   -- Single|Semi-detached|Row|Apartment|Multiples|Total
    is_total           BIT           NOT NULL CONSTRAINT DF_dwell_total DEFAULT 0,
    sort_order         SMALLINT      NOT NULL CONSTRAINT DF_dwell_sort DEFAULT 99,
    CONSTRAINT PK_dim_dwelling_type PRIMARY KEY CLUSTERED (dwelling_type_key),
    CONSTRAINT UQ_dim_dwelling_type UNIQUE (dwelling_type_name)
);
GO

/* ---------------------------------------------------------------------------
   dim_construction_stage - starts -> under construction -> completions.
   stage_order encodes the physical pipeline, which is what makes the
   "where are the bottlenecks?" analysis possible: a widening gap between
   stage 1 and stage 3 is a stalled pipeline.
   --------------------------------------------------------------------------- */
IF OBJECT_ID('dw.dim_construction_stage') IS NULL
CREATE TABLE dw.dim_construction_stage
(
    stage_key          INT           IDENTITY(1,1) NOT NULL,
    stage_name         NVARCHAR(60)  NOT NULL,
    stage_short        NVARCHAR(20)  NOT NULL,   -- Starts | Under construction | Completions
    stage_order        TINYINT       NOT NULL,
    is_flow            BIT           NOT NULL,   -- flow (starts/completions) vs stock (under construction)
    CONSTRAINT PK_dim_construction_stage PRIMARY KEY CLUSTERED (stage_key),
    CONSTRAINT UQ_dim_construction_stage UNIQUE (stage_name)
);
GO

/* ---------------------------------------------------------------------------
   dim_credit_product - decodes the Bank of Canada 'Components' strings
   ('Fixed rate, funds advanced, residential mortgages, insured, 5 years and
   over') into orthogonal attributes so they can be sliced independently.

   Keyed on (source_alias, member_id), NOT on component_name. StatCan flattens
   hierarchical dimensions down to the leaf label, so 'Non-banks' occurs six
   times in 36-10-0639 under six different parents. The label is not a key; the
   COORDINATE member ID is. is_leaf marks publisher subtotals that must be
   excluded from sums - the BoC table stores 'Total, funds advanced, ...'
   alongside its own children.
   --------------------------------------------------------------------------- */
IF OBJECT_ID('dw.dim_credit_product') IS NULL
CREATE TABLE dw.dim_credit_product
(
    credit_product_key INT           IDENTITY(1,1) NOT NULL,
    source_alias       VARCHAR(60)   NOT NULL,
    member_id          INT           NOT NULL,
    component_name     NVARCHAR(300) NOT NULL,   -- leaf label, deliberately NOT unique
    hierarchy_path     NVARCHAR(1000) NULL,
    parent_name        NVARCHAR(300) NULL,
    root_name          NVARCHAR(300) NULL,
    depth              TINYINT       NULL,
    is_leaf            BIT           NOT NULL CONSTRAINT DF_cp_leaf DEFAULT 1,
    product_family     NVARCHAR(60)  NULL,       -- Residential mortgage | Personal credit | ...
    rate_type          NVARCHAR(30)  NULL,       -- Fixed | Variable | Total
    insurance_status   NVARCHAR(30)  NULL,       -- Insured | Uninsured | Total
    term_band          NVARCHAR(40)  NULL,       -- <1 yr | 1-3 yr | 3-5 yr | 5 yr+ | Total
    lending_stage      NVARCHAR(30)  NULL,       -- Funds advanced | Outstanding balance
    lender_type        NVARCHAR(60)  NULL,       -- Chartered banks | Credit unions | ...
    CONSTRAINT PK_dim_credit_product PRIMARY KEY CLUSTERED (credit_product_key),
    CONSTRAINT UQ_dim_credit_product UNIQUE (source_alias, member_id)
);
GO

/* ---------------------------------------------------------------------------
   dim_price_component - New Housing Price Index breakdown.
   --------------------------------------------------------------------------- */
IF OBJECT_ID('dw.dim_price_component') IS NULL
CREATE TABLE dw.dim_price_component
(
    price_component_key INT          IDENTITY(1,1) NOT NULL,
    component_name      NVARCHAR(60) NOT NULL,   -- House only | Land only | Total (house and land)
    sort_order          TINYINT      NOT NULL CONSTRAINT DF_price_sort DEFAULT 9,
    CONSTRAINT PK_dim_price_component PRIMARY KEY CLUSTERED (price_component_key),
    CONSTRAINT UQ_dim_price_component UNIQUE (component_name)
);
GO

/* ---------------------------------------------------------------------------
   dim_source - lineage as a first-class dimension.
   Every fact row carries source_key, so any figure on the dashboard can be
   traced to a table number, a URL and a file hash without leaving Power BI.
   --------------------------------------------------------------------------- */
IF OBJECT_ID('dw.dim_source') IS NULL
CREATE TABLE dw.dim_source
(
    source_key       INT           IDENTITY(1,1) NOT NULL,
    source_alias     VARCHAR(60)   NOT NULL,
    source_table     VARCHAR(30)   NULL,
    publisher        NVARCHAR(100) NULL,
    source_url       NVARCHAR(500) NULL,
    licence          NVARCHAR(120) NULL,
    CONSTRAINT PK_dim_source PRIMARY KEY CLUSTERED (source_key),
    CONSTRAINT UQ_dim_source UNIQUE (source_alias)
);
GO

/* =============================================================================
   Unknown members. Inserted at key -1 in every dimension.
   ============================================================================= */
SET IDENTITY_INSERT dw.dim_geography ON;
IF NOT EXISTS (SELECT 1 FROM dw.dim_geography WHERE geography_key = -1)
    INSERT dw.dim_geography (geography_key, geo_name, geo_level, is_aggregate, sort_order)
    VALUES (-1, N'Unknown', 'Unknown', 0, 9999);
SET IDENTITY_INSERT dw.dim_geography OFF;
GO

SET IDENTITY_INSERT dw.dim_arrears_region ON;
IF NOT EXISTS (SELECT 1 FROM dw.dim_arrears_region WHERE arrears_region_key = -1)
    INSERT dw.dim_arrears_region (arrears_region_key, region_name, region_code, sort_order)
    VALUES (-1, N'Unknown', 'UNKNOWN', 999);
SET IDENTITY_INSERT dw.dim_arrears_region OFF;
GO

SET IDENTITY_INSERT dw.dim_coverage ON;
IF NOT EXISTS (SELECT 1 FROM dw.dim_coverage WHERE coverage_key = -1)
    INSERT dw.dim_coverage (coverage_key, coverage_name, coverage_desc)
    VALUES (-1, N'Unknown', N'Unresolved coverage');
SET IDENTITY_INSERT dw.dim_coverage OFF;
GO

SET IDENTITY_INSERT dw.dim_dwelling_type ON;
IF NOT EXISTS (SELECT 1 FROM dw.dim_dwelling_type WHERE dwelling_type_key = -1)
    INSERT dw.dim_dwelling_type (dwelling_type_key, dwelling_type_name, dwelling_category, sort_order)
    VALUES (-1, N'Unknown', N'Unknown', 999);
SET IDENTITY_INSERT dw.dim_dwelling_type OFF;
GO

SET IDENTITY_INSERT dw.dim_construction_stage ON;
IF NOT EXISTS (SELECT 1 FROM dw.dim_construction_stage WHERE stage_key = -1)
    INSERT dw.dim_construction_stage (stage_key, stage_name, stage_short, stage_order, is_flow)
    VALUES (-1, N'Unknown', N'Unknown', 9, 0);
SET IDENTITY_INSERT dw.dim_construction_stage OFF;
GO

SET IDENTITY_INSERT dw.dim_credit_product ON;
IF NOT EXISTS (SELECT 1 FROM dw.dim_credit_product WHERE credit_product_key = -1)
    INSERT dw.dim_credit_product (credit_product_key, source_alias, member_id, component_name, is_leaf)
    VALUES (-1, 'unknown', -1, N'Unknown', 1);
SET IDENTITY_INSERT dw.dim_credit_product OFF;
GO

SET IDENTITY_INSERT dw.dim_price_component ON;
IF NOT EXISTS (SELECT 1 FROM dw.dim_price_component WHERE price_component_key = -1)
    INSERT dw.dim_price_component (price_component_key, component_name, sort_order) VALUES (-1, N'Unknown', 99);
SET IDENTITY_INSERT dw.dim_price_component OFF;
GO

SET IDENTITY_INSERT dw.dim_source ON;
IF NOT EXISTS (SELECT 1 FROM dw.dim_source WHERE source_key = -1)
    INSERT dw.dim_source (source_key, source_alias, publisher) VALUES (-1, 'unknown', N'Unknown');
SET IDENTITY_INSERT dw.dim_source OFF;
GO
