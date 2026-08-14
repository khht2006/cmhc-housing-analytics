"""
STEP 2: Turn the raw downloads into the star schema.

Run:  python build.py

This is the biggest file in the project. It goes in order:
    1. create the empty tables (from sql/schema.sql)
    2. fill the dimension tables
    3. fill the fact tables

Every time you run it, it wipes the tables and rebuilds from scratch. That
sounds wasteful, but it's the right call here: Statistics Canada quietly
revises old months (they update estimates as better data arrives), so if we
only ever added new rows, our numbers would slowly drift away from theirs.
Rebuilding everything takes about 15 seconds, so there's no real cost.
"""

import calendar

import duckdb
import pandas as pd

from config import (EXPORT_DIR, RAW_DIR, SQL_DIR, START_MONTH, STATCAN_TABLES,
                    WAREHOUSE_FILE)
from geography import ARREARS_REGION, classify

UNKNOWN = -1   # the key every dimension uses for "we don't recognise this"


def raw_csv(name):
    """
    Build a DuckDB read_csv() call for one of our downloaded files.

    DuckDB can query a CSV file directly, no import step needed. We pass
    all_varchar=true so it reads everything as text - Statistics Canada puts
    footnote letters in number columns, and we'd rather convert them ourselves
    than have DuckDB guess and fail.
    """
    pid = next(t["pid"] for t in STATCAN_TABLES if t["name"] == name)
    path = (RAW_DIR / name / f"{pid}.csv").as_posix()
    return f"read_csv('{path}', header=true, all_varchar=true, sample_size=-1)"


# Statistics Canada writes dates as '2026-05'. We turn that into the number
# 20260501, which is what dim_date uses as its key.
DATE_KEY = "CAST(replace(REF_DATE, '-', '') || '01' AS INTEGER)"


def insert_dataframe(db, table_name, dataframe):
    """Replace everything in a table with the contents of a pandas DataFrame."""
    db.register("temp_data", dataframe)
    db.execute(f"DELETE FROM {table_name}")
    db.execute(f"INSERT INTO {table_name} SELECT * FROM temp_data")
    db.unregister("temp_data")
    print(f"  {table_name:<28} {len(dataframe):>8,} rows")


# =============================================================================
# DIMENSIONS
# =============================================================================

def build_dim_date(db):
    """One row per month from START_MONTH up to today."""
    months = pd.period_range(start=START_MONTH, end=pd.Timestamp.today(), freq="M")

    rows = []
    for m in months:
        rows.append({
            "date_key": int(f"{m.year}{m.month:02d}01"),
            "date": m.to_timestamp().date(),
            "year_month": f"{m.year}-{m.month:02d}",
            "year": m.year,
            "quarter": (m.month - 1) // 3 + 1,
            "month": m.month,
            "month_name": calendar.month_name[m.month],
            "quarter_name": f"{m.year} Q{(m.month - 1) // 3 + 1}",
        })

    insert_dataframe(db, "dim_date", pd.DataFrame(rows))


def all_place_names(db):
    """Collect every distinct place name across all the downloaded tables."""
    names = set()
    for table in STATCAN_TABLES:
        # mortgage_rate and originations are national only - no GEO detail.
        columns = db.sql(f"SELECT * FROM {raw_csv(table['name'])} LIMIT 0").description
        if "GEO" not in [c[0] for c in columns]:
            continue
        found = db.sql(f"SELECT DISTINCT GEO FROM {raw_csv(table['name'])}").fetchall()
        names.update(row[0] for row in found if row[0])
    return sorted(names)


def build_dim_geography(db):
    """Run every place name through geography.py."""
    rows = []
    for name in all_place_names(db):
        info = classify(name)
        rows.append({"geo_name": name, **info})

    table = pd.DataFrame(rows).sort_values(["sort_order", "geo_name"])
    table.insert(0, "geography_key", range(1, len(table) + 1))

    unknown_row = pd.DataFrame([{
        "geography_key": UNKNOWN, "geo_name": "Unknown", "geo_level": "Unknown",
        "province_name": None, "province_code": None, "arrears_region": None,
        "is_aggregate": False, "sort_order": 9999,
    }])
    table = pd.concat([unknown_row, table], ignore_index=True)

    column_order = ["geography_key", "geo_name", "geo_level", "province_name",
                    "province_code", "arrears_region", "is_aggregate", "sort_order"]
    insert_dataframe(db, "dim_geography", table[column_order])

    # Warn if anything didn't classify - that means geography.py needs a new rule.
    unknown_names = table[table.geo_level == "Unknown"].geo_name.tolist()
    if len(unknown_names) > 1:      # the seeded "Unknown" row is expected
        print(f"    WARNING: couldn't classify {unknown_names[1:]}")


# The CBA's 8 regions. covers_provinces gets shown on the dashboard so someone
# looking at Nova Scotia understands why the arrears number says "Atlantic".
ARREARS_REGIONS = [
    ("Canada",           "CANADA",           "everywhere",       True,  0),
    ("Atlantic",         "ATLANTIC",         "NL, PE, NS, NB",   False, 1),
    ("Quebec",           "QUEBEC",           "QC",               False, 2),
    ("Ontario",          "ONTARIO",          "ON",               False, 3),
    ("Manitoba",         "MANITOBA",         "MB",               False, 4),
    ("Saskatchewan",     "SASKATCHEWAN",     "SK",               False, 5),
    ("Alberta",          "ALBERTA",          "AB (plus NT, NU)", False, 6),
    ("British Columbia", "BRITISH COLUMBIA", "BC (plus YT)",     False, 7),
    ("Territories",      "TERRITORIES",      "YT, NT, NU",       False, 8),
]


def build_dim_arrears_region(db):
    table = pd.DataFrame(ARREARS_REGIONS, columns=[
        "region_name", "region_code", "covers_provinces", "is_national", "sort_order"])
    table.insert(0, "arrears_region_key", range(1, len(table) + 1))

    unknown_row = pd.DataFrame([{
        "arrears_region_key": UNKNOWN, "region_name": "Unknown",
        "region_code": "UNKNOWN", "covers_provinces": None,
        "is_national": False, "sort_order": 999,
    }])
    insert_dataframe(db, "dim_arrears_region",
                     pd.concat([unknown_row, table], ignore_index=True))


COVERAGES = [
    ("Towns of 10,000 and over",
     "Every town of 10,000+ people, by province. The default view.", True),
    ("Big metro areas only",
     "Just the 37 largest metro areas. These are INSIDE the figure above, "
     "so never add the two together.", False),
]


def build_dim_coverage(db):
    table = pd.DataFrame(COVERAGES, columns=["coverage_name", "description", "is_default"])
    table.insert(0, "coverage_key", range(1, len(table) + 1))

    unknown_row = pd.DataFrame([{
        "coverage_key": UNKNOWN, "coverage_name": "Unknown",
        "description": None, "is_default": False,
    }])
    insert_dataframe(db, "dim_coverage",
                     pd.concat([unknown_row, table], ignore_index=True))


STAGES = [
    ("Housing starts", "Starts", 1),
    ("Housing under construction", "Under construction", 2),
    ("Housing completions", "Completions", 3),
]


def build_dim_construction_stage(db):
    table = pd.DataFrame(STAGES, columns=["stage_name", "stage_short", "stage_order"])
    table.insert(0, "stage_key", range(1, len(table) + 1))

    unknown_row = pd.DataFrame([{
        "stage_key": UNKNOWN, "stage_name": "Unknown",
        "stage_short": "Unknown", "stage_order": 9,
    }])
    insert_dataframe(db, "dim_construction_stage",
                     pd.concat([unknown_row, table], ignore_index=True))


# CMHC spells the same thing differently in different tables (for example
# "Apartment and other units" vs "Apartment and other unit types"). This maps
# each spelling to one tidy name, plus a sort order.
DWELLING_TYPES = {
    "single-detached units": ("Single-detached", False, 1),
    "single detached units": ("Single-detached", False, 1),
    "semi-detached units": ("Semi-detached", False, 2),
    "row units": ("Row houses", False, 3),
    "apartment and other units": ("Apartments & other", False, 4),
    "apartment and other unit types": ("Apartments & other", False, 4),
    "row, apartment and other units": ("Row + apartments", False, 5),
    "single-detached and semi-detached units": ("Single + semi", False, 6),
    "multiples": ("Multiples", False, 7),
    "total units": ("All types", True, 9),
}


def build_dim_dwelling_type(db):
    names = set()
    for table_config in STATCAN_TABLES:
        columns = [c[0] for c in
                   db.sql(f"SELECT * FROM {raw_csv(table_config['name'])} LIMIT 0").description]
        # The column is called "Type of unit" in some tables and
        # "Type of dwelling unit" in others.
        type_column = next((c for c in columns if c.startswith("Type of")), None)
        if not type_column:
            continue
        found = db.sql(f'SELECT DISTINCT "{type_column}" '
                       f"FROM {raw_csv(table_config['name'])}").fetchall()
        names.update(row[0] for row in found if row[0])

    rows = []
    for name in sorted(names):
        category, is_total, order = DWELLING_TYPES.get(
            name.lower(), (name, False, 50))
        rows.append({"dwelling_type_name": name, "dwelling_category": category,
                     "is_total": is_total, "sort_order": order})

    table = pd.DataFrame(rows).sort_values(["sort_order", "dwelling_type_name"])
    table.insert(0, "dwelling_type_key", range(1, len(table) + 1))

    unknown_row = pd.DataFrame([{
        "dwelling_type_key": UNKNOWN, "dwelling_type_name": "Unknown",
        "dwelling_category": "Unknown", "is_total": False, "sort_order": 999,
    }])
    insert_dataframe(db, "dim_dwelling_type",
                     pd.concat([unknown_row, table], ignore_index=True))


def build_dim_price_component(db):
    found = db.sql('SELECT DISTINCT "New housing price indexes" '
                   f"FROM {raw_csv('price_index')}").fetchall()
    names = sorted(row[0] for row in found if row[0])

    table = pd.DataFrame({"component_name": names})
    table.insert(0, "price_component_key", range(1, len(table) + 1))

    unknown_row = pd.DataFrame([{"price_component_key": UNKNOWN,
                                 "component_name": "Unknown"}])
    insert_dataframe(db, "dim_price_component",
                     pd.concat([unknown_row, table], ignore_index=True))


def build_dim_credit_product(db):
    """
    The Bank of Canada describes each type of lending with a long sentence like:

        "Fixed rate, funds advanced, residential mortgages, insured, 5 years and more"

    We pull out the useful bits so they can be used as separate filters, and
    flag the rows that start with "Total," because those are subtotals of the
    rows below them - adding everything up would count them twice.
    """
    found = db.sql(f'SELECT DISTINCT "Components" FROM {raw_csv("originations")}').fetchall()
    names = sorted(row[0] for row in found if row[0])

    rows = []
    for name in names:
        lowered = name.lower()
        rows.append({
            "product_name": name,
            "product_family": credit_family(lowered),
            "rate_type": ("Fixed" if "fixed rate" in lowered
                          else "Variable" if "variable rate" in lowered
                          else "All"),
            "insurance_status": ("Uninsured" if "uninsured" in lowered
                                 else "Insured" if "insured" in lowered
                                 else "All"),
            "is_total": lowered.startswith("total,"),
        })

    table = pd.DataFrame(rows)
    table.insert(0, "credit_product_key", range(1, len(table) + 1))

    unknown_row = pd.DataFrame([{
        "credit_product_key": UNKNOWN, "product_name": "Unknown",
        "product_family": None, "rate_type": None,
        "insurance_status": None, "is_total": False,
    }])
    insert_dataframe(db, "dim_credit_product",
                     pd.concat([unknown_row, table], ignore_index=True))


def credit_family(lowered_name):
    """
    Sort a Bank of Canada product name into a family.

    Two traps this exists to handle:

    1. The table isn't just mortgages. Business loans and credit cards are in
       there too, so adding everything up gives a number that has nothing to do
       with housing.

    2. Variable rate mortgages appear TWICE, split two different ways - once as
       insured/uninsured under residential mortgages, and again as
       open/closed/convertible. Both breakdowns describe the same money, so
       counting both double-counts. We park the second one in its own family so
       it can't get added in by accident.

    Order matters here - the checks run most specific first.
    """
    if "non-residential mortgage" in lowered_name:
        return "Commercial mortgage"

    if "residential mortgage" in lowered_name:
        return "Residential mortgage"

    # The alternative variable-rate breakdown - same money as above, sliced a
    # different way. Deliberately kept out of "Residential mortgage".
    if "variable rate mortgage" in lowered_name:
        return "Variable rate mortgage (alternative split)"

    if "business loan" in lowered_name:
        return "Business loan"

    if "consumer credit" in lowered_name or "non-mortgage loan" in lowered_name:
        return "Consumer credit"

    return "Other"


# =============================================================================
# FACTS
# =============================================================================
#
# Two rules used in every query below, both worth remembering:
#
#   LEFT JOIN, not JOIN, onto dimensions.
#       A plain JOIN silently drops fact rows whose dimension is missing. With
#       LEFT JOIN + COALESCE(..., -1) they land on the Unknown row instead, so
#       we keep the row AND can count how many went wrong.
#
#   TRY_CAST, not CAST, on the VALUE column.
#       Statistics Canada puts blanks and footnote letters in number columns.
#       CAST would crash the whole load on one bad cell; TRY_CAST gives NULL,
#       which correctly means "not published".

HOUSING_SOURCES = [
    ("housing_activity", "Towns of 10,000 and over"),
    ("housing_cma",      "Big metro areas only"),
]


def build_fact_housing_activity(db):
    db.execute("DELETE FROM fact_housing_activity")

    for source_name, coverage_name in HOUSING_SOURCES:
        db.execute(f"""
            INSERT INTO fact_housing_activity
            SELECT
                {DATE_KEY},
                COALESCE(geo.geography_key, {UNKNOWN}),
                COALESCE(dwelling.dwelling_type_key, {UNKNOWN}),
                COALESCE(stage.stage_key, {UNKNOWN}),
                COALESCE(cov.coverage_key, {UNKNOWN}),
                TRY_CAST(raw.VALUE AS DECIMAL(18, 2))
            FROM {raw_csv(source_name)} AS raw
            LEFT JOIN dim_geography          AS geo      ON geo.geo_name = raw.GEO
            LEFT JOIN dim_dwelling_type      AS dwelling ON dwelling.dwelling_type_name = raw."Type of unit"
            LEFT JOIN dim_construction_stage AS stage    ON stage.stage_name = raw."Housing estimates"
            LEFT JOIN dim_coverage           AS cov      ON cov.coverage_name = '{coverage_name}'
            -- This JOIN is on purpose: it throws away months before 1990,
            -- which is the date range we said we wanted in config.py.
            JOIN dim_date AS d ON d.date_key = {DATE_KEY}
        """)

    count = db.sql("SELECT count(*) FROM fact_housing_activity").fetchone()[0]
    print(f"  fact_housing_activity        {count:>8,} rows")


def build_fact_market_absorption(db):
    """
    The source stores "Absorptions" and "Unabsorbed inventory" as two ROWS.
    We want them as two COLUMNS side by side, so one row of our table answers
    "how many finished homes sold, and how many are still empty?".

    That flip from rows to columns is what the SUM(CASE WHEN ...) does. It's a
    common pattern - people call it a pivot.
    """
    db.execute("DELETE FROM fact_market_absorption")
    db.execute(f"""
        INSERT INTO fact_market_absorption
        SELECT
            {DATE_KEY},
            COALESCE(geo.geography_key, {UNKNOWN}),
            COALESCE(dwelling.dwelling_type_key, {UNKNOWN}),
            SUM(CASE WHEN raw."Completed dwelling units" = 'Absorptions'
                     THEN TRY_CAST(raw.VALUE AS DECIMAL(18, 2)) END),
            SUM(CASE WHEN raw."Completed dwelling units" = 'Unabsorbed inventory'
                     THEN TRY_CAST(raw.VALUE AS DECIMAL(18, 2)) END)
        FROM {raw_csv('absorptions')} AS raw
        LEFT JOIN dim_geography     AS geo      ON geo.geo_name = raw.GEO
        LEFT JOIN dim_dwelling_type AS dwelling ON dwelling.dwelling_type_name = raw."Type of dwelling unit"
        JOIN dim_date AS d ON d.date_key = {DATE_KEY}
        GROUP BY 1, 2, 3
    """)
    count = db.sql("SELECT count(*) FROM fact_market_absorption").fetchone()[0]
    print(f"  fact_market_absorption       {count:>8,} rows")


def build_fact_unoccupied_housing(db):
    """Finished homes nobody has moved into yet - the other half of the
    bottleneck story. Building lots of houses nobody moves into is a very
    different problem from not building enough."""
    db.execute("DELETE FROM fact_unoccupied_housing")
    db.execute(f"""
        INSERT INTO fact_unoccupied_housing
        SELECT
            {DATE_KEY},
            COALESCE(geo.geography_key, {UNKNOWN}),
            COALESCE(dwelling.dwelling_type_key, {UNKNOWN}),
            TRY_CAST(raw.VALUE AS DECIMAL(18, 2))
        FROM {raw_csv('unoccupied')} AS raw
        LEFT JOIN dim_geography     AS geo      ON geo.geo_name = raw.GEO
        LEFT JOIN dim_dwelling_type AS dwelling ON dwelling.dwelling_type_name = raw."Type of unit"
        JOIN dim_date AS d ON d.date_key = {DATE_KEY}
    """)
    count = db.sql("SELECT count(*) FROM fact_unoccupied_housing").fetchone()[0]
    print(f"  fact_unoccupied_housing      {count:>8,} rows")


def build_fact_mortgage_arrears(db):
    db.execute("DELETE FROM fact_mortgage_arrears")
    path = (RAW_DIR / "arrears" / "arrears.csv").as_posix()

    db.execute(f"""
        INSERT INTO fact_mortgage_arrears
        SELECT
            CAST(replace(raw.month, '-', '') || '01' AS INTEGER),
            COALESCE(region.arrears_region_key, {UNKNOWN}),
            TRY_CAST(raw.total_mortgages AS BIGINT),
            TRY_CAST(raw.mortgages_in_arrears AS BIGINT),
            TRY_CAST(raw.arrears_rate AS DECIMAL(9, 4)),
            raw.mortgages_in_arrears IS NULL
        FROM read_csv('{path}', header=true, all_varchar=true, sample_size=-1) AS raw
        LEFT JOIN dim_arrears_region AS region ON region.region_code = raw.region
        JOIN dim_date AS d
          ON d.date_key = CAST(replace(raw.month, '-', '') || '01' AS INTEGER)
    """)
    count = db.sql("SELECT count(*) FROM fact_mortgage_arrears").fetchone()[0]
    print(f"  fact_mortgage_arrears        {count:>8,} rows")


def build_fact_mortgage_originations(db):
    """
    The Bank of Canada packs three different things into one VALUE column.

    A separate "Unit of measure" column says whether a row is dollars or an
    interest rate. And within the dollar rows, the product name says whether
    it's "funds advanced" (new lending this month) or "outstanding balances"
    (total debt still owed).

    Those two are NOT the same thing and must never be added together:
    new lending in May 2026 was a few billion, while outstanding balances were
    about 1.25 million million - that is, $1.25 trillion. Summing them gives a
    number that is wrong by a factor of hundreds.

    So we pull them apart into three properly-named columns.
    """
    db.execute("DELETE FROM fact_mortgage_originations")
    db.execute(f"""
        INSERT INTO fact_mortgage_originations
        SELECT
            {DATE_KEY},
            COALESCE(product.credit_product_key, {UNKNOWN}),

            -- New money lent this month (a flow).
            SUM(CASE WHEN raw."Unit of measure" = 'Dollars'
                      AND lower(raw."Components") LIKE '%funds advanced%'
                     THEN TRY_CAST(raw.VALUE AS DECIMAL(20, 2)) END),

            -- Total money still owed (a snapshot).
            SUM(CASE WHEN raw."Unit of measure" = 'Dollars'
                      AND lower(raw."Components") LIKE '%outstanding%'
                     THEN TRY_CAST(raw.VALUE AS DECIMAL(20, 2)) END),

            -- Rates get averaged, never summed.
            AVG(CASE WHEN raw."Unit of measure" = 'Interest rate'
                     THEN TRY_CAST(raw.VALUE AS DECIMAL(9, 4)) END)

        FROM {raw_csv('originations')} AS raw
        LEFT JOIN dim_credit_product AS product ON product.product_name = raw."Components"
        JOIN dim_date AS d ON d.date_key = {DATE_KEY}
        GROUP BY 1, 2
    """)
    count = db.sql("SELECT count(*) FROM fact_mortgage_originations").fetchone()[0]
    print(f"  fact_mortgage_originations   {count:>8,} rows")


def build_fact_price_index(db):
    db.execute("DELETE FROM fact_price_index")
    db.execute(f"""
        INSERT INTO fact_price_index
        SELECT
            {DATE_KEY},
            COALESCE(geo.geography_key, {UNKNOWN}),
            COALESCE(part.price_component_key, {UNKNOWN}),
            TRY_CAST(raw.VALUE AS DECIMAL(12, 4))
        FROM {raw_csv('price_index')} AS raw
        LEFT JOIN dim_geography       AS geo  ON geo.geo_name = raw.GEO
        LEFT JOIN dim_price_component AS part ON part.component_name = raw."New housing price indexes"
        JOIN dim_date AS d ON d.date_key = {DATE_KEY}
    """)
    count = db.sql("SELECT count(*) FROM fact_price_index").fetchone()[0]
    print(f"  fact_price_index             {count:>8,} rows")


def build_fact_mortgage_rate(db):
    db.execute("DELETE FROM fact_mortgage_rate")
    db.execute(f"""
        INSERT INTO fact_mortgage_rate
        SELECT {DATE_KEY}, AVG(TRY_CAST(raw.VALUE AS DECIMAL(9, 4)))
        FROM {raw_csv('mortgage_rate')} AS raw
        JOIN dim_date AS d ON d.date_key = {DATE_KEY}
        GROUP BY 1
    """)
    count = db.sql("SELECT count(*) FROM fact_mortgage_rate").fetchone()[0]
    print(f"  fact_mortgage_rate           {count:>8,} rows")


# =============================================================================
# Run everything
# =============================================================================

def main():
    print("=" * 70)
    print("STEP 2: BUILDING THE WAREHOUSE")
    print("=" * 70)

    WAREHOUSE_FILE.parent.mkdir(parents=True, exist_ok=True)
    EXPORT_DIR.mkdir(parents=True, exist_ok=True)
    db = duckdb.connect(str(WAREHOUSE_FILE))

    # Drop everything before recreating it.
    #
    # This matters more than it looks. "CREATE TABLE IF NOT EXISTS" does nothing
    # if the table already exists - including when you've since ADDED A COLUMN to
    # sql/schema.sql. Without this you'd edit the schema, re-run, see no error,
    # and quietly keep using the old table shape.
    #
    # We rebuild from the raw files every time anyway, so there's nothing to lose.
    for (view,) in db.sql(
        "SELECT table_name FROM information_schema.tables WHERE table_type = 'VIEW'"
    ).fetchall():
        db.execute(f'DROP VIEW IF EXISTS "{view}" CASCADE')

    for (table,) in db.sql(
        "SELECT table_name FROM information_schema.tables WHERE table_type = 'BASE TABLE'"
    ).fetchall():
        db.execute(f'DROP TABLE IF EXISTS "{table}" CASCADE')

    db.execute((SQL_DIR / "schema.sql").read_text(encoding="utf-8"))

    print("\nDimensions:")
    build_dim_date(db)
    build_dim_geography(db)
    build_dim_arrears_region(db)
    build_dim_coverage(db)
    build_dim_construction_stage(db)
    build_dim_dwelling_type(db)
    build_dim_price_component(db)
    build_dim_credit_product(db)

    print("\nFacts:")
    build_fact_housing_activity(db)
    build_fact_market_absorption(db)
    build_fact_unoccupied_housing(db)
    build_fact_mortgage_arrears(db)
    build_fact_mortgage_originations(db)
    build_fact_price_index(db)
    build_fact_mortgage_rate(db)

    print("\nViews:")
    # Views are saved queries. We use them so the tricky calculations (like
    # "how many months of backlog") are written down once, in SQL, instead of
    # being rebuilt slightly differently by each person who needs them.
    views_sql = (SQL_DIR / "views.sql").read_text(encoding="utf-8")
    for statement in views_sql.split(";"):
        if statement.strip():
            db.execute(statement)
    print("  created (see sql/views.sql)")

    total = db.sql("""
        SELECT (SELECT count(*) FROM fact_housing_activity)
             + (SELECT count(*) FROM fact_market_absorption)
             + (SELECT count(*) FROM fact_unoccupied_housing)
             + (SELECT count(*) FROM fact_mortgage_arrears)
             + (SELECT count(*) FROM fact_mortgage_originations)
             + (SELECT count(*) FROM fact_price_index)
             + (SELECT count(*) FROM fact_mortgage_rate)
    """).fetchone()[0]

    print(f"\nDone. {total:,} rows of facts in {WAREHOUSE_FILE.name}")
    db.close()


if __name__ == "__main__":
    main()
