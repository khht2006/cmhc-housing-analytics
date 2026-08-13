"""
Extract layer: Canadian Bankers Association residential mortgage arrears.

Why this module exists
----------------------
Canada has no public API for sub-national mortgage delinquency. The CBA is the
only publisher with a long (1995->present), monthly, regional series of
mortgages 90+ days in arrears, and it ships as a PDF. Parsing it is the price of
having a real delinquency fact instead of a modelled one.

The PDF shape
-------------
  page 0            summary for the latest month
  pages 1..N        one region per 2-page spread, laid out in TWO side-by-side
                    column groups so a 30-year monthly series fits on a page:

     As at:   Total   Arrears   %      As at:   Total   Arrears   %
     1995-01  2,184,443  11,014  0.50%  2011-01  4,192,307  18,702  0.45%

pdfplumber's extract_text() flattens that into one line per row containing BOTH
column groups, so the parser reads each line with a regex that captures one or
two (period, total, arrears, pct) tuples and emits them as separate records.

Robustness
----------
The URL is date-stamped (stat-mortgages-arrears-<month>-<year>-en.pdf) and the
CBA publishes on a lag, so we walk backwards from the current month to find the
newest file that exists rather than assuming one is there.
"""

from __future__ import annotations

import hashlib
import io
import json
import re
from datetime import date, datetime, timezone

import pandas as pd
import pdfplumber
import requests

from src.common.logging_setup import get_logger
from src.common.paths import RAW_DIR, config

log = get_logger(__name__)

RAW_SUBDIR = RAW_DIR / "cba_arrears"
MANIFEST = RAW_DIR / "_manifest.json"
HEADERS = {"User-Agent": "cmhc-housing-analytics/1.0 (portfolio ETL)"}
MONTHS = [
    "january", "february", "march", "april", "may", "june",
    "july", "august", "september", "october", "november", "december",
]

# One (period, total, arrears, pct) group. Totals carry thousands separators;
# the arrears count and percentage are absent for suppressed cells, which is why
# the tail of the group is optional.
_GROUP = r"(\d{4}-\d{2})\s+([\d,]+)(?:\s+([\d,]+))?(?:\s+([\d.]+)%)?"
ROW_RE = re.compile(_GROUP)
REGION_RE = re.compile(r"REGION:\s*([A-Z ']+?)(?:\*+)?\s*$", re.MULTILINE)


def _to_int(raw: str | None) -> int | None:
    if raw in (None, "", "-"):
        return None
    return int(raw.replace(",", ""))


def find_latest_pdf(max_lookback: int = 8) -> tuple[str, bytes]:
    """Walk back month by month until a published arrears PDF responds 200."""
    template = config()["cba"]["url_template"]
    today = date.today()

    for back in range(max_lookback):
        year = today.year
        month_idx = today.month - 1 - back
        while month_idx < 0:
            month_idx += 12
            year -= 1

        url = template.format(month=MONTHS[month_idx], year=year)
        resp = requests.get(url, timeout=120, headers=HEADERS)
        if resp.status_code == 200 and resp.content[:4] == b"%PDF":
            log.info("CBA arrears: using %s-%02d release", year, month_idx + 1)
            return url, resp.content
        log.debug("CBA arrears: %s not published (%s)", url, resp.status_code)

    raise RuntimeError(f"No CBA arrears PDF found in the last {max_lookback} months")


def parse_pdf(payload: bytes) -> pd.DataFrame:
    """Turn the PDF into a tidy (region, period, total, arrears, pct) frame."""
    records: list[dict] = []
    current_region: str | None = None

    with pdfplumber.open(io.BytesIO(payload)) as pdf:
        for page_no, page in enumerate(pdf.pages):
            text = page.extract_text() or ""

            # A region header starts a new block; continuation pages inherit it.
            header = REGION_RE.search(text)
            if header:
                current_region = header.group(1).strip()

            if current_region is None:
                continue  # page 0 summary - the detail pages supersede it

            for line in text.splitlines():
                for period, total, arrears, pct in ROW_RE.findall(line):
                    records.append(
                        {
                            "region": current_region,
                            "period": period,
                            "total_mortgages": _to_int(total),
                            "mortgages_in_arrears": _to_int(arrears),
                            "arrears_rate_pct": float(pct) if pct else None,
                            "source_page": page_no,
                        }
                    )

    df = pd.DataFrame.from_records(records)
    if df.empty:
        raise RuntimeError("CBA parse produced zero rows - PDF layout changed")

    # The same (region, period) can appear twice if a spread repeats a boundary
    # month. Keep the first occurrence; they are identical by construction.
    df = df.drop_duplicates(subset=["region", "period"], keep="first")
    return df.sort_values(["region", "period"]).reset_index(drop=True)


def extract(force: bool = False) -> dict:
    RAW_SUBDIR.mkdir(parents=True, exist_ok=True)
    url, payload = find_latest_pdf()
    digest = hashlib.sha256(payload).hexdigest()

    manifest = json.loads(MANIFEST.read_text(encoding="utf-8")) if MANIFEST.exists() else {}
    previous = manifest.get("cba_arrears", {})
    out_csv = RAW_SUBDIR / "cba_arrears.csv"

    if previous.get("sha256") == digest and out_csv.exists() and not force:
        log.info("CBA arrears unchanged since %s - skipping parse", previous.get("extracted_at"))
        return previous

    (RAW_SUBDIR / "source.pdf").write_bytes(payload)
    df = parse_pdf(payload)
    df.to_csv(out_csv, index=False)

    entry = {
        "alias": "cba_arrears",
        "publisher": "Canadian Bankers Association",
        "feeds": "fact_mortgage_arrears",
        "source_url": url,
        "sha256": digest,
        "data_file": str(out_csv.relative_to(RAW_DIR)),
        "rows": int(len(df)),
        "regions": sorted(df["region"].unique().tolist()),
        "period_min": df["period"].min(),
        "period_max": df["period"].max(),
        "extracted_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "changed": True,
    }
    manifest["cba_arrears"] = entry
    MANIFEST.write_text(json.dumps(manifest, indent=2), encoding="utf-8")

    log.info(
        "CBA arrears: %d rows, %d regions, %s .. %s",
        entry["rows"], len(entry["regions"]), entry["period_min"], entry["period_max"],
    )
    return entry


if __name__ == "__main__":
    import argparse

    ap = argparse.ArgumentParser(description="Extract CBA mortgage arrears")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    result = extract(force=args.force)
    print(json.dumps({k: v for k, v in result.items() if k != "regions"}, indent=2))
    print("regions:", result.get("regions"))
