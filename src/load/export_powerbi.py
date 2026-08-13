"""
Export the star schema for Power BI.

Why Parquet and not a live DuckDB connection
--------------------------------------------
Power BI has no native DuckDB connector; reaching it needs a third-party ODBC
driver installed on every machine that opens the file, plus on the gateway.
Parquet needs nothing - Power BI reads a folder of Parquet natively, preserves
types (no "everything is text" repair step), and compresses ~10x versus CSV.

The export is deliberately the STAR, not a flattened wide table. Handing Power
BI one denormalised table is the single most common mistake in this kind of
project: it destroys the ability to cross-filter facts through shared
dimensions, bloats the model, and forces every measure to re-derive context.
Import the star, wire the relationships once, and every fact filters together.

The analytical views are exported too, but as convenience tables for the
narrative pages - the star remains the model.
"""

from __future__ import annotations

import json
import shutil
from datetime import datetime, timezone

import duckdb

from src.common.logging_setup import get_logger
from src.common.paths import EXPORT_DIR, duckdb_path

log = get_logger(__name__)

# The model Power BI imports. Order matters only for readability.
DIMENSIONS = [
    "dim_date",
    "dim_geography",
    "dim_arrears_region",
    "dim_coverage",
    "dim_dwelling_type",
    "dim_construction_stage",
    "dim_credit_product",
    "dim_price_component",
    "dim_source",
]

FACTS = [
    "fact_housing_activity",
    "fact_market_absorption",
    "fact_mortgage_arrears",
    "fact_mortgage_originations",
    "fact_price_index",
    "fact_household_credit",
    "fact_rate_environment",
]

# Exported for the narrative pages; the star above is still the model.
VIEWS = [
    ("dw", "vw_construction_pipeline"),
    ("dw", "vw_absorption_health"),
    ("dw", "vw_arrears_trend"),
    ("dw", "vw_what_changed"),
    ("ops", "vw_reconciliation_summary"),
]


def export(clean: bool = True) -> dict:
    con = duckdb.connect(str(duckdb_path()), read_only=True)

    if clean and EXPORT_DIR.exists():
        # A stale Parquet file from a renamed table would be silently imported
        # by Power BI's folder connector, so the export directory is rebuilt.
        shutil.rmtree(EXPORT_DIR)
    (EXPORT_DIR / "dimensions").mkdir(parents=True, exist_ok=True)
    (EXPORT_DIR / "facts").mkdir(parents=True, exist_ok=True)
    (EXPORT_DIR / "views").mkdir(parents=True, exist_ok=True)

    manifest: dict = {
        "exported_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "tables": {},
    }

    def dump(schema: str, name: str, folder: str) -> None:
        path = (EXPORT_DIR / folder / f"{name}.parquet").as_posix()
        con.execute(
            f"COPY (SELECT * FROM {schema}.{name}) TO '{path}' "
            f"(FORMAT PARQUET, COMPRESSION ZSTD)"
        )
        n = con.sql(f"SELECT count(*) FROM {schema}.{name}").fetchone()[0]
        size = (EXPORT_DIR / folder / f"{name}.parquet").stat().st_size
        manifest["tables"][name] = {
            "schema": schema, "folder": folder, "rows": n, "bytes": size,
        }
        log.info("  %-32s %9d rows  %7.2f MB", name, n, size / 1_048_576)

    log.info("--- dimensions ---")
    for name in DIMENSIONS:
        dump("dw", name, "dimensions")

    log.info("--- facts ---")
    for name in FACTS:
        dump("dw", name, "facts")

    log.info("--- views ---")
    for schema, name in VIEWS:
        dump(schema, name, "views")

    (EXPORT_DIR / "_export_manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    total_rows = sum(t["rows"] for t in manifest["tables"].values())
    total_mb = sum(t["bytes"] for t in manifest["tables"].values()) / 1_048_576
    log.info("=" * 70)
    log.info("exported %d tables, %d rows, %.1f MB to %s",
             len(manifest["tables"]), total_rows, total_mb, EXPORT_DIR)
    con.close()
    return manifest


if __name__ == "__main__":
    export()
