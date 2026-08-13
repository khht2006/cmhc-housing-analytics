"""
STEP 4: Write the tables out for Power BI.

Run:  python export.py

Power BI can't open a DuckDB file directly, so we write every table out as a
Parquet file instead. Parquet is a column-based file format that Power BI reads
natively - no drivers to install, and unlike CSV it remembers what type each
column is, so you don't have to fix "everything is text" on import.

One thing worth noticing: we export the tables SEPARATELY, exactly as they are
in the database. It's tempting to join everything into one big flat table
first, but that's a mistake - you'd lose the ability to filter several fact
tables with one slicer, and the file would be much bigger.
"""

import duckdb

from config import EXPORT_DIR, WAREHOUSE_FILE

TABLES = [
    # dimensions
    "dim_date",
    "dim_geography",
    "dim_arrears_region",
    "dim_coverage",
    "dim_dwelling_type",
    "dim_construction_stage",
    "dim_credit_product",
    "dim_price_component",
    # facts
    "fact_housing_activity",
    "fact_market_absorption",
    "fact_unoccupied_housing",
    "fact_mortgage_arrears",
    "fact_mortgage_originations",
    "fact_price_index",
    "fact_mortgage_rate",
    # views, for the pages that tell a story
    "pipeline_health",
    "what_changed",
    "arrears_trend",
    # so the dashboard can show its own quality report
    "check_results",
]


def main():
    print("=" * 70)
    print("STEP 4: EXPORTING FOR POWER BI")
    print("=" * 70)

    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    db = duckdb.connect(str(WAREHOUSE_FILE), read_only=True)

    total_rows = 0
    for table in TABLES:
        output_path = (EXPORT_DIR / f"{table}.parquet").as_posix()
        db.execute(f"COPY (SELECT * FROM {table}) TO '{output_path}' (FORMAT PARQUET)")

        rows = db.sql(f"SELECT count(*) FROM {table}").fetchone()[0]
        size_mb = (EXPORT_DIR / f"{table}.parquet").stat().st_size / 1024 / 1024
        total_rows += rows
        print(f"  {table:<28} {rows:>9,} rows   {size_mb:>6.2f} MB")

    db.close()
    print(f"\nDone. {len(TABLES)} files, {total_rows:,} rows, in {EXPORT_DIR}")
    print("Open Power BI and follow powerbi/setup.md to load them.")


if __name__ == "__main__":
    main()
