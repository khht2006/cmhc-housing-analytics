"""
Unit tests for the geography classifier.

These are pinned to the exact labels StatCan publishes today. If CMHC renames a
place in a future vintage, these tests fail loudly rather than the warehouse
silently reclassifying a CMA as Unknown and dropping it out of provincial totals.
"""

from src.transform.geography import classify, normalise


def test_canada_is_an_aggregate():
    got = classify("Canada")
    assert got["geo_level"] == "Country"
    assert got["is_aggregate"] is True


def test_provinces_are_leaves():
    for name in ["Ontario", "Quebec", "British Columbia", "Prince Edward Island"]:
        got = classify(name)
        assert got["geo_level"] == "Province", name
        assert got["is_aggregate"] is False, name
        assert got["province_code"] is not None, name


def test_province_maps_to_cba_region():
    assert classify("Nova Scotia")["cba_region"] == "ATLANTIC"
    assert classify("New Brunswick")["cba_region"] == "ATLANTIC"
    assert classify("Alberta")["cba_region"] == "ALBERTA"
    # CBA folds the territories into other regions - the model must reflect that.
    assert classify("Yukon")["cba_region"] == "BRITISH COLUMBIA"
    assert classify("Nunavut")["cba_region"] == "ALBERTA"


def test_multi_province_regions_are_aggregates():
    for name in ["Atlantic provinces", "Atlantic Region", "Prairie provinces", "Prairie Region"]:
        assert classify(name)["is_aggregate"] is True, name


def test_cmas_resolve_to_their_province():
    cases = {
        "Toronto, Ontario": "ON",
        "Vancouver, British Columbia": "BC",
        "Calgary, Alberta": "AB",
        "St. John's, Newfoundland and Labrador": "NL",
        "Kitchener-Cambridge-Waterloo, Ontario": "ON",
        "Trois-Rivières, Quebec": "QC",
        "Charlottetown, Prince Edward Island": "PE",
    }
    for name, code in cases.items():
        got = classify(name)
        assert got["geo_level"] == "CMA", name
        assert got["province_code"] == code, f"{name} -> {got['province_code']}"
        assert got["is_aggregate"] is False, name


def test_split_cma_parts_resolve_but_the_combined_row_is_an_aggregate():
    """The Ottawa-Gatineau trap: three rows, one of which is the sum of the other two."""
    on_part = classify("Ottawa-Gatineau, Ontario part, Ontario/Quebec")
    qc_part = classify("Ottawa-Gatineau, Quebec part, Ontario/Quebec")
    combined = classify("Ottawa-Gatineau, Ontario/Quebec")

    assert on_part["province_code"] == "ON"
    assert on_part["is_aggregate"] is False
    assert qc_part["province_code"] == "QC"
    assert qc_part["is_aggregate"] is False

    # If this ever flips to False, every Ontario CMA total silently inflates.
    assert combined["is_aggregate"] is True


def test_residual_complements_are_aggregates():
    for name in [
        "British Columbia excluding Vancouver",
        "Ontario excluding Toronto",
        "Manitoba excluding Winnipeg",
        "Montréal excluding Saint-Jérôme, Quebec",
        "Quebec excluding Montréal as of 1991 census",
    ]:
        got = classify(name)
        assert got["is_aggregate"] is True, name


def test_multi_cma_grouping_is_an_aggregate():
    got = classify("Saint John, Fredericton, and Moncton, New Brunswick")
    assert got["is_aggregate"] is True
    assert got["province_code"] == "NB"


def test_universe_labels_are_aggregates():
    for name in [
        "Census metropolitan areas",
        "Census agglomerations 50,000 and over",
        "Census metropolitan areas and census agglomerations of 50,000 and over",
    ]:
        assert classify(name)["is_aggregate"] is True, name


def test_unknown_label_is_not_silently_bucketed():
    got = classify("Atlantis, Underwater")
    assert got["geo_level"] == "Unknown"
    assert got["province_code"] is None


def test_normalise_folds_cmhc_vintage_variants():
    a = normalise("Montréal excluding Saint-Jérôme, Quebec")
    b = normalise("Montreal excluding Saint-Jerome, Quebec")
    assert a == b
