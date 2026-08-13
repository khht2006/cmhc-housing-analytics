"""
Apply the analytical views and smoke-test them.

Views are rebuilt on every refresh (CREATE OR REPLACE) rather than being
created once by hand, so the view definitions live in version control alongside
the schema instead of drifting inside a database somebody clicked together.

The smoke test is deliberately shallow - it asserts each view compiles and
returns rows. Correctness of the DEFINITIONS is covered by
tests/test_views.py and by the reconciliation suite.
"""

from __future__ import annotations

import duckdb

from src.common.logging_setup import get_logger
from src.common.paths import SQL_DIR, duckdb_path
from src.common.sql_script import split_statements

log = get_logger(__name__)

VIEWS = [
    ("dw", "vw_construction_pipeline"),
    ("dw", "vw_absorption_health"),
    ("dw", "vw_arrears_trend"),
    ("dw", "vw_what_changed"),
    ("dw", "vw_data_dictionary"),
    ("ops", "vw_reconciliation_summary"),
]


def build(con: duckdb.DuckDBPyConnection | None = None) -> dict[str, int]:
    own = con is None
    con = con or duckdb.connect(str(duckdb_path()))

    ddl = (SQL_DIR / "duckdb" / "02_views.sql").read_text(encoding="utf-8")
    statements = split_statements(ddl)
    for idx, statement in enumerate(statements, start=1):
        try:
            con.execute(statement)
        except Exception:
            head = " ".join(statement.split())[:160]
            log.error("view statement %d/%d failed: %s", idx, len(statements), head)
            raise
    log.info("views applied from sql/duckdb/02_views.sql (%d statements)", len(statements))

    counts: dict[str, int] = {}
    for schema, view in VIEWS:
        n = con.sql(f"SELECT count(*) FROM {schema}.{view}").fetchone()[0]
        counts[f"{schema}.{view}"] = n
        if n == 0:
            log.warning("%s.%s returned 0 rows", schema, view)
        else:
            log.info("  %-34s %9d rows", f"{schema}.{view}", n)

    if own:
        con.close()
    return counts


if __name__ == "__main__":
    build()
