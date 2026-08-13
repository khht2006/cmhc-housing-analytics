"""
Coverage test: every GEO label actually present in the raw sources must classify.

The unit tests in test_geography.py pin specific tricky cases. This one asserts
the weaker but broader property that NOTHING in the real data falls through to
'Unknown', so a publisher adding a new place breaks the build instead of quietly
disappearing from provincial totals.

Skips if the raw extract has not been run.
"""

from __future__ import annotations

import duckdb
import pytest

from src.common.paths import RAW_DIR, config
from src.transform.geography import classify

SKIP_NO_GEO = {"mortgage_rate_5yr"}


def _all_geo_labels() -> set[str]:
    con = duckdb.connect()
    labels: set[str] = set()
    for spec in config()["statcan"]["tables"]:
        if spec["alias"] in SKIP_NO_GEO:
            continue
        path = RAW_DIR / spec["alias"] / f"{spec['pid']}.csv"
        if not path.exists():
            continue
        rows = con.sql(
            f"SELECT DISTINCT GEO FROM read_csv('{path.as_posix()}', "
            f"header=true, all_varchar=true, sample_size=-1)"
        ).fetchall()
        labels.update(r[0] for r in rows if r[0])
    return labels


@pytest.fixture(scope="module")
def geo_labels() -> set[str]:
    labels = _all_geo_labels()
    if not labels:
        pytest.skip("raw extract not present - run `python -m src.extract.statcan` first")
    return labels


def test_no_label_falls_through_to_unknown(geo_labels):
    unmapped = sorted(g for g in geo_labels if classify(g)["geo_level"] == "Unknown")
    assert not unmapped, f"{len(unmapped)} unmapped GEO labels: {unmapped}"


def test_every_cma_and_province_has_a_cba_region(geo_labels):
    """Arrears roll-up must be resolvable for every non-aggregate place."""
    orphans = sorted(
        g for g in geo_labels
        if (c := classify(g))["geo_level"] in ("CMA", "Province")
        and not c["is_aggregate"]
        and c["cba_region"] is None
    )
    assert not orphans, f"places with no CBA region: {orphans}"


def test_all_ten_provinces_are_present(geo_labels):
    found = {
        classify(g)["province_code"]
        for g in geo_labels
        if classify(g)["geo_level"] == "Province"
    }
    expected = {"NL", "PE", "NS", "NB", "QC", "ON", "MB", "SK", "AB", "BC"}
    assert expected <= found, f"missing provinces: {expected - found}"
