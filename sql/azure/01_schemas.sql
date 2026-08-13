/* =============================================================================
   Canadian Mortgage & Housing Analytics - Azure SQL Database deployment
   01: schemas and run-control tables
   -----------------------------------------------------------------------------
   Three schemas, matching the three layers of the pipeline:

     stg   landing zone. Heap tables, no constraints, truncated every run.
           Mirrors the publisher's long/tidy shape verbatim so that a bad load
           can be diagnosed against the source CSV without re-downloading.

     dw    the star schema itself. Constrained, indexed, the only thing Power BI
           and analysts are allowed to read.

     ops   run control, lineage and reconciliation results. Every refresh writes
           a row here; the Power BI "Data quality" page reads from it.

   Target: Azure SQL Database (single database, S2 or above / General Purpose).
   Idempotent - safe to re-run.
   ============================================================================= */

IF SCHEMA_ID('stg') IS NULL EXEC('CREATE SCHEMA stg');
IF SCHEMA_ID('dw')  IS NULL EXEC('CREATE SCHEMA dw');
IF SCHEMA_ID('ops') IS NULL EXEC('CREATE SCHEMA ops');
GO

/* ---------------------------------------------------------------------------
   ops.etl_run - one row per pipeline execution.
   The monthly refresh is unattended, so this table is the audit trail that
   answers "when did this number last change, and from which publisher vintage?"
   --------------------------------------------------------------------------- */
IF OBJECT_ID('ops.etl_run') IS NULL
CREATE TABLE ops.etl_run
(
    run_id           BIGINT        IDENTITY(1,1) NOT NULL,
    run_started_utc  DATETIME2(0)  NOT NULL CONSTRAINT DF_etl_run_started DEFAULT SYSUTCDATETIME(),
    run_ended_utc    DATETIME2(0)  NULL,
    status           VARCHAR(20)   NOT NULL CONSTRAINT DF_etl_run_status DEFAULT 'RUNNING',
    triggered_by     NVARCHAR(100) NULL,
    rows_loaded      BIGINT        NULL,
    notes            NVARCHAR(MAX) NULL,
    CONSTRAINT PK_etl_run PRIMARY KEY CLUSTERED (run_id),
    CONSTRAINT CK_etl_run_status CHECK (status IN ('RUNNING','SUCCESS','FAILED','BLOCKED'))
);
GO

/* ---------------------------------------------------------------------------
   ops.source_vintage - which publisher file produced which load.
   sha256 lets the pipeline skip unchanged sources and lets an analyst prove
   that two months' reports were built from different source vintages.
   --------------------------------------------------------------------------- */
IF OBJECT_ID('ops.source_vintage') IS NULL
CREATE TABLE ops.source_vintage
(
    vintage_id      BIGINT        IDENTITY(1,1) NOT NULL,
    run_id          BIGINT        NOT NULL,
    source_alias    VARCHAR(60)   NOT NULL,
    publisher       NVARCHAR(100) NULL,
    source_table    VARCHAR(30)   NULL,     -- e.g. 34-10-0143-01
    source_url      NVARCHAR(500) NULL,
    sha256          CHAR(64)      NULL,
    row_count       BIGINT        NULL,
    period_min      CHAR(7)       NULL,     -- YYYY-MM
    period_max      CHAR(7)       NULL,
    extracted_utc   DATETIME2(0)  NULL,
    CONSTRAINT PK_source_vintage PRIMARY KEY CLUSTERED (vintage_id),
    CONSTRAINT FK_source_vintage_run FOREIGN KEY (run_id) REFERENCES ops.etl_run(run_id)
);
GO

/* ---------------------------------------------------------------------------
   ops.reconciliation_result - the <1% variance gate.
   One row per (check, period, grain) comparison of a warehouse figure against
   an independently published control total.
   --------------------------------------------------------------------------- */
IF OBJECT_ID('ops.reconciliation_result') IS NULL
CREATE TABLE ops.reconciliation_result
(
    result_id        BIGINT        IDENTITY(1,1) NOT NULL,
    run_id           BIGINT        NOT NULL,
    check_name       VARCHAR(80)   NOT NULL,
    check_grain      NVARCHAR(200) NULL,     -- e.g. 'Ontario / 2026-05'
    warehouse_value  DECIMAL(20,4) NULL,
    control_value    DECIMAL(20,4) NULL,
    variance_abs     AS (ABS(ISNULL(warehouse_value,0) - ISNULL(control_value,0))) PERSISTED,
    variance_pct     DECIMAL(12,6) NULL,
    threshold_pct    DECIMAL(12,6) NOT NULL CONSTRAINT DF_recon_threshold DEFAULT 1.0,
    passed           BIT           NOT NULL,
    checked_utc      DATETIME2(0)  NOT NULL CONSTRAINT DF_recon_checked DEFAULT SYSUTCDATETIME(),
    CONSTRAINT PK_reconciliation_result PRIMARY KEY CLUSTERED (result_id),
    CONSTRAINT FK_recon_run FOREIGN KEY (run_id) REFERENCES ops.etl_run(run_id)
);
GO

CREATE NONCLUSTERED INDEX IX_recon_run_passed
    ON ops.reconciliation_result (run_id, passed)
    INCLUDE (check_name, variance_pct);
GO
