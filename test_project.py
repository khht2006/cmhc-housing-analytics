"""
Tests.

Run:  python -m pytest test_project.py -v

Two groups:

  1. Tests for geography.py - the place-name sorting. These run on their own
     and are the ones worth reading first, because they show exactly which
     tricky names the code has to handle.

  2. Tests on the built database - these check the data came out right. They
     skip themselves if you haven't run the pipeline yet.
"""

import duckdb
import pytest

from config import WAREHOUSE_FILE
from geography import classify


# =============================================================================
# 1. Place names
# =============================================================================

def test_canada_is_a_total():
    """Canada is the sum of the provinces, so it must never be added to them."""
    result = classify("Canada")
    assert result["geo_level"] == "Country"
    assert result["is_aggregate"] is True


def test_provinces_are_real_places():
    for name in ["Ontario", "Quebec", "British Columbia", "Prince Edward Island"]:
        result = classify(name)
        assert result["geo_level"] == "Province", name
        assert result["is_aggregate"] is False, name
        assert result["province_code"] is not None, name


def test_cities_get_matched_to_their_province():
    cities = {
        "Toronto, Ontario": "ON",
        "Vancouver, British Columbia": "BC",
        "Calgary, Alberta": "AB",
        "St. John's, Newfoundland and Labrador": "NL",   # has an apostrophe
        "Trois-Rivières, Quebec": "QC",                  # has an accent
        "Kitchener-Cambridge-Waterloo, Ontario": "ON",   # has dashes
    }
    for name, expected_code in cities.items():
        result = classify(name)
        assert result["geo_level"] == "CMA", name
        assert result["province_code"] == expected_code, name
        assert result["is_aggregate"] is False, name


def test_the_ottawa_gatineau_trap():
    """
    Ottawa-Gatineau sits on the Ontario/Quebec border, so it appears as THREE
    rows: the Ontario half, the Quebec half, and both halves together.

    The two halves are real places. The combined row is a total of them. If we
    got this wrong, every Ontario city total would be too big.
    """
    ontario_half = classify("Ottawa-Gatineau, Ontario part, Ontario/Quebec")
    quebec_half = classify("Ottawa-Gatineau, Quebec part, Ontario/Quebec")
    both = classify("Ottawa-Gatineau, Ontario/Quebec")

    assert ontario_half["province_code"] == "ON"
    assert ontario_half["is_aggregate"] is False

    assert quebec_half["province_code"] == "QC"
    assert quebec_half["is_aggregate"] is False

    assert both["is_aggregate"] is True     # this is the one that matters


def test_totals_are_recognised_as_totals():
    """Every one of these overlaps something else, so all must be excluded."""
    totals = [
        "Atlantic provinces",                     # NL + PE + NS + NB
        "Prairie provinces",
        "British Columbia excluding Vancouver",   # part of BC
        "Ontario excluding Toronto",
        "Census metropolitan areas",              # all the metro areas at once
        "Saint John, Fredericton, and Moncton, New Brunswick",   # three cities
    ]
    for name in totals:
        assert classify(name)["is_aggregate"] is True, name


def test_provinces_map_to_the_right_arrears_region():
    """The CBA lumps the four Atlantic provinces together."""
    assert classify("Nova Scotia")["arrears_region"] == "ATLANTIC"
    assert classify("New Brunswick")["arrears_region"] == "ATLANTIC"
    assert classify("Ontario")["arrears_region"] == "ONTARIO"


def test_unknown_names_are_not_guessed_at():
    """
    A name we don't recognise must come back as Unknown, NOT be quietly
    treated as a city. If it were treated as a city with no province, it would
    silently vanish from every provincial total and nobody would notice.
    """
    result = classify("Atlantis, Underwater")
    assert result["geo_level"] == "Unknown"
    assert result["province_code"] is None


# =============================================================================
# 2. The built database
# =============================================================================

@pytest.fixture(scope="module")
def db():
    if not WAREHOUSE_FILE.exists():
        pytest.skip("Run 'python run_all.py' first to build the database")
    connection = duckdb.connect(str(WAREHOUSE_FILE), read_only=True)
    yield connection
    connection.close()


def count(db, query):
    return db.sql(query).fetchone()[0]


def test_no_duplicate_rows_in_the_main_fact_table(db):
    """
    Each combination of month + place + house type + stage + coverage should
    appear exactly once. Duplicates would mean we loaded a file twice, and
    every number on the dashboard would be doubled.
    """
    duplicates = count(db, """
        SELECT count(*) FROM (
            SELECT date_key, geography_key, dwelling_type_key, stage_key, coverage_key
            FROM fact_housing_activity
            GROUP BY ALL HAVING count(*) > 1
        )
    """)
    assert duplicates == 0


def test_every_fact_points_at_a_real_dimension_row(db):
    """No fact should reference a dimension row that doesn't exist."""
    orphans = count(db, """
        SELECT count(*) FROM fact_housing_activity f
        LEFT JOIN dim_geography g ON g.geography_key = f.geography_key
        WHERE g.geography_key IS NULL
    """)
    assert orphans == 0


def test_nothing_landed_on_the_unknown_row(db):
    """
    We used LEFT JOIN when building the facts, so anything we couldn't match
    ends up on the Unknown row (-1) rather than disappearing. There should be
    none - if this fails, geography.py needs a new rule.
    """
    unknowns = count(db, """
        SELECT count(*) FROM fact_housing_activity WHERE geography_key = -1
    """)
    assert unknowns == 0


def test_totals_are_flagged_in_the_database(db):
    """Same check as the geography tests, but on the data that actually loaded."""
    for name in ["Canada", "Atlantic provinces", "Ottawa-Gatineau, Ontario/Quebec"]:
        row = db.sql("SELECT is_aggregate FROM dim_geography WHERE geo_name = ?",
                     params=[name]).fetchone()
        if row is None:
            continue     # this name isn't in the current data
        assert row[0] is True, f"{name} should be flagged as a total"


def test_no_two_places_share_a_name(db):
    """
    Two rows with the same name would show up twice in a Power BI dropdown,
    which is confusing and usually means two different things got mixed into
    one table.
    """
    duplicates = count(db, """
        SELECT count(*) FROM (
            SELECT geo_name FROM dim_geography GROUP BY 1 HAVING count(*) > 1
        )
    """)
    assert duplicates == 0


def test_all_ten_provinces_are_there(db):
    found = count(db, """
        SELECT count(DISTINCT province_code) FROM dim_geography
        WHERE geo_level = 'Province' AND province_code IS NOT NULL
    """)
    assert found == 10


def test_hidden_arrears_are_null_not_zero(db):
    """
    The CBA hides the Territories' arrears count because the numbers are small
    enough to identify people. We store NULL. If we stored 0, it would look
    like nobody up there is behind on payments, and it would drag the national
    average down.
    """
    wrong = count(db, """
        SELECT count(*) FROM fact_mortgage_arrears
        WHERE is_hidden = TRUE AND mortgages_in_arrears IS NOT NULL
    """)
    assert wrong == 0


def test_we_have_at_least_ten_years_of_data(db):
    years = count(db, """
        SELECT max(d.year) - min(d.year)
        FROM fact_housing_activity f JOIN dim_date d ON d.date_key = f.date_key
        WHERE f.units IS NOT NULL
    """)
    assert years >= 10


def test_the_views_actually_return_something(db):
    for view in ["pipeline_health", "what_changed", "arrears_trend"]:
        rows = count(db, f"SELECT count(*) FROM {view}")
        assert rows > 0, f"{view} is empty"
