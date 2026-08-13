"""
Integrity tests against the built warehouse.

These assert STRUCTURAL invariants - grain, referential integrity, additivity
guards. They are distinct from the reconciliation suite, which compares values
against the publisher. Both are needed: a warehouse can be internally perfect
and still disagree with the source, or agree with the source while having a
broken grain that only shows up when someone slices it a new way.

Skips cleanly if the warehouse has not been built.
"""

from __future__ import annotations

import duckdb
import pytest

from src.common.paths import duckdb_path


@pytest.fixture(scope="module")
def con():
    path = duckdb_path()
    if not path.exists():
        pytest.skip("warehouse not built - run `python -m pipeline.refresh` first")
    connection = duckdb.connect(str(path), read_only=True)
    yield connection
    connection.close()


def scalar(con, sql: str):
    return con.sql(sql).fetchone()[0]


# --------------------------------------------------------------------------- #
# Grain
# --------------------------------------------------------------------------- #

def test_housing_activity_grain_is_unique(con):
    """
    Coverage is PART OF THE GRAIN. If this fails, either a source was loaded
    twice or two sources were mapped to the same coverage - which would double
    every figure on the dashboard.
    """
    dupes = scalar(con, """
        SELECT count(*) FROM (
            SELECT date_key, geography_key, dwelling_type_key, stage_key, coverage_key
            FROM dw.fact_housing_activity
            GROUP BY ALL HAVING count(*) > 1
        )
    """)
    assert dupes == 0, f"{dupes} duplicated grain combinations in fact_housing_activity"


def test_arrears_grain_is_one_row_per_region_month(con):
    dupes = scalar(con, """
        SELECT count(*) FROM (
            SELECT date_key, arrears_region_key FROM dw.fact_mortgage_arrears
            GROUP BY ALL HAVING count(*) > 1
        )
    """)
    assert dupes == 0


# --------------------------------------------------------------------------- #
# Referential integrity
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "fact,fk,dim,pk",
    [
        ("fact_housing_activity", "geography_key", "dim_geography", "geography_key"),
        ("fact_housing_activity", "dwelling_type_key", "dim_dwelling_type", "dwelling_type_key"),
        ("fact_housing_activity", "stage_key", "dim_construction_stage", "stage_key"),
        ("fact_housing_activity", "coverage_key", "dim_coverage", "coverage_key"),
        ("fact_housing_activity", "date_key", "dim_date", "date_key"),
        ("fact_market_absorption", "geography_key", "dim_geography", "geography_key"),
        ("fact_mortgage_arrears", "arrears_region_key", "dim_arrears_region", "arrears_region_key"),
        ("fact_mortgage_originations", "credit_product_key", "dim_credit_product", "credit_product_key"),
        ("fact_household_credit", "credit_product_key", "dim_credit_product", "credit_product_key"),
        ("fact_price_index", "price_component_key", "dim_price_component", "price_component_key"),
    ],
)
def test_no_orphan_foreign_keys(con, fact, fk, dim, pk):
    orphans = scalar(con, f"""
        SELECT count(*) FROM dw.{fact} f
        LEFT JOIN dw.{dim} d ON d.{pk} = f.{fk}
        WHERE d.{pk} IS NULL
    """)
    assert orphans == 0, f"{orphans} orphan {fact}.{fk}"


def test_unknown_key_rate_is_negligible(con):
    """
    Loaders LEFT JOIN and COALESCE to -1 rather than dropping rows, so a broken
    mapping surfaces as unknown keys. Anything above 0.1% is a real defect.
    """
    pct = scalar(con, """
        SELECT 100.0 * SUM(CASE WHEN geography_key = -1 OR dwelling_type_key = -1
                                  OR stage_key = -1 OR coverage_key = -1
                                THEN 1 ELSE 0 END) / count(*)
        FROM dw.fact_housing_activity
    """)
    assert pct <= 0.1, f"{pct:.4f}% of fact_housing_activity rows have unknown keys"


# --------------------------------------------------------------------------- #
# Additivity guards - the things that silently produce wrong numbers
# --------------------------------------------------------------------------- #

def test_aggregate_geographies_are_flagged(con):
    """The specific rows that would double-count a provincial total."""
    for name in ["Canada", "Atlantic provinces", "Prairie provinces",
                 "British Columbia excluding Vancouver", "Ontario excluding Toronto",
                 "Ottawa-Gatineau, Ontario/Quebec", "Census metropolitan areas"]:
        flag = con.sql(
            "SELECT is_aggregate FROM dw.dim_geography WHERE geo_name = ?", params=[name]
        ).fetchone()
        if flag is None:
            continue  # label absent from this vintage
        assert flag[0] is True, f"{name!r} is not flagged is_aggregate"


def test_split_cma_parts_are_not_flagged_as_aggregates(con):
    """The two Ottawa-Gatineau 'part' rows ARE leaves and must stay summable."""
    for name in ["Ottawa-Gatineau, Ontario part, Ontario/Quebec",
                 "Ottawa-Gatineau, Quebec part, Ontario/Quebec"]:
        row = con.sql(
            "SELECT is_aggregate FROM dw.dim_geography WHERE geo_name = ?", params=[name]
        ).fetchone()
        if row is None:
            continue
        assert row[0] is False, f"{name!r} should be a leaf"


def test_credit_product_subtotals_are_flagged(con):
    """
    The BoC table stores 'Total, funds advanced, ...' beside its own children.
    If is_leaf were all TRUE, mortgage originations would roughly double.
    """
    subtotals = scalar(con, "SELECT count(*) FROM dw.dim_credit_product WHERE NOT is_leaf")
    assert subtotals > 0, "no credit products flagged as publisher subtotals"


def test_credit_product_label_is_not_unique_but_key_is(con):
    """
    Documents the trap that broke the first load: 'Non-banks' occurs many times
    under different parents. The dimension must key on (source_alias, member_id).
    """
    dupe_labels = scalar(con, """
        SELECT count(*) FROM (
            SELECT component_name FROM dw.dim_credit_product
            GROUP BY 1 HAVING count(*) > 1
        )
    """)
    assert dupe_labels > 0, "expected repeated component labels - has the source changed?"

    dupe_keys = scalar(con, """
        SELECT count(*) FROM (
            SELECT source_alias, member_id FROM dw.dim_credit_product
            GROUP BY ALL HAVING count(*) > 1
        )
    """)
    assert dupe_keys == 0


def test_geography_has_no_duplicate_display_names(con):
    """
    Regression guard. CBA arrears regions once lived in dim_geography, putting
    two members named 'Ontario' in one dimension and showing a user two identical
    entries in a single slicer. They now live in dim_arrears_region.
    """
    dupes = scalar(con, """
        SELECT count(*) FROM (
            SELECT geo_name FROM dw.dim_geography GROUP BY 1 HAVING count(*) > 1
        )
    """)
    assert dupes == 0, "duplicate geo_name in dim_geography"


# --------------------------------------------------------------------------- #
# Coverage and completeness
# --------------------------------------------------------------------------- #

def test_all_ten_provinces_present(con):
    n = scalar(con, """
        SELECT count(DISTINCT province_code) FROM dw.dim_geography
        WHERE geo_level = 'Province' AND province_code IS NOT NULL
    """)
    assert n >= 10, f"only {n} provinces in dim_geography"


def test_history_spans_at_least_ten_years(con):
    years = scalar(con, """
        SELECT max(d.year) - min(d.year)
        FROM dw.fact_housing_activity f JOIN dw.dim_date d ON d.date_key = f.date_key
        WHERE f.units IS NOT NULL
    """)
    assert years >= 10, f"only {years} years of housing history"


def test_fact_volume_meets_project_scale(con):
    total = scalar(con, """
        SELECT (SELECT count(*) FROM dw.fact_housing_activity)
             + (SELECT count(*) FROM dw.fact_market_absorption)
             + (SELECT count(*) FROM dw.fact_price_index)
             + (SELECT count(*) FROM dw.fact_household_credit)
             + (SELECT count(*) FROM dw.fact_mortgage_originations)
             + (SELECT count(*) FROM dw.fact_mortgage_arrears)
             + (SELECT count(*) FROM dw.fact_rate_environment)
    """)
    assert total >= 500_000, f"only {total:,} fact rows"


def test_every_dimension_has_an_unknown_member(con):
    dims = {
        "dim_geography": "geography_key",
        "dim_arrears_region": "arrears_region_key",
        "dim_coverage": "coverage_key",
        "dim_dwelling_type": "dwelling_type_key",
        "dim_construction_stage": "stage_key",
        "dim_credit_product": "credit_product_key",
        "dim_price_component": "price_component_key",
        "dim_source": "source_key",
    }
    for table, key in dims.items():
        n = scalar(con, f"SELECT count(*) FROM dw.{table} WHERE {key} = -1")
        assert n == 1, f"{table} is missing its Unknown member"


def test_suppressed_arrears_are_null_not_zero(con):
    """
    A suppressed count stored as 0 would drag the national rate down and read as
    an improvement in credit quality that never happened.
    """
    bad = scalar(con, """
        SELECT count(*) FROM dw.fact_mortgage_arrears
        WHERE is_suppressed AND mortgages_in_arrears IS NOT NULL
    """)
    assert bad == 0


def test_views_return_rows(con):
    for schema, view in [
        ("dw", "vw_construction_pipeline"),
        ("dw", "vw_absorption_health"),
        ("dw", "vw_arrears_trend"),
        ("dw", "vw_what_changed"),
    ]:
        n = scalar(con, f"SELECT count(*) FROM {schema}.{view}")
        assert n > 0, f"{schema}.{view} is empty"


def test_pipeline_view_excludes_overlapping_geographies(con):
    """
    vw_construction_pipeline must keep leaves plus the national row, and nothing
    that overlaps a sibling - otherwise a SUM within one geo_level double-counts.
    """
    bad = scalar(con, """
        SELECT count(*) FROM dw.vw_construction_pipeline
        WHERE geo_level <> 'Country'
          AND geo_name IN (SELECT geo_name FROM dw.dim_geography WHERE is_aggregate)
    """)
    assert bad == 0
