"""
One-off investigation: are 34-10-0143 and 34-10-0151 the same universe?

Both are titled "housing starts, under construction and completions in
(all) centres 10,000 and over". If they carry identical values for the same
province/month/stage/type, loading both into fact_housing_activity would double
every figure. If they differ, they are genuinely different populations and both
belong - under different coverage keys.

This is the kind of question you must answer with the data, not the title.
"""

import duckdb

from src.common.paths import RAW_DIR

A = (RAW_DIR / "housing_activity_centres_10k" / "34100143.csv").as_posix()
B = (RAW_DIR / "housing_activity_all_centres" / "34100151.csv").as_posix()

con = duckdb.connect()
con.execute(f"""
CREATE VIEW a AS SELECT REF_DATE, GEO, "Housing estimates" AS stage,
       "Type of unit" AS unit, TRY_CAST(VALUE AS DOUBLE) AS v
FROM read_csv('{A}', header=true, all_varchar=true, sample_size=-1);

CREATE VIEW b AS SELECT REF_DATE, GEO, "Housing estimates" AS stage,
       "Type of unit" AS unit, TRY_CAST(VALUE AS DOUBLE) AS v
FROM read_csv('{B}', header=true, all_varchar=true, sample_size=-1);
""")

print("--- distinct unit labels ---")
print("34100143:", [r[0] for r in con.sql("SELECT DISTINCT unit FROM a ORDER BY 1").fetchall()])
print("34100151:", [r[0] for r in con.sql("SELECT DISTINCT unit FROM b ORDER BY 1").fetchall()])

print("\n--- overlapping (period, geo, stage, unit) cells: do values agree? ---")
res = con.sql("""
    SELECT count(*)                                      AS overlapping_cells,
           sum(CASE WHEN a.v IS NOT DISTINCT FROM b.v THEN 1 ELSE 0 END) AS identical,
           sum(CASE WHEN a.v IS DISTINCT FROM b.v THEN 1 ELSE 0 END)     AS differing
    FROM a JOIN b
      ON a.REF_DATE = b.REF_DATE AND a.GEO = b.GEO
     AND a.stage = b.stage AND a.unit = b.unit
""").fetchall()
print("overlapping / identical / differing:", res)

print("\n--- sample of differing cells ---")
rows = con.sql("""
    SELECT a.REF_DATE, a.GEO, a.stage, a.unit, a.v AS v_34100143, b.v AS v_34100151
    FROM a JOIN b
      ON a.REF_DATE = b.REF_DATE AND a.GEO = b.GEO
     AND a.stage = b.stage AND a.unit = b.unit
    WHERE a.v IS DISTINCT FROM b.v
    ORDER BY a.REF_DATE DESC
    LIMIT 12
""").fetchall()
for r in rows:
    print("   ", r)

print("\n--- head-to-head, Ontario housing starts, recent months ---")
rows = con.sql("""
    SELECT a.REF_DATE, a.unit, a.v AS centres_10k_34100143, b.v AS all_centres_34100151
    FROM a JOIN b
      ON a.REF_DATE = b.REF_DATE AND a.GEO = b.GEO
     AND a.stage = b.stage AND a.unit = b.unit
    WHERE a.GEO = 'Ontario' AND a.stage = 'Housing starts'
      AND a.REF_DATE >= '2026-01'
    ORDER BY a.REF_DATE, a.unit
""").fetchall()
for r in rows:
    print("   ", r)
