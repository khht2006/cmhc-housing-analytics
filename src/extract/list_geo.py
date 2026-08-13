"""One-off: list every distinct GEO label across the housing sources.

The geography mapping (province code, CBA region, aggregate flag) is written by
hand, so it has to be written against the *actual* published labels rather than
assumed ones. This dumps them.
"""

import duckdb

from src.common.paths import RAW_DIR, config

FILES = {
    s["alias"]: s["pid"]
    for s in config()["statcan"]["tables"]
    if s["alias"] != "mortgage_rate_5yr"
}

con = duckdb.connect()
seen: dict[str, list[str]] = {}

for alias, pid in FILES.items():
    path = (RAW_DIR / alias / f"{pid}.csv").as_posix()
    rows = con.sql(
        f"SELECT DISTINCT GEO FROM read_csv('{path}', header=true, "
        f"all_varchar=true, sample_size=-1)"
    ).fetchall()
    for (geo,) in rows:
        seen.setdefault(geo, []).append(alias)

print(f"distinct GEO labels: {len(seen)}\n")
for geo in sorted(seen):
    tags = ",".join(a[:12] for a in seen[geo])
    print(f"  {geo!r:<64} {tags}")
