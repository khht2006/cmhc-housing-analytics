"""
Profile the raw StatCan CSVs before modelling anything.

StatCan ships every table in the same long/tidy shape: one row per
(geography, dimension members..., reference period) with a single VALUE column.
That uniformity is what makes a generic loader possible - but the *dimension
columns differ per table*, so we have to look before we build.

This script answers, per table: how many rows, which columns are dimensions,
how many distinct members each has, and what the period range is.

Run:  python -m src.extract.profile_raw [alias ...]
"""

from __future__ import annotations

import sys

import duckdb

from src.common.paths import RAW_DIR, config

# Columns StatCan puts in every table that are NOT analytical dimensions.
STRUCTURAL = {
    "REF_DATE", "DGUID", "UOM", "UOM_ID", "SCALAR_FACTOR", "SCALAR_ID",
    "VECTOR", "COORDINATE", "VALUE", "STATUS", "SYMBOL", "TERMINATED", "DECIMALS",
}


def profile(alias: str, pid: str, con: duckdb.DuckDBPyConnection) -> None:
    path = (RAW_DIR / alias / f"{pid}.csv").as_posix()
    rel = f"read_csv('{path}', header=true, all_varchar=true, sample_size=-1)"

    rows = con.sql(f"SELECT count(*) FROM {rel}").fetchone()[0]
    cols = [c[0] for c in con.sql(f"SELECT * FROM {rel} LIMIT 0").description]
    dims = [c for c in cols if c not in STRUCTURAL]

    lo, hi = con.sql(f"SELECT min(REF_DATE), max(REF_DATE) FROM {rel}").fetchone()
    print(f"\n{'='*78}\n{alias}  (PID {pid})")
    print(f"  rows={rows:,}   period={lo} .. {hi}")
    print(f"  dimension columns ({len(dims)}):")

    for d in dims:
        n = con.sql(f'SELECT count(DISTINCT "{d}") FROM {rel}').fetchone()[0]
        sample = con.sql(
            f'SELECT DISTINCT "{d}" FROM {rel} WHERE "{d}" IS NOT NULL '
            f'ORDER BY 1 LIMIT 4'
        ).fetchall()
        shown = " | ".join(str(s[0])[:34] for s in sample)
        print(f"    - {d:<42} {n:>6} members   e.g. {shown}")

    uom = con.sql(
        f"SELECT UOM, count(*) c FROM {rel} GROUP BY 1 ORDER BY c DESC LIMIT 4"
    ).fetchall()
    print(f"  units of measure: {uom}")


def main() -> None:
    con = duckdb.connect()
    wanted = set(sys.argv[1:])
    for spec in config()["statcan"]["tables"]:
        if wanted and spec["alias"] not in wanted:
            continue
        try:
            profile(spec["alias"], spec["pid"], con)
        except Exception as exc:  # noqa: BLE001
            print(f"\n{spec['alias']}: PROFILE FAILED - {exc}")


if __name__ == "__main__":
    main()
