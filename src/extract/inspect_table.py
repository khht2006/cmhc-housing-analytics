"""Inspect one raw table's real columns and find what breaks an assumed grain.

Usage: python -m src.extract.inspect_table <alias> [grain_col ...]
"""

import sys

import duckdb

from src.common.paths import RAW_DIR, config

alias = sys.argv[1]
grain = sys.argv[2:]

spec = next(s for s in config()["statcan"]["tables"] if s["alias"] == alias)
path = (RAW_DIR / alias / f"{spec['pid']}.csv").as_posix()
rel = f"read_csv('{path}', header=true, all_varchar=true, sample_size=-1)"

con = duckdb.connect()
cols = [c[0] for c in con.sql(f"SELECT * FROM {rel} LIMIT 0").description]
print(f"{alias} columns ({len(cols)}):")
for c in cols:
    n = con.sql(f'SELECT count(DISTINCT "{c}") FROM {rel}').fetchone()[0]
    print(f"   {c:<45} {n:>6} distinct")

total = con.sql(f"SELECT count(*) FROM {rel}").fetchone()[0]
print(f"\ntotal rows: {total:,}")

if grain:
    keys = ", ".join(f'"{g}"' for g in grain)
    dupes = con.sql(f"""
        SELECT {keys}, count(*) AS n
        FROM {rel} GROUP BY {keys} HAVING count(*) > 1
        ORDER BY n DESC LIMIT 5
    """).fetchall()
    print(f"\nassumed grain ({', '.join(grain)}) -> {len(dupes)} duplicated combos shown:")
    for d in dupes:
        print("   ", d)

    if dupes:
        where = " AND ".join(f'"{g}" = ?' for g in grain)
        sample = con.execute(
            f"SELECT * FROM {rel} WHERE {where} LIMIT 6", list(dupes[0][:-1])
        ).fetchall()
        print("\n   full rows for the first duplicated combo:")
        print("   cols:", cols)
        for s in sample:
            print("   ", s)
