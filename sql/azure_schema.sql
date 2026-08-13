/*
    The same tables, written for Azure SQL Database.

    Why there are two versions of the schema:

    The project runs locally on DuckDB, which is a database that lives in a
    single file - no server to install, great for building and testing. But in
    a real job the warehouse would sit on a server, so this file shows the same
    design deployed to Azure SQL.

    The table and column names are IDENTICAL to sql/schema.sql on purpose. That
    means the Power BI report can be pointed at Azure instead of the local
    files by changing the connection - all the relationships and calculations
    keep working, nothing needs rebuilding.

    The differences are only the things SQL Server does its own way:
      - IDENTITY instead of us numbering rows ourselves
      - BIT instead of BOOLEAN
      - NVARCHAR instead of VARCHAR (handles accents in names like Montréal)
      - real FOREIGN KEY constraints, so the database itself rejects a fact row
        pointing at a dimension that doesn't exist

    Run this once against an empty database, then load it with the same
    Parquet files export.py produces.
*/

/* ========================= DIMENSIONS ========================= */

CREATE TABLE dim_date (
    date_key     INT          NOT NULL PRIMARY KEY,   -- 20260501
    [date]       DATE         NOT NULL,
    year_month   CHAR(7)      NOT NULL,               -- '2026-05'
    [year]       SMALLINT     NOT NULL,
    [quarter]    TINYINT      NOT NULL,
    [month]      TINYINT      NOT NULL,
    month_name   NVARCHAR(12) NOT NULL,
    quarter_name CHAR(7)      NOT NULL
);

CREATE TABLE dim_geography (
    geography_key  INT           NOT NULL PRIMARY KEY,
    geo_name       NVARCHAR(160) NOT NULL,
    geo_level      NVARCHAR(20)  NOT NULL,
    province_name  NVARCHAR(60)  NULL,
    province_code  CHAR(2)       NULL,
    arrears_region NVARCHAR(30)  NULL,
    -- The most important column in the whole database. TRUE means this row is
    -- a total of other rows, so leave it out when adding things up.
    is_aggregate   BIT           NOT NULL,
    sort_order     SMALLINT      NOT NULL
);

CREATE TABLE dim_arrears_region (
    arrears_region_key INT           NOT NULL PRIMARY KEY,
    region_name        NVARCHAR(60)  NOT NULL,
    region_code        NVARCHAR(30)  NOT NULL,
    covers_provinces   NVARCHAR(120) NULL,
    is_national        BIT           NOT NULL,
    sort_order         SMALLINT      NOT NULL
);

CREATE TABLE dim_coverage (
    coverage_key  INT            NOT NULL PRIMARY KEY,
    coverage_name NVARCHAR(80)   NOT NULL,
    [description] NVARCHAR(400)  NULL,
    is_default    BIT            NOT NULL
);

CREATE TABLE dim_dwelling_type (
    dwelling_type_key  INT          NOT NULL PRIMARY KEY,
    dwelling_type_name NVARCHAR(80) NOT NULL,
    dwelling_category  NVARCHAR(40) NOT NULL,
    is_total           BIT          NOT NULL,
    sort_order         SMALLINT     NOT NULL
);

CREATE TABLE dim_construction_stage (
    stage_key   INT          NOT NULL PRIMARY KEY,
    stage_name  NVARCHAR(60) NOT NULL,
    stage_short NVARCHAR(20) NOT NULL,
    stage_order TINYINT      NOT NULL
);

CREATE TABLE dim_credit_product (
    credit_product_key INT            NOT NULL PRIMARY KEY,
    product_name       NVARCHAR(300)  NOT NULL,
    rate_type          NVARCHAR(30)   NULL,
    insurance_status   NVARCHAR(30)   NULL,
    is_total           BIT            NOT NULL
);

CREATE TABLE dim_price_component (
    price_component_key INT          NOT NULL PRIMARY KEY,
    component_name      NVARCHAR(60) NOT NULL
);

/* =========================== FACTS ============================ */

CREATE TABLE fact_housing_activity (
    date_key          INT NOT NULL,
    geography_key     INT NOT NULL,
    dwelling_type_key INT NOT NULL,
    stage_key         INT NOT NULL,
    coverage_key      INT NOT NULL,
    units             DECIMAL(18, 2) NULL,   -- NULL means "not published"

    CONSTRAINT FK_activity_date     FOREIGN KEY (date_key)          REFERENCES dim_date(date_key),
    CONSTRAINT FK_activity_geo      FOREIGN KEY (geography_key)     REFERENCES dim_geography(geography_key),
    CONSTRAINT FK_activity_dwelling FOREIGN KEY (dwelling_type_key) REFERENCES dim_dwelling_type(dwelling_type_key),
    CONSTRAINT FK_activity_stage    FOREIGN KEY (stage_key)         REFERENCES dim_construction_stage(stage_key),
    CONSTRAINT FK_activity_coverage FOREIGN KEY (coverage_key)      REFERENCES dim_coverage(coverage_key)
);

/*
    This is the one index worth explaining.

    A COLUMNSTORE index stores each column separately instead of each row
    together. That suits this table because reports ask things like "add up
    units for Ontario in 2026" - they touch two or three columns out of six,
    across hundreds of thousands of rows. Row-based storage would read all six
    columns of every row to answer that.

    It also compresses very well here, because the table is almost entirely
    repeated integer keys.
*/
CREATE CLUSTERED COLUMNSTORE INDEX CCI_housing_activity ON fact_housing_activity;

CREATE TABLE fact_market_absorption (
    date_key             INT NOT NULL,
    geography_key        INT NOT NULL,
    dwelling_type_key    INT NOT NULL,
    absorptions          DECIMAL(18, 2) NULL,
    unabsorbed_inventory DECIMAL(18, 2) NULL,

    CONSTRAINT FK_absorption_date     FOREIGN KEY (date_key)          REFERENCES dim_date(date_key),
    CONSTRAINT FK_absorption_geo      FOREIGN KEY (geography_key)     REFERENCES dim_geography(geography_key),
    CONSTRAINT FK_absorption_dwelling FOREIGN KEY (dwelling_type_key) REFERENCES dim_dwelling_type(dwelling_type_key)
);

CREATE TABLE fact_unoccupied_housing (
    date_key          INT NOT NULL,
    geography_key     INT NOT NULL,
    dwelling_type_key INT NOT NULL,
    unoccupied_units  DECIMAL(18, 2) NULL,

    CONSTRAINT FK_unoccupied_date     FOREIGN KEY (date_key)          REFERENCES dim_date(date_key),
    CONSTRAINT FK_unoccupied_geo      FOREIGN KEY (geography_key)     REFERENCES dim_geography(geography_key),
    CONSTRAINT FK_unoccupied_dwelling FOREIGN KEY (dwelling_type_key) REFERENCES dim_dwelling_type(dwelling_type_key)
);

CREATE CLUSTERED COLUMNSTORE INDEX CCI_unoccupied ON fact_unoccupied_housing;

CREATE TABLE fact_mortgage_arrears (
    date_key             INT NOT NULL,
    arrears_region_key   INT NOT NULL,
    total_mortgages      BIGINT NULL,
    mortgages_in_arrears BIGINT NULL,   -- NULL where CBA hides small numbers
    arrears_rate         DECIMAL(9, 4) NULL,
    is_hidden            BIT NOT NULL,

    CONSTRAINT PK_arrears PRIMARY KEY (date_key, arrears_region_key),
    CONSTRAINT FK_arrears_date   FOREIGN KEY (date_key)           REFERENCES dim_date(date_key),
    CONSTRAINT FK_arrears_region FOREIGN KEY (arrears_region_key) REFERENCES dim_arrears_region(arrears_region_key)
);
/* Only 3,400 rows, so a normal table beats columnstore here - columnstore
   needs about 100,000 rows before it starts paying off. */

CREATE TABLE fact_mortgage_originations (
    date_key           INT NOT NULL,
    credit_product_key INT NOT NULL,
    funds_advanced     DECIMAL(20, 2) NULL,
    interest_rate      DECIMAL(9, 4)  NULL,

    CONSTRAINT PK_originations PRIMARY KEY (date_key, credit_product_key),
    CONSTRAINT FK_originations_date    FOREIGN KEY (date_key)           REFERENCES dim_date(date_key),
    CONSTRAINT FK_originations_product FOREIGN KEY (credit_product_key) REFERENCES dim_credit_product(credit_product_key)
);

CREATE TABLE fact_price_index (
    date_key            INT NOT NULL,
    geography_key       INT NOT NULL,
    price_component_key INT NOT NULL,
    index_value         DECIMAL(12, 4) NULL,

    CONSTRAINT PK_price_index PRIMARY KEY (date_key, geography_key, price_component_key),
    CONSTRAINT FK_price_date      FOREIGN KEY (date_key)            REFERENCES dim_date(date_key),
    CONSTRAINT FK_price_geo       FOREIGN KEY (geography_key)       REFERENCES dim_geography(geography_key),
    CONSTRAINT FK_price_component FOREIGN KEY (price_component_key) REFERENCES dim_price_component(price_component_key)
);

CREATE TABLE fact_mortgage_rate (
    date_key   INT NOT NULL PRIMARY KEY,
    rate_5year DECIMAL(9, 4) NULL,

    CONSTRAINT FK_rate_date FOREIGN KEY (date_key) REFERENCES dim_date(date_key)
);
