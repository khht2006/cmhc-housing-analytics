"""
Build the Power BI project automatically.

Run:  python make_powerbi_project.py

WHAT THIS SAVES YOU
-------------------
Normally you'd build the Power BI model by hand: load 19 files, draw 19
relationships, hide the ID columns, set 5 sort orders, mark the date table, and
paste in 30 measures one at a time. That's about 45 minutes of clicking, and
every hand-typed measure is a chance to introduce a typo.

Power BI can also open a project as plain text files instead, so this script
writes those files out. Everything ends up already wired up.

It reads the measures straight out of powerbi/measures.dax, so that file stays
the single place measures are written - change it, re-run this, and the model
updates.

WHAT YOU GET
------------
    powerbi/CMHC Housing.pbip                <- double-click this
    powerbi/CMHC Housing.SemanticModel/      <- the tables and measures
    powerbi/CMHC Housing.Report/             <- an empty report to build on

If Power BI won't open it for any reason, powerbi/setup.md still explains how
to build the same thing by hand.
"""

import json
import re
import shutil
import uuid

import duckdb

from config import EXPORT_DIR, PROJECT_DIR, WAREHOUSE_FILE

POWERBI_DIR = PROJECT_DIR / "powerbi"
PROJECT_NAME = "CMHC Housing"
MODEL_DIR = POWERBI_DIR / f"{PROJECT_NAME}.SemanticModel"
REPORT_DIR = POWERBI_DIR / f"{PROJECT_NAME}.Report"


# =============================================================================
# Which tables to load, and how they connect
# =============================================================================

TABLES = [
    "dim_date", "dim_geography", "dim_arrears_region", "dim_coverage",
    "dim_dwelling_type", "dim_construction_stage", "dim_credit_product",
    "dim_price_component",
    "fact_housing_activity", "fact_market_absorption", "fact_unoccupied_housing",
    "fact_mortgage_arrears", "fact_mortgage_originations", "fact_price_index",
    "fact_mortgage_rate",
    "pipeline_health", "what_changed", "arrears_trend", "check_results",
]

# (dimension table, its key, fact table, matching column)
# Every one is one-to-many, from the dimension to the fact.
#
# Note there is deliberately NO link between dim_geography and
# fact_mortgage_arrears. Arrears use 8 regions that don't line up with
# provinces (Atlantic is four provinces in one row), which is exactly why
# dim_arrears_region is separate.
RELATIONSHIPS = [
    ("dim_date", "date_key", "fact_housing_activity", "date_key"),
    ("dim_geography", "geography_key", "fact_housing_activity", "geography_key"),
    ("dim_dwelling_type", "dwelling_type_key", "fact_housing_activity", "dwelling_type_key"),
    ("dim_construction_stage", "stage_key", "fact_housing_activity", "stage_key"),
    ("dim_coverage", "coverage_key", "fact_housing_activity", "coverage_key"),

    ("dim_date", "date_key", "fact_market_absorption", "date_key"),
    ("dim_geography", "geography_key", "fact_market_absorption", "geography_key"),
    ("dim_dwelling_type", "dwelling_type_key", "fact_market_absorption", "dwelling_type_key"),

    ("dim_date", "date_key", "fact_unoccupied_housing", "date_key"),
    ("dim_geography", "geography_key", "fact_unoccupied_housing", "geography_key"),
    ("dim_dwelling_type", "dwelling_type_key", "fact_unoccupied_housing", "dwelling_type_key"),

    ("dim_date", "date_key", "fact_mortgage_arrears", "date_key"),
    ("dim_arrears_region", "arrears_region_key", "fact_mortgage_arrears", "arrears_region_key"),

    ("dim_date", "date_key", "fact_mortgage_originations", "date_key"),
    ("dim_credit_product", "credit_product_key", "fact_mortgage_originations", "credit_product_key"),

    ("dim_date", "date_key", "fact_price_index", "date_key"),
    ("dim_geography", "geography_key", "fact_price_index", "geography_key"),
    ("dim_price_component", "price_component_key", "fact_price_index", "price_component_key"),

    ("dim_date", "date_key", "fact_mortgage_rate", "date_key"),
]

# Columns that should sort by a different column, so months come out in
# calendar order rather than alphabetically (April, August, December...).
SORT_BY = {
    ("dim_date", "month_name"): "month",
    ("dim_geography", "geo_name"): "sort_order",
    ("dim_dwelling_type", "dwelling_type_name"): "sort_order",
    ("dim_construction_stage", "stage_short"): "stage_order",
    ("dim_arrears_region", "region_name"): "sort_order",
}

# Percentages and money display better with a format applied up front.
FORMATS = {
    "Housing Starts Change %": "0.0%",
    "Arrears Rate": "0.00%",
    "Arrears Rate Last Year": "0.00%",
    "Arrears Rate 3 Month Average": "0.00%",
    "Completion Ratio": "0.00",
    "Backlog Months": "0.0",
    "Backlog Months Last Year": "0.0",
    "Backlog Months Change": "+0.0;-0.0;0.0",
    "Percent Unsold": "0.0%",
    "Share Of National Change": "0.0%",
    "Price Index Change %": "0.0%",
    "Check Pass Rate": "0.00%",
    "Housing Starts": "#,0",
    "Housing Completions": "#,0",
    "Under Construction": "#,0",
    "Housing Starts 12 Months": "#,0",
    "Housing Completions 12 Months": "#,0",
    "Total Mortgages": "#,0",
    "Mortgages In Arrears": "#,0",
    "New Mortgage Lending": "$#,0,,\" M\"",
    "New Mortgage Lending 12 Months": "$#,0,,\" M\"",
    "Mortgage Debt Outstanding": "$#,0,,,\" B\"",
    "Variable Rate Share": "0.0%",
    "Mortgage Rate": "0.00",
    "Mortgage Rate 18 Months Ago": "0.00",
    "Average Rate On New Lending": "0.00",
    "Price Index": "0.0",
}


# =============================================================================
# Reading the measures out of measures.dax
# =============================================================================

def read_measures():
    """
    Pull every measure out of powerbi/measures.dax.

    The file is laid out so each measure is one solid block of lines with blank
    lines around it, and every comment is a whole line starting with //. So:
    drop the comment lines, split on blank lines, and each remaining block is
    one measure - first line is "Name =", the rest is the formula.
    """
    text = (POWERBI_DIR / "measures.dax").read_text(encoding="utf-8")

    lines = [line for line in text.splitlines() if not line.strip().startswith("//")]

    measures = []
    block = []
    for line in lines + [""]:          # trailing "" flushes the last block
        if line.strip():
            block.append(line)
            continue
        if not block:
            continue

        header = block[0]
        if "=" not in header:
            print(f"  skipped a block that doesn't look like a measure: {header[:60]}")
            block = []
            continue

        name, _, first_part = header.partition("=")
        name = name.strip()
        body = ([first_part.strip()] if first_part.strip() else []) + block[1:]

        measures.append({"name": name, "expression": body})
        block = []

    return measures


# =============================================================================
# Working out the column types
# =============================================================================

# DuckDB type -> Power BI type.
TYPE_MAP = {
    "BOOLEAN": "boolean",
    "TINYINT": "int64", "SMALLINT": "int64", "INTEGER": "int64", "BIGINT": "int64",
    "HUGEINT": "int64", "UBIGINT": "int64",
    "FLOAT": "double", "DOUBLE": "double",
    "DATE": "dateTime", "TIMESTAMP": "dateTime", "TIMESTAMP_NS": "dateTime",
    "VARCHAR": "string",
}


def powerbi_type(duckdb_type):
    base = duckdb_type.upper().split("(")[0].strip()
    if base.startswith("DECIMAL"):
        return "decimal"
    return TYPE_MAP.get(base, "string")


def read_table_columns(db, table):
    """Ask DuckDB what columns a table has, and what type each one is."""
    rows = db.sql(f"DESCRIBE SELECT * FROM {table}").fetchall()
    return [(name, powerbi_type(dtype)) for name, dtype, *_ in rows]


# =============================================================================
# Building the model file
# =============================================================================

def build_column(table, name, dtype):
    column = {
        "name": name,
        "dataType": dtype,
        "sourceColumn": name,
        "summarizeBy": "none",     # stop Power BI auto-summing ID columns
        "annotations": [{"name": "SummarizationSetBy", "value": "Automatic"}],
    }

    # Hide the ID columns. They're plumbing - nobody should drag date_key onto
    # a chart, and hiding them keeps the field list readable.
    if name.endswith("_key"):
        column["isHidden"] = True

    if dtype == "dateTime":
        column["formatString"] = "yyyy-mm-dd"

    sort_column = SORT_BY.get((table, name))
    if sort_column:
        column["sortByColumn"] = sort_column

    # Marking dim_date as the date table needs its date column flagged as the key.
    if table == "dim_date" and name == "date":
        column["isKey"] = True

    return column


def build_table(db, table):
    columns = [build_column(table, name, dtype)
               for name, dtype in read_table_columns(db, table)]

    entry = {
        "name": table,
        "columns": columns,
        "partitions": [{
            "name": table,
            "mode": "import",
            "source": {
                "type": "m",
                # DataFolder is a parameter defined below, so if you move the
                # project you only change the path in one place.
                "expression": [
                    "let",
                    f'    Source = Parquet.Document(File.Contents(DataFolder & "\\{table}.parquet"))',
                    "in",
                    "    Source",
                ],
            },
        }],
    }

    # This is what "Mark as date table" does. Without it, every
    # compare-to-last-year calculation quietly misbehaves at year boundaries.
    if table == "dim_date":
        entry["dataCategory"] = "Time"

    return entry


def build_measures_table(measures):
    """
    A table that holds nothing but measures.

    Power BI needs measures to live on some table. Putting them on a fact table
    scatters them where nobody can find them, so the convention is one empty
    table named so it sorts to the top of the field list.
    """
    return {
        "name": "_Measures",
        "columns": [{
            "name": "Column1", "dataType": "string", "sourceColumn": "Column1",
            "isHidden": True, "summarizeBy": "none",
            "annotations": [{"name": "SummarizationSetBy", "value": "Automatic"}],
        }],
        "partitions": [{
            "name": "_Measures", "mode": "import",
            "source": {"type": "m", "expression": [
                "let",
                '    Source = #table({"Column1"}, {{""}})',
                "in",
                "    Source",
            ]},
        }],
        "measures": [
            {
                "name": m["name"],
                "expression": m["expression"],
                **({"formatString": FORMATS[m["name"]]} if m["name"] in FORMATS else {}),
            }
            for m in measures
        ],
    }


def build_relationships():
    relationships = []
    for dim_table, dim_column, fact_table, fact_column in RELATIONSHIPS:
        relationships.append({
            # Power BI wants a stable unique name. A UUID built from the four
            # parts means re-running this produces the same name every time.
            "name": str(uuid.uuid5(uuid.NAMESPACE_DNS,
                                   f"{dim_table}.{dim_column}->{fact_table}.{fact_column}")),
            "fromTable": fact_table,       # "from" is the many side
            "fromColumn": fact_column,
            "toTable": dim_table,          # "to" is the one side
            "toColumn": dim_column,
            "crossFilteringBehavior": "oneDirection",
        })
    return relationships


def build_model(db, measures):
    tables = [build_table(db, table) for table in TABLES]
    tables.insert(0, build_measures_table(measures))

    export_path = str(EXPORT_DIR).replace("\\", "\\\\")

    return {
        "name": "SemanticModel",
        "compatibilityLevel": 1567,
        "model": {
            "culture": "en-US",
            "defaultPowerBIDataSourceVersion": "powerBI_V3",
            "sourceQueryCulture": "en-US",
            "dataAccessOptions": {
                "legacyRedirects": True,
                "returnErrorValuesAsNull": True,
            },
            # A parameter holding the folder path, so moving the project is a
            # one-line change rather than editing 19 queries.
            "expressions": [{
                "name": "DataFolder",
                "kind": "m",
                "expression": (
                    f'"{export_path}" meta [IsParameterQuery=true, '
                    'Type="Text", IsParameterQueryRequired=true]'
                ),
            }],
            "tables": tables,
            "relationships": build_relationships(),
            "annotations": [{"name": "PBI_QueryOrder",
                             "value": json.dumps(["DataFolder"] + TABLES)}],
        },
    }


# =============================================================================
# Writing the files out
# =============================================================================

def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2), encoding="utf-8")


def build_empty_report():
    """One blank page, ready for you to drop visuals onto."""
    return {
        "config": json.dumps({
            "version": "5.43",
            "themeCollection": {"baseTheme": {"name": "CY24SU10"}},
        }),
        "layoutOptimization": 0,
        "resourcePackages": [{
            "resourcePackage": {
                "disabled": False, "items": [
                    {"name": "CY24SU10", "path": "BaseThemes/CY24SU10.json", "type": 202}
                ],
                "name": "SharedResources", "type": 2,
            }
        }],
        "sections": [{
            "name": "MainPage",
            "displayName": "Start here",
            "filters": "[]",
            "ordinal": 0,
            "visualContainers": [],
            "config": json.dumps({}),
            "displayOption": 1,
            "width": 1280,
            "height": 720,
        }],
    }


def main():
    print("=" * 70)
    print("BUILDING THE POWER BI PROJECT")
    print("=" * 70)

    if not WAREHOUSE_FILE.exists():
        print("\nThe database doesn't exist yet. Run this first:")
        print("    python run_all.py")
        return 1

    missing = [t for t in TABLES if not (EXPORT_DIR / f"{t}.parquet").exists()]
    if missing:
        print(f"\nThese exports are missing: {missing}")
        print("Run 'python run_all.py' to create them.")
        return 1

    measures = read_measures()
    print(f"\nFound {len(measures)} measures in powerbi/measures.dax")

    db = duckdb.connect(str(WAREHOUSE_FILE), read_only=True)
    model = build_model(db, measures)
    db.close()

    column_count = sum(len(t["columns"]) for t in model["model"]["tables"])
    print(f"Built {len(model['model']['tables'])} tables, {column_count} columns, "
          f"{len(model['model']['relationships'])} relationships")

    # Start clean so an old run can't leave stale files behind.
    for folder in (MODEL_DIR, REPORT_DIR):
        if folder.exists():
            shutil.rmtree(folder)

    write_json(MODEL_DIR / "model.bim", model)
    write_json(MODEL_DIR / "definition.pbism", {"version": "1.0", "settings": {}})

    write_json(REPORT_DIR / "report.json", build_empty_report())
    write_json(REPORT_DIR / "definition.pbir", {
        "version": "1.0",
        "datasetReference": {"byPath": {"path": f"../{PROJECT_NAME}.SemanticModel"}},
    })

    write_json(POWERBI_DIR / f"{PROJECT_NAME}.pbip", {
        "version": "1.0",
        "artifacts": [{"report": {"path": f"{PROJECT_NAME}.Report"}}],
        "settings": {"enableAutoRecovery": True},
    })

    print(f"\nWritten to {POWERBI_DIR}")
    print(f"\nNow open:  powerbi\\{PROJECT_NAME}.pbip")
    print("Power BI will load the data and everything will already be connected.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
