"""
Discover candidate Statistics Canada / CMHC tables from the StatCan Web Data Service.

StatCan exposes every table ("cube") through a REST API. `getAllCubesListLite`
returns one JSON record per table with its productId (the 8-digit PID behind a
table number like 34-10-0143-01) plus English/French titles and the date range
covered. We pull that once, cache it, and grep it for the subject areas we care
about so the pipeline is built on verified PIDs instead of guessed ones.

Run:  python -m src.extract.discover_sources
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import requests

WDS = "https://www150.statcan.gc.ca/t1/wds/rest"
CACHE = Path(__file__).resolve().parents[2] / "data" / "raw" / "_statcan_cubes.json"

# Subject areas that map to the dashboard's three fact tables.
KEYWORD_GROUPS: dict[str, list[str]] = {
    "housing_starts": ["housing start", "under construction", "completion"],
    "mortgage_credit": ["mortgage", "residential loan", "funds advanced"],
    "delinquency_risk": ["delinquen", "arrears", "debt service ratio", "insolvenc"],
    "prices_market": ["new housing price", "residential property price", "average rent"],
    "household_credit": ["credit liabilities", "household debt", "net worth of household"],
}


def fetch_cube_list(refresh: bool = False) -> list[dict]:
    """Download (and cache) the full StatCan cube catalogue."""
    if CACHE.exists() and not refresh:
        return json.loads(CACHE.read_text(encoding="utf-8"))

    CACHE.parent.mkdir(parents=True, exist_ok=True)
    resp = requests.get(f"{WDS}/getAllCubesListLite", timeout=120)
    resp.raise_for_status()
    cubes = resp.json()
    CACHE.write_text(json.dumps(cubes), encoding="utf-8")
    return cubes


def pid_to_table_number(pid: str) -> str:
    """36100434 -> 36-10-0434-01 (the number StatCan shows on its website)."""
    pid = str(pid)
    return f"{pid[0:2]}-{pid[2:4]}-{pid[4:8]}-01"


def search(cubes: list[dict], keywords: list[str]) -> list[dict]:
    pattern = re.compile("|".join(re.escape(k) for k in keywords), re.IGNORECASE)
    hits = []
    for cube in cubes:
        title = cube.get("cubeTitleEn") or ""
        if not pattern.search(title):
            continue
        hits.append(
            {
                "pid": str(cube.get("productId")),
                "table": pid_to_table_number(cube.get("productId")),
                "title": title,
                "start": cube.get("cubeStartDate"),
                "end": cube.get("cubeEndDate"),
                "frequency": cube.get("frequencyCode"),
                # The Lite endpoint returns a bare code, not archiveStatusEn.
                # Verified against known-live tables (34-10-0143, 18-10-0205):
                # "2" = CURRENT, "1" = ARCHIVED. Note this reads backwards.
                "archived": str(cube.get("archived")),
            }
        )
    # Most recently updated first - we need tables still being published.
    return sorted(hits, key=lambda h: (h["end"] or ""), reverse=True)


def main() -> None:
    cubes = fetch_cube_list()
    print(f"StatCan catalogue: {len(cubes):,} tables\n")

    for group, keywords in KEYWORD_GROUPS.items():
        hits = search(cubes, keywords)
        live = [h for h in hits if h["archived"] == "2"]
        print(f"=== {group} ({len(hits)} matches, {len(live)} current) ===")
        for h in live[:25]:
            print(f"  {h['table']}  {h['start']}..{h['end']}  freq={h['frequency']}  {h['title'][:110]}")
        print()


if __name__ == "__main__":
    main()
