"""
All the settings for this project in one place.

Every data source we download is listed here, so if you want to add or remove
one you only edit this file - never the download code.
"""

from pathlib import Path

# --- Where things live -------------------------------------------------------
# __file__ is this file's path, .parent is the folder it's in. Doing it this way
# means the project works no matter where you put the folder.
PROJECT_DIR = Path(__file__).parent
RAW_DIR = PROJECT_DIR / "data" / "raw"          # downloaded files, untouched
WAREHOUSE_FILE = PROJECT_DIR / "data" / "cmhc.duckdb"   # the database
EXPORT_DIR = PROJECT_DIR / "data" / "exports"   # what Power BI reads
SQL_DIR = PROJECT_DIR / "sql"


# --- Statistics Canada tables we download ------------------------------------
# Statistics Canada gives every table an 8-digit "product ID" (PID). The table
# number you see on their website, like 34-10-0143-01, is just the PID with
# dashes: 34100143.
#
# You can look any of these up at:
#   https://www150.statcan.gc.ca/t1/tbl1/en/tv.action?pid=<PID>01

STATCAN_TABLES = [
    {
        "pid": "34100143",
        "name": "housing_activity",
        "description": "Housing starts, under construction, completions - towns of 10,000+",
    },
    {
        "pid": "34100154",
        "name": "housing_cma",
        "description": "Same three measures, but for 37 big metro areas",
    },
    {
        "pid": "34100149",
        "name": "absorptions",
        "description": "Finished homes: how many sold, how many still empty",
    },
    {
        "pid": "34100162",
        "name": "unoccupied",
        "description": "Newly built homes that are finished but nobody lives in yet",
    },
    {
        "pid": "18100205",
        "name": "price_index",
        "description": "New housing price index",
    },
    {
        "pid": "34100145",
        "name": "mortgage_rate",
        "description": "5-year conventional mortgage rate",
    },
    {
        "pid": "10100006",
        "name": "originations",
        "description": "Bank of Canada: new mortgage money lent out each month",
    },
]


# --- Canadian Bankers Association mortgage arrears ---------------------------
# This is the only public source of mortgage delinquency data by region in
# Canada, going back to 1995. Annoyingly it's only published as a PDF, so we
# have to read the numbers out of it (see extract.py).
CBA_URL = ("https://cba.ca/Assets/CanadianBankersAssociation/Documents/"
           "Articles/Statistics/stat-mortgages-arrears-{month}-{year}-en.pdf")


# --- Settings ----------------------------------------------------------------
# We only load data from 1990 onwards. Some tables go back to 1948, but most
# start in the 1990s, and a date list full of empty months is annoying to use.
START_MONTH = "1990-01"

# When we check our numbers against the published ones, how far apart can they
# be before we call it a problem? See check.py for why there are two of these.
MAX_PERCENT_DIFFERENCE = 1.0      # for counts and dollars
MAX_RATE_DIFFERENCE = 0.02        # for percentages, measured in percentage points
