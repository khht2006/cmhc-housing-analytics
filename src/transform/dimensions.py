"""
Build the conformed dimensions.

Pattern used throughout: gather the distinct member set from the raw sources,
enrich it in pandas, then hand it to DuckDB as a registered frame and INSERT
... SELECT into the real table. Surrogate keys are assigned with row_number()
over a deterministic ORDER BY, so a rebuild from identical sources produces
identical keys.

Keys are only guaranteed stable *within* a source vintage. The pipeline is
truncate-and-reload for both dimensions and facts, so keys never dangle - but
this is why nothing outside the warehouse (bookmarks, saved filters, hand-written
extracts) may persist a surrogate key.
"""

from __future__ import annotations

import calendar
import json

import duckdb
import pandas as pd

from src.common.logging_setup import get_logger
from src.common.paths import RAW_DIR, config
from src.transform.geography import (
    CBA_REGION_BY_PROVINCE,
    classify,
)
from src.transform.statcan_metadata import dimension_members

log = get_logger(__name__)

UNKNOWN_KEY = -1

# The four statistical universes CMHC publishes housing activity for. They are
# NESTED, not a drillable hierarchy:
#
#   All areas  >  centres 10,000+  >  centres 50,000+  >  selected CMAs
#
# Verified empirically rather than inferred from titles - 34-10-0143 exceeds
# 34-10-0151 in every overlapping cell (src/extract/compare_universes.py).
# Because they overlap, coverage sits ON the fact grain: summing across
# coverages would count the same dwelling up to four times.
COVERAGES = [
    {
        "coverage_name": "All areas (SAAR)",
        "coverage_desc": "Every area including rural, seasonally adjusted at annual rates. Broadest universe, but the LEVEL is not comparable to raw monthly counts - it is an annualised rate.",
        "is_seasonally_adj": True,
        "is_annualised": True,
        "is_default": False,
        "source_alias": "housing_starts_provincial_saar",
    },
    {
        "coverage_name": "Centres 10,000 and over",
        "coverage_desc": "Urban centres of 10,000+ population, at Canada / province / selected CMA level. Raw monthly counts back to 1948. Default dashboard view.",
        "is_seasonally_adj": False,
        "is_annualised": False,
        "is_default": True,
        "source_alias": "housing_activity_centres_10k",
    },
    {
        "coverage_name": "Centres 50,000 and over",
        "coverage_desc": "Urban centres of 50,000+ population, Canada and provinces. A subset of the 10,000+ universe.",
        "is_seasonally_adj": False,
        "is_annualised": False,
        "is_default": False,
        "source_alias": "housing_activity_centres_50k",
    },
    {
        "coverage_name": "Census metropolitan areas",
        "coverage_desc": "Selected census metropolitan areas. Finest geographic detail; CMAs do not tile a province, so they do not sum to a provincial total.",
        "is_seasonally_adj": False,
        "is_annualised": False,
        "is_default": False,
        "source_alias": "housing_activity_cma",
    },
]

STAGES = [
    ("Housing starts", "Starts", 1, True),
    ("Housing under construction", "Under construction", 2, False),
    ("Housing completions", "Completions", 3, True),
]

# CMHC uses several label variants for the same dwelling concept across sibling
# tables. dwelling_category is the conformed grouping analysts actually slice by.
DWELLING_CATEGORY = {
    "single-detached units": ("Single-detached", False, 1),
    "single detached units": ("Single-detached", False, 1),
    "semi-detached units": ("Semi-detached", False, 2),
    "row units": ("Row", False, 3),
    "apartment and other units": ("Apartment & other", False, 4),
    "apartment and other unit types": ("Apartment & other", False, 4),
    "row, apartment and other units": ("Row + apartment", False, 5),
    "single-detached and semi-detached units": ("Single + semi", False, 6),
    "multiples": ("Multiples", False, 7),
    "total units": ("Total", True, 9),
}


# --------------------------------------------------------------------------- #
# dim_date
# --------------------------------------------------------------------------- #
def build_dim_date(con: duckdb.DuckDBPyConnection, start: str, end: str) -> int:
    """Month-grain date dimension spanning the full range present in the facts."""
    periods = pd.period_range(start=start, end=end, freq="M")
    rows = []
    for p in periods:
        first = p.to_timestamp().date()
        last_day = calendar.monthrange(p.year, p.month)[1]
        # Canadian federal fiscal year: 1 April - 31 March, named by end year.
        fy = p.year + 1 if p.month >= 4 else p.year
        fq = ((p.month - 4) % 12) // 3 + 1
        rows.append(
            {
                "date_key": int(f"{p.year}{p.month:02d}01"),
                "date": first,
                "year_month": f"{p.year}-{p.month:02d}",
                "year": p.year,
                "quarter": (p.month - 1) // 3 + 1,
                "quarter_name": f"{p.year} Q{(p.month - 1) // 3 + 1}",
                "month": p.month,
                "month_name": calendar.month_name[p.month],
                "month_abbr": calendar.month_abbr[p.month],
                "month_end_date": first.replace(day=last_day),
                "days_in_month": last_day,
                "fiscal_year": fy,
                "fiscal_quarter": fq,
                "is_current_month": False,
            }
        )

    df = pd.DataFrame(rows)
    df.loc[df.index[-1], "is_current_month"] = True

    con.register("_dim_date_src", df)
    con.execute("DELETE FROM dw.dim_date")
    con.execute("INSERT INTO dw.dim_date SELECT * FROM _dim_date_src")
    con.unregister("_dim_date_src")

    log.info("dim_date: %d months (%s .. %s)", len(df), df.year_month.iloc[0], df.year_month.iloc[-1])
    return len(df)


# --------------------------------------------------------------------------- #
# dim_geography
# --------------------------------------------------------------------------- #
def _distinct_geo_labels(con: duckdb.DuckDBPyConnection) -> list[str]:
    labels: set[str] = set()
    for spec in config()["statcan"]["tables"]:
        path = RAW_DIR / spec["alias"] / f"{spec['pid']}.csv"
        if not path.exists():
            continue
        cols = con.sql(
            f"SELECT * FROM read_csv('{path.as_posix()}', header=true, "
            f"all_varchar=true, sample_size=-1) LIMIT 0"
        ).description
        if "GEO" not in {c[0] for c in cols}:
            continue
        rows = con.sql(
            f"SELECT DISTINCT GEO FROM read_csv('{path.as_posix()}', header=true, "
            f"all_varchar=true, sample_size=-1) WHERE GEO IS NOT NULL"
        ).fetchall()
        labels.update(r[0] for r in rows)
    return sorted(labels)


def build_dim_geography(con: duckdb.DuckDBPyConnection) -> int:
    """
    Every published GEO label, classified.

    CBA arrears regions deliberately do NOT live here - they are a different
    grain and got their own dim_arrears_region. Keeping them here produced two
    members named 'Ontario' (a province and a CBA region), which would surface
    as two identical entries in one geography slicer.

    dim_geography.cba_region survives as the bridge ATTRIBUTE, so filtering to
    Nova Scotia can still resolve which arrears region to show.
    """
    records = []

    for name in _distinct_geo_labels(con):
        attrs = classify(name)
        records.append({"geo_name": name, "dguid": None, **attrs})

    df = pd.DataFrame(records).drop_duplicates(subset=["geo_name", "geo_level"])
    df = df.sort_values(["sort_order", "geo_name"]).reset_index(drop=True)
    df.insert(0, "geography_key", range(1, len(df) + 1))

    unknown = pd.DataFrame([{
        "geography_key": UNKNOWN_KEY, "geo_name": "Unknown", "geo_level": "Unknown",
        "dguid": None, "province_code": None, "province_name": None,
        "cba_region": None, "is_aggregate": False, "sort_order": 9999,
    }])
    df = pd.concat([unknown, df], ignore_index=True)

    cols = ["geography_key", "geo_name", "geo_level", "dguid", "province_code",
            "province_name", "cba_region", "is_aggregate", "sort_order"]
    con.register("_dim_geo_src", df[cols])
    con.execute("DELETE FROM dw.dim_geography")
    con.execute("INSERT INTO dw.dim_geography SELECT * FROM _dim_geo_src")
    con.unregister("_dim_geo_src")

    by_level = df.groupby("geo_level").size().to_dict()
    n_agg = int(df.is_aggregate.sum())
    log.info("dim_geography: %d members %s, %d flagged is_aggregate", len(df), by_level, n_agg)
    return len(df)


# --------------------------------------------------------------------------- #
# dim_arrears_region - the CBA delinquency grain
# --------------------------------------------------------------------------- #
# covers_provinces is documentation the user can SEE. A reader filtering to
# Nova Scotia and getting an 'Atlantic' arrears figure needs to know why, and
# putting it in the dimension beats putting it in a footnote nobody opens.
ARREARS_REGIONS = [
    ("Canada",           "CANADA",           "All provinces and territories", True,  0),
    ("Atlantic",         "ATLANTIC",         "NL, PE, NS, NB",                False, 1),
    ("Quebec",           "QUEBEC",           "QC",                            False, 2),
    ("Ontario",          "ONTARIO",          "ON",                            False, 3),
    ("Manitoba",         "MANITOBA",         "MB",                            False, 4),
    ("Saskatchewan",     "SASKATCHEWAN",     "SK",                            False, 5),
    ("Alberta",          "ALBERTA",          "AB (incl. NT, NU)",             False, 6),
    ("British Columbia", "BRITISH COLUMBIA", "BC (incl. YT)",                 False, 7),
    ("Territories",      "TERRITORIES",      "YT, NT, NU (reported separately)", False, 8),
]


def build_dim_arrears_region(con: duckdb.DuckDBPyConnection) -> int:
    df = pd.DataFrame(
        ARREARS_REGIONS,
        columns=["region_name", "region_code", "covers_provinces", "is_national", "sort_order"],
    )
    df.insert(0, "arrears_region_key", range(1, len(df) + 1))
    unknown = pd.DataFrame([{
        "arrears_region_key": UNKNOWN_KEY, "region_name": "Unknown",
        "region_code": "UNKNOWN", "covers_provinces": None,
        "is_national": False, "sort_order": 999,
    }])
    df = pd.concat([unknown, df], ignore_index=True)

    con.register("_arr", df)
    con.execute("DELETE FROM dw.dim_arrears_region")
    con.execute("INSERT INTO dw.dim_arrears_region SELECT * FROM _arr")
    con.unregister("_arr")
    log.info("dim_arrears_region: %d members", len(df))
    return len(df)


# --------------------------------------------------------------------------- #
# Small seeded dimensions
# --------------------------------------------------------------------------- #
def build_dim_coverage(con: duckdb.DuckDBPyConnection) -> int:
    df = pd.DataFrame(COVERAGES).drop(columns=["source_alias"])
    df.insert(0, "coverage_key", range(1, len(df) + 1))
    unknown = pd.DataFrame([{
        "coverage_key": UNKNOWN_KEY, "coverage_name": "Unknown",
        "coverage_desc": "Unresolved coverage", "is_seasonally_adj": False,
        "is_annualised": False, "is_default": False,
    }])
    df = pd.concat([unknown, df], ignore_index=True)

    con.register("_cov", df)
    con.execute("DELETE FROM dw.dim_coverage")
    con.execute("INSERT INTO dw.dim_coverage SELECT * FROM _cov")
    con.unregister("_cov")
    log.info("dim_coverage: %d members", len(df))
    return len(df)


def build_dim_construction_stage(con: duckdb.DuckDBPyConnection) -> int:
    df = pd.DataFrame(STAGES, columns=["stage_name", "stage_short", "stage_order", "is_flow"])
    df.insert(0, "stage_key", range(1, len(df) + 1))
    unknown = pd.DataFrame([{
        "stage_key": UNKNOWN_KEY, "stage_name": "Unknown",
        "stage_short": "Unknown", "stage_order": 9, "is_flow": False,
    }])
    df = pd.concat([unknown, df], ignore_index=True)

    con.register("_stage", df)
    con.execute("DELETE FROM dw.dim_construction_stage")
    con.execute("INSERT INTO dw.dim_construction_stage SELECT * FROM _stage")
    con.unregister("_stage")
    log.info("dim_construction_stage: %d members", len(df))
    return len(df)


def build_dim_dwelling_type(con: duckdb.DuckDBPyConnection) -> int:
    """Distinct dwelling labels across all CMHC tables, mapped to a conformed category."""
    labels: set[str] = set()
    for spec in config()["statcan"]["tables"]:
        path = RAW_DIR / spec["alias"] / f"{spec['pid']}.csv"
        if not path.exists():
            continue
        cols = {c[0] for c in con.sql(
            f"SELECT * FROM read_csv('{path.as_posix()}', header=true, "
            f"all_varchar=true, sample_size=-1) LIMIT 0"
        ).description}
        col = next((c for c in cols if "type of" in c.lower() and "unit" in c.lower()), None)
        if not col:
            continue
        rows = con.sql(
            f'SELECT DISTINCT "{col}" FROM read_csv(\'{path.as_posix()}\', header=true, '
            f'all_varchar=true, sample_size=-1) WHERE "{col}" IS NOT NULL'
        ).fetchall()
        labels.update(r[0] for r in rows)

    records = []
    for name in sorted(labels):
        cat, is_total, order = DWELLING_CATEGORY.get(
            name.strip().lower(), (name.strip(), False, 50)
        )
        records.append({
            "dwelling_type_name": name, "dwelling_category": cat,
            "is_total": is_total, "sort_order": order,
        })

    df = pd.DataFrame(records).sort_values(["sort_order", "dwelling_type_name"]).reset_index(drop=True)
    df.insert(0, "dwelling_type_key", range(1, len(df) + 1))
    unknown = pd.DataFrame([{
        "dwelling_type_key": UNKNOWN_KEY, "dwelling_type_name": "Unknown",
        "dwelling_category": "Unknown", "is_total": False, "sort_order": 999,
    }])
    df = pd.concat([unknown, df], ignore_index=True)

    con.register("_dwell", df)
    con.execute("DELETE FROM dw.dim_dwelling_type")
    con.execute("INSERT INTO dw.dim_dwelling_type SELECT * FROM _dwell")
    con.unregister("_dwell")

    unmapped = [r["dwelling_type_name"] for r in records if r["sort_order"] == 50]
    if unmapped:
        log.warning("dim_dwelling_type: %d labels not in DWELLING_CATEGORY: %s",
                    len(unmapped), unmapped)
    log.info("dim_dwelling_type: %d members", len(df))
    return len(df)


def build_dim_price_component(con: duckdb.DuckDBPyConnection) -> int:
    order = {"Total (house and land)": 1, "House only": 2, "Land only": 3}
    spec = next(s for s in config()["statcan"]["tables"] if s["alias"] == "new_housing_price_index")
    path = RAW_DIR / spec["alias"] / f"{spec['pid']}.csv"

    rows = con.sql(
        f'SELECT DISTINCT "New housing price indexes" FROM read_csv(\'{path.as_posix()}\', '
        f'header=true, all_varchar=true, sample_size=-1)'
    ).fetchall()
    names = sorted((r[0] for r in rows if r[0]), key=lambda n: order.get(n, 9))

    df = pd.DataFrame({"component_name": names})
    df["sort_order"] = df.component_name.map(lambda n: order.get(n, 9))
    df.insert(0, "price_component_key", range(1, len(df) + 1))
    unknown = pd.DataFrame([{
        "price_component_key": UNKNOWN_KEY, "component_name": "Unknown", "sort_order": 99,
    }])
    df = pd.concat([unknown, df], ignore_index=True)

    con.register("_pc", df)
    con.execute("DELETE FROM dw.dim_price_component")
    con.execute("INSERT INTO dw.dim_price_component SELECT * FROM _pc")
    con.unregister("_pc")
    log.info("dim_price_component: %d members", len(df))
    return len(df)


# --------------------------------------------------------------------------- #
# dim_credit_product - parses the Bank of Canada component strings
# --------------------------------------------------------------------------- #
def _parse_credit_component(name: str) -> dict:
    """
    Decompose a BoC 'Components' label into orthogonal attributes.

    Example input:
      'Fixed rate, funds advanced, residential mortgages, insured, 5 years and over'

    The publisher encodes five independent facets in one comma-delimited string.
    Splitting them into columns is what turns 51 opaque strings into a dimension
    a business user can actually slice - "show me uninsured variable-rate
    originations" becomes two slicer clicks instead of a text filter.
    """
    low = name.lower()

    rate_type = (
        "Fixed" if "fixed rate" in low
        else "Variable" if "variable rate" in low
        else "Total"
    )
    lending_stage = (
        "Funds advanced" if "funds advanced" in low
        else "Outstanding balance" if "outstanding" in low
        else None
    )
    insurance_status = (
        "Insured" if "insured" in low and "uninsured" not in low
        else "Uninsured" if "uninsured" in low
        else "Total"
    )
    if "residential mortgage" in low:
        family = "Residential mortgage"
    elif "personal loan" in low or "personal line" in low:
        family = "Personal credit"
    elif "credit card" in low:
        family = "Credit card"
    else:
        family = "Other"

    if "less than 1 year" in low or "under 1 year" in low:
        term = "< 1 year"
    elif "1 to 3 year" in low:
        term = "1-3 years"
    elif "3 to 5 year" in low:
        term = "3-5 years"
    elif "5 years and over" in low or "5 year and over" in low:
        term = "5+ years"
    elif "variable" in low:
        term = "Variable"
    else:
        term = "Total"

    return {
        "product_family": family,
        "rate_type": rate_type,
        "insurance_status": insurance_status,
        "term_band": term,
        "lending_stage": lending_stage,
        "lender_type": None,
    }


# Which metadata dimension carries the credit product in each source table.
# (COORDINATE is a dot-separated tuple of member IDs in dimension order.)
CREDIT_SOURCES = [
    ("boc_lending", "10100006", 2),
    ("household_credit_liabilities", "36100639", 3),
]


def build_dim_credit_product(con: duckdb.DuckDBPyConnection) -> int:
    """
    Build from the METADATA member tree, not from the data file's labels.

    The data CSV only carries the leaf label, which is ambiguous - 'Non-banks'
    appears six times in 36-10-0639 under different parents. Sourcing the
    dimension from *_MetaData.csv recovers member_id and the full ancestry, so
    the six 'Non-banks' become six distinct, correctly-named members:

        Non-mortgage loans > Non-banks
        Mortgage loans > Residential mortgages > Non-banks
        ...

    is_leaf is carried through so measures can exclude publisher subtotals; the
    BoC table stores 'Total, funds advanced, ...' as a row beside its children.
    """
    records: list[dict] = []

    for alias, pid, dim_id in CREDIT_SOURCES:
        meta_path = RAW_DIR / alias / f"{pid}_MetaData.csv"
        if not meta_path.exists():
            log.warning("dim_credit_product: metadata missing for %s", alias)
            continue

        members = dimension_members(meta_path).get(dim_id)
        if members is None or members.empty:
            log.warning("dim_credit_product: dimension %d absent in %s", dim_id, alias)
            continue

        for row in members.itertuples(index=False):
            # Parse attributes from the FULL PATH, not the leaf label - the path
            # is what carries 'residential mortgages' / 'insured' / term band.
            attrs = _parse_credit_component(row.hierarchy_path)
            if alias == "household_credit_liabilities":
                # Here the member names a lender or instrument, not a product.
                attrs["lender_type"] = row.member_name
            records.append({
                "source_alias": alias,
                "member_id": int(row.member_id),
                "component_name": row.member_name,
                "hierarchy_path": row.hierarchy_path,
                "parent_name": row.parent_name,
                "root_name": row.root_name,
                "depth": int(row.depth),
                "is_leaf": bool(row.is_leaf),
                **attrs,
            })

    df = pd.DataFrame(records).sort_values(["source_alias", "member_id"]).reset_index(drop=True)
    df.insert(0, "credit_product_key", range(1, len(df) + 1))

    unknown = pd.DataFrame([{
        "credit_product_key": UNKNOWN_KEY, "source_alias": "unknown", "member_id": -1,
        "component_name": "Unknown", "hierarchy_path": None, "parent_name": None,
        "root_name": None, "depth": 0, "is_leaf": True, "product_family": None,
        "rate_type": None, "insurance_status": None, "term_band": None,
        "lending_stage": None, "lender_type": None,
    }])
    df = pd.concat([unknown, df], ignore_index=True)

    cols = ["credit_product_key", "source_alias", "member_id", "component_name",
            "hierarchy_path", "parent_name", "root_name", "depth", "is_leaf",
            "product_family", "rate_type", "insurance_status", "term_band",
            "lending_stage", "lender_type"]
    con.register("_cp", df[cols])
    con.execute("DELETE FROM dw.dim_credit_product")
    con.execute("INSERT INTO dw.dim_credit_product SELECT * FROM _cp")
    con.unregister("_cp")

    n_leaf = int(df.is_leaf.sum())
    log.info("dim_credit_product: %d members (%d leaf, %d publisher subtotals)",
             len(df), n_leaf, len(df) - n_leaf)
    return len(df)


# --------------------------------------------------------------------------- #
# dim_source - lineage
# --------------------------------------------------------------------------- #
OPEN_LICENCE = "Statistics Canada Open Licence"


def build_dim_source(con: duckdb.DuckDBPyConnection) -> int:
    """Read the extract manifest so every fact row can name its provenance."""
    manifest_path = RAW_DIR / "_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8")) if manifest_path.exists() else {}

    records = []
    for alias, entry in sorted(manifest.items()):
        publisher = entry.get("publisher") or "Statistics Canada"
        records.append({
            "source_alias": alias,
            "source_table": entry.get("table"),
            "publisher": publisher,
            "source_url": entry.get("source_url"),
            "licence": OPEN_LICENCE if "Bankers" not in publisher else "CBA published statistics",
            "sha256": entry.get("sha256"),
            "extracted_utc": entry.get("extracted_at"),
        })

    df = pd.DataFrame(records)
    if df.empty:
        df = pd.DataFrame(columns=["source_alias", "source_table", "publisher",
                                   "source_url", "licence", "sha256", "extracted_utc"])
    df.insert(0, "source_key", range(1, len(df) + 1))
    unknown = pd.DataFrame([{
        "source_key": UNKNOWN_KEY, "source_alias": "unknown", "source_table": None,
        "publisher": "Unknown", "source_url": None, "licence": None,
        "sha256": None, "extracted_utc": None,
    }])
    df = pd.concat([unknown, df], ignore_index=True)
    df["extracted_utc"] = pd.to_datetime(df["extracted_utc"], errors="coerce", utc=True).dt.tz_localize(None)

    con.register("_src", df)
    con.execute("DELETE FROM dw.dim_source")
    con.execute("INSERT INTO dw.dim_source SELECT * FROM _src")
    con.unregister("_src")
    log.info("dim_source: %d members", len(df))
    return len(df)
