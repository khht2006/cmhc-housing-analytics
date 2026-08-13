"""
Extract layer: pull full StatCan tables into data/raw.

Design notes
------------
1. We download the FULL table, not an incremental slice. StatCan revises history
   silently (seasonal adjustment factors get re-estimated, preliminary months get
   restated), so an append-only incremental load would slowly drift away from the
   published figures. Full reload + a reconciliation gate is the honest pattern
   for this data, and at ~5 MB zipped the cost is trivial.

2. Every download is content-hashed and recorded in a manifest. If the hash has
   not changed since last run we skip the unzip/parse work, and the manifest
   gives us the "what vintage produced this number?" audit trail that the
   reconciliation report cites.

3. The *_MetaData.csv inside each zip is kept. It holds the official dimension
   member lists and footnote codes, which is how we build conformed dimensions
   rather than string-matching whatever showed up in the data file.
"""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import requests

from src.common.logging_setup import get_logger
from src.common.paths import RAW_DIR, config

log = get_logger(__name__)

MANIFEST = RAW_DIR / "_manifest.json"
TIMEOUT = 180
HEADERS = {"User-Agent": "cmhc-housing-analytics/1.0 (portfolio ETL)"}


def _load_manifest() -> dict:
    if MANIFEST.exists():
        return json.loads(MANIFEST.read_text(encoding="utf-8"))
    return {}


def _save_manifest(manifest: dict) -> None:
    MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def _resolve_download_url(pid: str) -> str:
    """Ask WDS for the current signed CSV download URL for a product ID."""
    base = config()["statcan"]["wds_base"]
    resp = requests.get(
        f"{base}/getFullTableDownloadCSV/{pid}/en", timeout=TIMEOUT, headers=HEADERS
    )
    resp.raise_for_status()
    payload = resp.json()
    if payload.get("status") != "SUCCESS":
        raise RuntimeError(f"WDS refused PID {pid}: {payload}")
    return payload["object"]


def extract_table(spec: dict, force: bool = False) -> dict:
    """
    Download one StatCan table and unzip it into data/raw/<alias>/.

    Returns the manifest entry describing what was pulled.
    """
    pid, alias = spec["pid"], spec["alias"]
    manifest = _load_manifest()
    previous = manifest.get(alias, {})

    url = _resolve_download_url(pid)
    log.info("[%s] downloading %s from %s", alias, spec["table"], url)

    resp = requests.get(url, timeout=TIMEOUT, headers=HEADERS)
    resp.raise_for_status()
    payload = resp.content
    digest = hashlib.sha256(payload).hexdigest()

    dest = RAW_DIR / alias
    unchanged = previous.get("sha256") == digest and dest.exists()

    if unchanged and not force:
        log.info("[%s] unchanged since %s - skipping unzip", alias, previous.get("extracted_at"))
        return previous

    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(payload)) as zf:
        names = zf.namelist()
        zf.extractall(dest)

    data_file = dest / f"{pid}.csv"
    meta_file = dest / f"{pid}_MetaData.csv"
    if not data_file.exists():
        # A few older cubes name the payload differently; fall back to the
        # largest extracted CSV rather than failing the whole run.
        csvs = sorted(dest.glob("*.csv"), key=lambda p: p.stat().st_size, reverse=True)
        if not csvs:
            raise FileNotFoundError(f"[{alias}] zip contained no CSV: {names}")
        data_file = csvs[0]

    entry = {
        "pid": pid,
        "table": spec["table"],
        "alias": alias,
        "publisher": spec.get("publisher"),
        "feeds": spec.get("feeds"),
        "source_url": url,
        "sha256": digest,
        "zip_bytes": len(payload),
        "data_file": str(data_file.relative_to(RAW_DIR)),
        "metadata_file": str(meta_file.relative_to(RAW_DIR)) if meta_file.exists() else None,
        "data_bytes": data_file.stat().st_size,
        "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "changed": not unchanged,
    }

    manifest[alias] = entry
    _save_manifest(manifest)
    log.info(
        "[%s] extracted %.1f MB CSV (zip %.1f MB, sha %s)",
        alias,
        entry["data_bytes"] / 1_048_576,
        entry["zip_bytes"] / 1_048_576,
        digest[:12],
    )
    return entry


def extract_all(force: bool = False) -> list[dict]:
    specs = config()["statcan"]["tables"]
    log.info("StatCan extract: %d tables", len(specs))
    results = []
    for spec in specs:
        try:
            results.append(extract_table(spec, force=force))
        except Exception:
            # One dead table must not abandon the other nine. The reconciliation
            # stage will flag any fact left stale by a failed extract.
            log.exception("[%s] extract FAILED", spec["alias"])
    return results


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Download StatCan source tables")
    ap.add_argument("--force", action="store_true", help="re-unzip even if unchanged")
    args = ap.parse_args()

    entries = extract_all(force=args.force)
    total = sum(e.get("data_bytes", 0) for e in entries) / 1_048_576
    log.info("Done: %d/%d tables, %.1f MB raw CSV",
             len(entries), len(config()["statcan"]["tables"]), total)
