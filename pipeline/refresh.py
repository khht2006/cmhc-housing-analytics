"""
Monthly refresh orchestrator - the single entry point Task Scheduler calls.

Stages, in order:

    1. extract        StatCan WDS tables + CBA arrears PDF -> data/raw
    2. build          raw -> dimensions -> facts (one transaction)
    3. views          rebuild the analytical views
    4. reconcile      compare against independently published control totals
    5. export         write the star to Parquet for Power BI

The gate that matters
---------------------
Stage 5 does NOT run if stage 4 finds a new breach. That ordering is the whole
point of the exercise: a refresh that silently publishes numbers it cannot
reconcile is worse than one that fails loudly, because a wrong dashboard gets
believed and acted on. On a breach the previous export stays in place, Power BI
keeps showing last month's verified figures, and the run exits non-zero so the
scheduler surfaces it.

"New breach" excludes the documented publisher anomalies in
src/quality/reconciliation.py::KNOWN_ANOMALIES - those are the publisher's
errors, already investigated, and must not cry wolf every month.

Usage
-----
    python -m pipeline.refresh                 # full monthly refresh
    python -m pipeline.refresh --skip-extract   # rebuild from cached raw data
    python -m pipeline.refresh --force-extract  # ignore source hash, re-download
"""

from __future__ import annotations

import argparse
import sys
import traceback
from datetime import datetime, timezone

import duckdb

from src.common.logging_setup import get_logger
from src.common.paths import duckdb_path
from src.extract import cba_arrears, statcan
from src.load import build_views, build_warehouse, export_powerbi
from src.quality import reconciliation

log = get_logger("pipeline.refresh")


def _mark_run(run_id: int, status: str, notes: str = "") -> None:
    con = duckdb.connect(str(duckdb_path()))
    con.execute(
        "UPDATE ops.etl_run SET status = ?, run_ended_utc = ?, notes = ? WHERE run_id = ?",
        [status, datetime.now(timezone.utc), notes, run_id],
    )
    con.close()


def refresh(skip_extract: bool = False, force_extract: bool = False,
            triggered_by: str = "scheduler") -> int:
    started = datetime.now(timezone.utc)
    log.info("#" * 78)
    log.info("MONTHLY REFRESH starting %s (triggered by: %s)",
             started.strftime("%Y-%m-%d %H:%M:%S UTC"), triggered_by)
    log.info("#" * 78)

    run_id = 0
    try:
        # -- 1. extract ------------------------------------------------------
        if skip_extract:
            log.info("[1/5] extract SKIPPED (--skip-extract)")
        else:
            log.info("[1/5] extract")
            entries = statcan.extract_all(force=force_extract)
            changed = [e for e in entries if e.get("changed")]
            log.info("      StatCan: %d tables, %d changed since last run",
                     len(entries), len(changed))

            arrears = cba_arrears.extract(force=force_extract)
            log.info("      CBA arrears: %s .. %s (%d rows)",
                     arrears.get("period_min"), arrears.get("period_max"),
                     arrears.get("rows", 0))

        # -- 2. build --------------------------------------------------------
        log.info("[2/5] build warehouse")
        result = build_warehouse.build(triggered_by=triggered_by)
        run_id = result["run_id"]

        # -- 3. views --------------------------------------------------------
        log.info("[3/5] build views")
        build_views.build()

        # -- 4. reconcile ----------------------------------------------------
        log.info("[4/5] reconcile")
        results = reconciliation.run(run_id=run_id)
        new_breaches = int((~results.within_tolerance & ~results.is_known_anomaly).sum())

        if new_breaches:
            # Deliberately NOT exporting. Power BI keeps last month's verified
            # numbers rather than picking up figures we cannot vouch for.
            detail = (
                results[~results.within_tolerance & ~results.is_known_anomaly]
                .head(10)[["check_name", "check_grain", "warehouse_value",
                           "control_value", "variance_pct"]]
                .to_string(index=False)
            )
            log.error("RECONCILIATION FAILED: %d new breaches\n%s", new_breaches, detail)
            log.error("EXPORT SKIPPED - Power BI will keep the last verified export.")
            _mark_run(run_id, "BLOCKED", f"{new_breaches} new reconciliation breaches")
            return 2

        # -- 5. export -------------------------------------------------------
        log.info("[5/5] export for Power BI")
        manifest = export_powerbi.export()

        elapsed = (datetime.now(timezone.utc) - started).total_seconds()
        total_rows = sum(t["rows"] for t in manifest["tables"].values())
        _mark_run(run_id, "SUCCESS",
                  f"{result['fact_rows']} fact rows, {len(results)} reconciliation checks")

        log.info("#" * 78)
        log.info("REFRESH SUCCEEDED in %.1fs", elapsed)
        log.info("  fact rows          %s", f"{result['fact_rows']:,}")
        log.info("  exported rows      %s", f"{total_rows:,}")
        log.info("  reconciliation     %d comparisons, 0 new breaches", len(results))
        log.info("#" * 78)
        return 0

    except Exception:
        log.error("REFRESH FAILED\n%s", traceback.format_exc())
        if run_id:
            _mark_run(run_id, "FAILED", traceback.format_exc()[:2000])
        return 1


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Monthly refresh")
    ap.add_argument("--skip-extract", action="store_true",
                    help="rebuild from cached raw files")
    ap.add_argument("--force-extract", action="store_true",
                    help="re-download even if the source hash is unchanged")
    ap.add_argument("--triggered-by", default="manual")
    args = ap.parse_args()

    sys.exit(refresh(args.skip_extract, args.force_extract, args.triggered_by))
