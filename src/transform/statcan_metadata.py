"""
Parse StatCan `*_MetaData.csv` files to recover dimension hierarchies.

The problem this solves
-----------------------
StatCan's data CSV flattens hierarchical dimensions to their LEAF LABEL ONLY.
In 36-10-0639 the label 'Non-banks' appears six times - as a child of
'Non-mortgage loans', of 'Residential mortgages', of 'Non-residential
mortgages', and so on. Keying a dimension on that label therefore collapses six
distinct series into one and violates the fact's grain. (The PRIMARY KEY on
fact_household_credit is what caught this; without it the load would have
silently summed unrelated series together.)

The metadata file carries the real structure:

    "Dimension ID","Member Name","Classification Code","Member ID","Parent Member ID",...
    "3","Mortgage loans","","17","",...
    "3","Residential mortgages","","18","17",...
    "3","Non-banks","","20","18",...

and the data file's COORDINATE column ('1.1.20') is the tuple of member IDs, one
per dimension. So COORDINATE is the join key that the label pretends to be.

This module returns, per dimension, a frame of members with a resolved ancestry
path - turning 'Non-banks' into
'Mortgage loans > Residential mortgages > Non-banks'.
"""

from __future__ import annotations

import csv
from pathlib import Path

import pandas as pd

from src.common.logging_setup import get_logger

log = get_logger(__name__)

MEMBER_HEADER = ("Dimension ID", "Member Name")


def _read_member_section(path: Path) -> list[dict]:
    """Pull the member-list block out of the multi-section metadata CSV."""
    with path.open(encoding="utf-8-sig", errors="replace", newline="") as fh:
        rows = list(csv.reader(fh))

    header_idx = None
    for i, row in enumerate(rows):
        if len(row) >= 2 and row[0] == MEMBER_HEADER[0] and row[1] == MEMBER_HEADER[1]:
            header_idx = i
            break
    if header_idx is None:
        return []

    header = rows[header_idx]
    members: list[dict] = []
    for row in rows[header_idx + 1:]:
        # Sections are separated by blank lines; the next section starts with
        # its own header row.
        if not row or not any(cell.strip() for cell in row):
            break
        record = dict(zip(header, row))
        if not record.get("Dimension ID", "").strip().isdigit():
            break
        members.append(record)
    return members


def dimension_members(metadata_path: Path) -> dict[int, pd.DataFrame]:
    """
    Return {dimension_id: DataFrame[member_id, member_name, parent_member_id,
    hierarchy_path, parent_name, depth, is_leaf]}.
    """
    raw = _read_member_section(metadata_path)
    if not raw:
        log.warning("no member section found in %s", metadata_path.name)
        return {}

    df = pd.DataFrame(raw)
    df = df.rename(columns={
        "Dimension ID": "dimension_id",
        "Member Name": "member_name",
        "Member ID": "member_id",
        "Parent Member ID": "parent_member_id",
    })
    df["dimension_id"] = pd.to_numeric(df["dimension_id"], errors="coerce").astype("Int64")
    df["member_id"] = pd.to_numeric(df["member_id"], errors="coerce").astype("Int64")
    df["parent_member_id"] = pd.to_numeric(
        df["parent_member_id"].replace("", None), errors="coerce"
    ).astype("Int64")

    out: dict[int, pd.DataFrame] = {}
    for dim_id, grp in df.groupby("dimension_id"):
        grp = grp[["dimension_id", "member_id", "member_name", "parent_member_id"]].copy()
        name_by_id = dict(zip(grp.member_id, grp.member_name))
        parent_by_id = dict(zip(grp.member_id, grp.parent_member_id))

        def ancestry(mid) -> list[str]:
            """Walk to the root, guarding against a malformed cyclic parent chain."""
            chain, seen = [], set()
            while pd.notna(mid) and mid not in seen:
                seen.add(mid)
                chain.append(name_by_id.get(mid, str(mid)))
                mid = parent_by_id.get(mid)
            return list(reversed(chain))

        paths = {mid: ancestry(mid) for mid in grp.member_id}
        grp["hierarchy_path"] = grp.member_id.map(lambda m: " > ".join(paths[m]))
        grp["depth"] = grp.member_id.map(lambda m: len(paths[m]))
        grp["parent_name"] = grp.parent_member_id.map(
            lambda p: name_by_id.get(p) if pd.notna(p) else None
        )
        grp["root_name"] = grp.member_id.map(lambda m: paths[m][0] if paths[m] else None)

        has_children = set(grp.parent_member_id.dropna().tolist())
        grp["is_leaf"] = ~grp.member_id.isin(has_children)

        out[int(dim_id)] = grp.reset_index(drop=True)

    return out


def coordinate_member(coordinate: str, dimension_id: int) -> int | None:
    """
    Extract one dimension's member ID from a COORDINATE string.

    COORDINATE '1.1.20' with dimension_id=3 -> 20. This is the join key between
    the data file and the metadata member list.
    """
    if not coordinate:
        return None
    parts = coordinate.split(".")
    if len(parts) < dimension_id:
        return None
    try:
        return int(parts[dimension_id - 1])
    except ValueError:
        return None
