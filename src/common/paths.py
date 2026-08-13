"""Project paths and config loading - the one place that knows the layout."""

from __future__ import annotations

import functools
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[2]

CONFIG_DIR = ROOT / "config"
DATA_DIR = ROOT / "data"
RAW_DIR = DATA_DIR / "raw"
STAGING_DIR = DATA_DIR / "staging"
WAREHOUSE_DIR = DATA_DIR / "warehouse"
EXPORT_DIR = DATA_DIR / "exports"
SQL_DIR = ROOT / "sql"
LOG_DIR = ROOT / "logs"

for _d in (RAW_DIR, STAGING_DIR, WAREHOUSE_DIR, EXPORT_DIR, LOG_DIR):
    _d.mkdir(parents=True, exist_ok=True)


@functools.lru_cache(maxsize=1)
def config() -> dict:
    """Parsed config/sources.yml. Cached - it is read many times per run."""
    with (CONFIG_DIR / "sources.yml").open(encoding="utf-8") as fh:
        return yaml.safe_load(fh)


def duckdb_path() -> Path:
    return ROOT / config()["warehouse"]["duckdb_path"]
