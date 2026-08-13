"""
Build the DuckDB warehouse: schema -> dimensions -> facts.

Load strategy: full truncate-and-reload, inside a single transaction.

That is a deliberate choice, not laziness. StatCan silently revises history -
seasonal adjustment factors get re-estimated and preliminary months restated -
so an incremental append would drift away from the published figures month by
month. At ~1M source rows the full rebuild takes seconds, and it makes the
warehouse a pure function of (sources, code), which is what lets the
reconciliation gate mean anything.

The whole build runs in one transaction so a mid-run failure leaves the previous
month's warehouse intact and queryable rather than half-loaded.
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone

import duckdb

from src.common.logging_setup import get_logger
from src.common.paths import ROOT, SQL_DIR, duckdb_path
from src.transform import dimensions as dims
from src.transform import facts as fct

log = get_logger(__name__)

FACT_BUILDERS = [
    ("fact_housing_activity", fct.build_fact_housing_activity),
    ("fact_market_absorption", fct.build_fact_market_absorption),
    ("fact_mortgage_arrears", fct.build_fact_mortgage_arrears),
    ("fact_mortgage_originations", fct.build_fact_mortgage_originations),
    ("fact_price_index", fct.build_fact_price_index),
    ("fact_household_credit", fct.build_fact_household_credit),
    ("fact_rate_environment", fct.build_fact_rate_environment),
]


def apply_schema(con: duckdb.DuckDBPyConnection) -> None:
    ddl = (SQL_DIR / "duckdb" / "01_schema.sql").read_text(encoding="utf-8")
    con.execute(ddl)
    log.info("schema applied from sql/duckdb/01_schema.sql")


def next_run_id(con: duckdb.DuckDBPyConnection) -> int:
    return (con.sql("SELECT COALESCE(max(run_id), 0) + 1 FROM ops.etl_run").fetchone()[0])


def build(date_start: str = "1990-01", date_end: str | None = None,
          triggered_by: str = "manual") -> dict:
    con = duckdb.connect(str(duckdb_path()))
    apply_schema(con)

    run_id = next_run_id(con)
    started = datetime.now(timezone.utc)
    con.execute(
        "INSERT INTO ops.etl_run (run_id, run_started_utc, status, triggered_by) "
        "VALUES (?, ?, 'RUNNING', ?)",
        [run_id, started, triggered_by],
    )

    # The date dimension must span the widest range any fact needs. Facts INNER
    # JOIN dim_date, so a short date dimension silently truncates history - a
    # failure mode worth being explicit about.
    if date_end is None:
        date_end = datetime.now(timezone.utc).strftime("%Y-%m")

    counts: dict[str, int] = {}
    try:
        con.execute("BEGIN TRANSACTION")

        log.info("--- dimensions ---")
        counts["dim_date"] = dims.build_dim_date(con, date_start, date_end)
        counts["dim_source"] = dims.build_dim_source(con)
        counts["dim_geography"] = dims.build_dim_geography(con)
        counts["dim_arrears_region"] = dims.build_dim_arrears_region(con)
        counts["dim_coverage"] = dims.build_dim_coverage(con)
        counts["dim_construction_stage"] = dims.build_dim_construction_stage(con)
        counts["dim_dwelling_type"] = dims.build_dim_dwelling_type(con)
        counts["dim_price_component"] = dims.build_dim_price_component(con)
        counts["dim_credit_product"] = dims.build_dim_credit_product(con)

        log.info("--- facts ---")
        for name, builder in FACT_BUILDERS:
            counts[name] = builder(con)

        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        con.execute(
            "UPDATE ops.etl_run SET status='FAILED', run_ended_utc=? WHERE run_id=?",
            [datetime.now(timezone.utc), run_id],
        )
        log.exception("warehouse build FAILED - rolled back, previous warehouse intact")
        raise

    fact_rows = sum(v for k, v in counts.items() if k.startswith("fact_"))
    con.execute(
        "UPDATE ops.etl_run SET status='SUCCESS', run_ended_utc=?, rows_loaded=? WHERE run_id=?",
        [datetime.now(timezone.utc), fact_rows, run_id],
    )

    elapsed = (datetime.now(timezone.utc) - started).total_seconds()
    log.info("=" * 70)
    log.info("run %d SUCCESS in %.1fs | %d dimension rows | %d fact rows",
             run_id,
             elapsed,
             sum(v for k, v in counts.items() if k.startswith("dim_")),
             fact_rows)
    log.info("warehouse: %s", duckdb_path().relative_to(ROOT))
    con.close()

    return {"run_id": run_id, "counts": counts, "fact_rows": fact_rows}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Build the DuckDB star schema")
    ap.add_argument("--start", default="1990-01", help="first month in dim_date")
    ap.add_argument("--end", default=None, help="last month in dim_date (default: now)")
    ap.add_argument("--triggered-by", default="manual")
    args = ap.parse_args()

    result = build(args.start, args.end, args.triggered_by)
    print()
    for table, n in result["counts"].items():
        print(f"  {table:<32} {n:>9,}")
