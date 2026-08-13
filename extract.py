"""
STEP 1: Download the raw data.

Run:  python extract.py

This downloads 6 tables from Statistics Canada and 1 PDF from the Canadian
Bankers Association, and saves them into data/raw/. Nothing is changed or
cleaned here - we keep the original files exactly as published so we can always
go back and check what the source actually said.
"""

import io
import re
import zipfile
from datetime import date

import pandas as pd
import pdfplumber
import requests

from config import CBA_URL, RAW_DIR, STATCAN_TABLES

# Statistics Canada's download API.
STATCAN_API = "https://www150.statcan.gc.ca/t1/wds/rest/getFullTableDownloadCSV"


def download_statcan_table(pid, name):
    """
    Download one Statistics Canada table.

    It's a two-step process: first we ask their API where the file is, then we
    download the zip it points us to. Each zip has two CSVs inside - the actual
    data, and a metadata file describing the columns.
    """
    # Step 1: ask where the file is. The API replies with JSON like:
    #   {"status": "SUCCESS", "object": "https://.../34100143-eng.zip"}
    response = requests.get(f"{STATCAN_API}/{pid}/en", timeout=120)
    response.raise_for_status()
    file_url = response.json()["object"]

    # Step 2: download the zip and unpack it.
    print(f"  downloading {name} (table {pid})...", end=" ", flush=True)
    zip_bytes = requests.get(file_url, timeout=180).content

    folder = RAW_DIR / name
    folder.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        archive.extractall(folder)

    csv_file = folder / f"{pid}.csv"
    size_mb = csv_file.stat().st_size / 1024 / 1024
    print(f"{size_mb:.1f} MB")


def download_all_statcan():
    print("Statistics Canada:")
    for table in STATCAN_TABLES:
        download_statcan_table(table["pid"], table["name"])


# -----------------------------------------------------------------------------
# The CBA arrears PDF
# -----------------------------------------------------------------------------
# This is the messy one. The PDF has a page per region, and each page lists
# monthly numbers in two side-by-side columns to fit 30 years onto one page:
#
#     As at:   Total      Arrears   %       As at:   Total      Arrears   %
#     1995-01  2,184,443  11,014    0.50%   2011-01  4,192,307  18,702    0.45%
#
# When we pull the text out, each line contains BOTH halves. So we use a regex
# that finds every "year-month, number, number, percent" group on a line, and
# turn each one into its own row.

MONTHS = ["january", "february", "march", "april", "may", "june",
          "july", "august", "september", "october", "november", "december"]

# Breaking the pattern down:
#   (\d{4}-\d{2})   the date, like "1995-01"
#   ([\d,]+)        total mortgages, digits and commas: "2,184,443"
#   ([\d,]+)?       arrears count - the ? makes it optional, because CBA hides
#                   this number for the Territories (too few to be private)
#   ([\d.]+)%?      the percentage, also optional for the same reason
ROW_PATTERN = re.compile(r"(\d{4}-\d{2})\s+([\d,]+)(?:\s+([\d,]+))?(?:\s+([\d.]+)%)?")
REGION_PATTERN = re.compile(r"REGION:\s*([A-Z ']+?)\*{0,2}\s*$", re.MULTILINE)


def find_latest_cba_pdf():
    """
    Find the newest arrears PDF.

    The filename contains the month and year, and CBA publishes a few months
    behind. So we start at this month and walk backwards until we find one that
    exists.
    """
    today = date.today()
    for months_back in range(8):
        month_index = today.month - 1 - months_back
        year = today.year
        while month_index < 0:       # went back past January, roll to last year
            month_index += 12
            year -= 1

        url = CBA_URL.format(month=MONTHS[month_index], year=year)
        response = requests.get(url, timeout=120)
        if response.status_code == 200:
            print(f"  found the {MONTHS[month_index].title()} {year} report")
            return response.content

    raise RuntimeError("Couldn't find a CBA arrears PDF from the last 8 months")


def text_to_number(text):
    """'2,184,443' -> 2184443. Returns None for missing values."""
    if not text:
        return None
    return int(text.replace(",", ""))


def read_cba_pdf(pdf_bytes):
    """Pull every (region, month, total, arrears, rate) row out of the PDF."""
    rows = []
    current_region = None

    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""

            # Each region's section starts with a "REGION: ONTARIO" header.
            # Pages without one are continuations, so we keep the region we're
            # already on.
            header = REGION_PATTERN.search(text)
            if header:
                current_region = header.group(1).strip()

            if current_region is None:
                continue    # first page is a summary we don't need

            for line in text.splitlines():
                for month, total, arrears, rate in ROW_PATTERN.findall(line):
                    rows.append({
                        "region": current_region,
                        "month": month,
                        "total_mortgages": text_to_number(total),
                        "mortgages_in_arrears": text_to_number(arrears),
                        "arrears_rate": float(rate) if rate else None,
                    })

    table = pd.DataFrame(rows)
    # A month can appear twice if a page boundary repeats it. The values are
    # identical, so keeping the first is safe.
    table = table.drop_duplicates(subset=["region", "month"])
    return table.sort_values(["region", "month"])


def download_cba_arrears():
    print("Canadian Bankers Association:")
    pdf_bytes = find_latest_cba_pdf()
    table = read_cba_pdf(pdf_bytes)

    folder = RAW_DIR / "arrears"
    folder.mkdir(parents=True, exist_ok=True)
    table.to_csv(folder / "arrears.csv", index=False)

    print(f"  read {len(table)} rows, {table.region.nunique()} regions, "
          f"{table.month.min()} to {table.month.max()}")


def main():
    print("=" * 70)
    print("STEP 1: DOWNLOADING DATA")
    print("=" * 70)
    download_all_statcan()
    download_cba_arrears()
    print("\nDone. Raw files are in data/raw/")


if __name__ == "__main__":
    main()
