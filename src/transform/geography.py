"""
Geography classification - the highest-risk transform in the project.

StatCan publishes 90 distinct GEO labels across the housing tables, and they are
NOT a clean hierarchy. Mixed into the leaf places are:

  * national and multi-province roll-ups   'Canada', 'Atlantic provinces'
  * residual complements                   'British Columbia excluding Vancouver'
  * universe labels                        'Census metropolitan areas'
  * split CMAs and their own total         'Ottawa-Gatineau, Ontario part, Ontario/Quebec'
                                           'Ottawa-Gatineau, Quebec part,  Ontario/Quebec'
                                           'Ottawa-Gatineau, Ontario/Quebec'   <- sum of the two
  * multi-CMA groupings                    'Saint John, Fredericton, and Moncton, New Brunswick'
  * vintage variants of one place          'Montréal excluding Saint-Jérôme, Quebec'
                                           'Montréal excluding St-Jérôme, Quebec'

Summing VALUE without classifying these roughly doubles every provincial total.
`is_aggregate` is therefore the single most important column in the warehouse:
it means "this row is a roll-up of other rows in the same source, exclude it
from leaf sums". Facts still load aggregate rows, because they are the
publisher's own figures and serve as the independent control totals that the
reconciliation suite checks leaf sums against.

This is done in Python rather than a SQL CASE expression because it is rule-based
string parsing with unit tests attached (tests/test_geography.py), and a 90-branch
CASE would be unreviewable.
"""

from __future__ import annotations

import re
import unicodedata

PROVINCES: dict[str, str] = {
    "Newfoundland and Labrador": "NL",
    "Prince Edward Island": "PE",
    "Nova Scotia": "NS",
    "New Brunswick": "NB",
    "Quebec": "QC",
    "Ontario": "ON",
    "Manitoba": "MB",
    "Saskatchewan": "SK",
    "Alberta": "AB",
    "British Columbia": "BC",
}

# Territories appear in some tables; CBA folds them into other regions.
TERRITORIES: dict[str, str] = {
    "Yukon": "YT",
    "Northwest Territories": "NT",
    "Nunavut": "NU",
}

# Province -> CBA arrears reporting region. This is the roll-up that lets the
# coarse-grained delinquency fact coexist with fine-grained housing facts.
# Note CBA folds YT into British Columbia and NT/NU into Alberta.
CBA_REGION_BY_PROVINCE: dict[str, str] = {
    "NL": "ATLANTIC", "PE": "ATLANTIC", "NS": "ATLANTIC", "NB": "ATLANTIC",
    "QC": "QUEBEC",
    "ON": "ONTARIO",
    "MB": "MANITOBA",
    "SK": "SASKATCHEWAN",
    "AB": "ALBERTA", "NT": "ALBERTA", "NU": "ALBERTA",
    "BC": "BRITISH COLUMBIA", "YT": "BRITISH COLUMBIA",
}

# Labels that are explicitly roll-ups or statistical universes, not places.
KNOWN_AGGREGATES: set[str] = {
    "Canada",
    "Atlantic provinces",
    "Atlantic Region",
    "Prairie provinces",
    "Prairie Region",
    "Census metropolitan areas",
    "Census agglomerations 50,000 and over",
    "Census metropolitan areas and census agglomerations of 50,000 and over",
}

MULTI_PROVINCE_REGIONS: dict[str, str] = {
    "Atlantic provinces": "ATLANTIC",
    "Atlantic Region": "ATLANTIC",
}

_ACCENT_FOLD = str.maketrans({"é": "e", "è": "e", "ê": "e", "É": "E", "à": "a", "ô": "o"})


def normalise(name: str) -> str:
    """Fold accents and collapse whitespace, for matching only - never stored.

    'Montréal excluding St-Jérôme' and 'Montréal excluding Saint-Jérôme' are the
    same place under two CMHC vintages; folding lets the dedupe logic see that.
    """
    folded = unicodedata.normalize("NFKD", name)
    folded = "".join(c for c in folded if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", folded).strip().lower()


def _province_from_segments(name: str) -> str | None:
    """Resolve the province a 'City, Province' style label belongs to."""
    # 'Ottawa-Gatineau, Ontario part, Ontario/Quebec' -> the 'X part' wins.
    part = re.search(r",\s*([A-Za-zÀ-ÿ ]+?)\s+part\s*,", name)
    if part and part.group(1).strip() in PROVINCES:
        return part.group(1).strip()

    # Otherwise the trailing segment is the province, if it is one.
    tail = name.rsplit(",", 1)[-1].strip()
    if tail in PROVINCES:
        return tail

    # Fall back to any province named anywhere in the label
    # ('Montréal excluding Saint-Jérôme, Quebec').
    for prov in PROVINCES:
        if re.search(rf"\b{re.escape(prov)}\b", name):
            return prov
    return None


def classify(geo_name: str) -> dict:
    """
    Map a published GEO label to its warehouse dimension attributes.

    Returns keys: geo_level, province_name, province_code, cba_region,
    is_aggregate, sort_order.
    """
    name = geo_name.strip()

    # --- 1. National total -------------------------------------------------
    if name == "Canada":
        return _row("Country", None, None, is_aggregate=True, sort_order=0)

    # --- 2. Named roll-ups and statistical universes ------------------------
    if name in KNOWN_AGGREGATES:
        cba = MULTI_PROVINCE_REGIONS.get(name)
        return _row("Region", None, cba, is_aggregate=True, sort_order=10)

    # --- 3. Exact province match -------------------------------------------
    if name in PROVINCES:
        code = PROVINCES[name]
        return _row(
            "Province", name, CBA_REGION_BY_PROVINCE[code],
            is_aggregate=False, sort_order=20, province_code=code,
        )

    if name in TERRITORIES:
        code = TERRITORIES[name]
        return _row(
            "Province", name, CBA_REGION_BY_PROVINCE[code],
            is_aggregate=False, sort_order=25, province_code=code,
        )

    # --- 4. Residual complements: 'X excluding Y' --------------------------
    # These overlap their parent province, so they must never enter a leaf sum.
    if " excluding " in name:
        prov = _province_from_segments(name)
        code = PROVINCES.get(prov) if prov else None
        return _row(
            "Region", prov, CBA_REGION_BY_PROVINCE.get(code) if code else None,
            is_aggregate=True, sort_order=30, province_code=code,
        )

    # --- 5. Multi-CMA groupings: 'A, B, and C, Province' -------------------
    if re.search(r",\s*and\s+", name):
        prov = _province_from_segments(name)
        code = PROVINCES.get(prov) if prov else None
        return _row(
            "Region", prov, CBA_REGION_BY_PROVINCE.get(code) if code else None,
            is_aggregate=True, sort_order=35, province_code=code,
        )

    # --- 6. Census metropolitan areas --------------------------------------
    # A label only earns 'CMA' if its province actually resolves. Accepting any
    # comma-containing string here would file an unmapped label as a CMA with a
    # NULL province, which is precisely the silent bucketing this module exists
    # to prevent - it would vanish from provincial roll-ups without warning.
    prov = _province_from_segments(name)
    if "," in name and prov is not None:
        tail = name.rsplit(",", 1)[-1].strip()

        # 'Ottawa-Gatineau, Ontario/Quebec' is the SUM of the two 'X part' rows
        # that also appear in the same table - an aggregate hiding as a CMA.
        is_combined = "/" in tail and " part" not in name
        code = PROVINCES.get(prov)

        return _row(
            "CMA", prov, CBA_REGION_BY_PROVINCE.get(code) if code else None,
            is_aggregate=is_combined, sort_order=40, province_code=code,
        )

    # --- 7. Anything unrecognised --------------------------------------------
    # Deliberately NOT silently bucketed as a CMA: an unmapped label should show
    # up in the quality report, not quietly inflate a provincial total.
    return _row("Unknown", None, None, is_aggregate=False, sort_order=999)


def _row(
    geo_level: str,
    province_name: str | None,
    cba_region: str | None,
    *,
    is_aggregate: bool,
    sort_order: int,
    province_code: str | None = None,
) -> dict:
    return {
        "geo_level": geo_level,
        "province_name": province_name,
        "province_code": province_code,
        "cba_region": cba_region,
        "is_aggregate": bool(is_aggregate),
        "sort_order": sort_order,
    }
