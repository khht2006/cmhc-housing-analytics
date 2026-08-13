# Power BI model setup

Everything below is done once. After that, a refresh is `Home → Refresh`.

## 1. Connect

`Get Data → Folder` → `data/exports`

Power BI Desktop reads Parquet natively; no ODBC driver, no gateway plumbing.
In the Navigator, load each `.parquet` as its own query rather than combining
them — the folder connector's default "Combine" step would union unrelated
tables into one.

Name each query after its file (`dim_date`, `fact_housing_activity`, …).

## 2. Mark the date table

`Table tools → Mark as date table` on **dim_date**, date column **`date`**.

Skipping this is the single most common cause of broken time intelligence.
Without it `DATEADD` and `DATESINPERIOD` silently return wrong results at year
boundaries rather than erroring.

## 3. Relationships

All are **one-to-many**, single direction, dimension → fact. Leave
cross-filtering **single** unless a row below says otherwise.

| From (1) | To (many) | Key |
|---|---|---|
| dim_date | fact_housing_activity | `date_key` |
| dim_geography | fact_housing_activity | `geography_key` |
| dim_dwelling_type | fact_housing_activity | `dwelling_type_key` |
| dim_construction_stage | fact_housing_activity | `stage_key` |
| dim_coverage | fact_housing_activity | `coverage_key` |
| dim_source | fact_housing_activity | `source_key` |
| dim_date | fact_market_absorption | `date_key` |
| dim_geography | fact_market_absorption | `geography_key` |
| dim_dwelling_type | fact_market_absorption | `dwelling_type_key` |
| dim_date | fact_mortgage_arrears | `date_key` |
| dim_arrears_region | fact_mortgage_arrears | `arrears_region_key` |
| dim_date | fact_mortgage_originations | `date_key` |
| dim_credit_product | fact_mortgage_originations | `credit_product_key` |
| dim_date | fact_price_index | `date_key` |
| dim_geography | fact_price_index | `geography_key` |
| dim_price_component | fact_price_index | `price_component_key` |
| dim_date | fact_household_credit | `date_key` |
| dim_credit_product | fact_household_credit | `credit_product_key` |
| dim_date | fact_rate_environment | `date_key` |

**Do not** relate `dim_geography` to `fact_mortgage_arrears`. Arrears are
published at CBA-region grain, which is not the housing geography — that is the
whole reason `dim_arrears_region` exists. Cross-filtering the two is done
through the `dim_geography[cba_region]` attribute in a report-level filter, not
through a relationship.

The `vw_*` tables load as standalone tables for the narrative pages. Relate
`vw_reconciliation_summary` to nothing.

## 4. Measures

Create an empty table named `_Measures`
(`Enter data → Load`, delete the placeholder column), then paste from
[`measures.dax`](measures.dax). Keeping measures in one home table stops them
scattering across fact tables where nobody can find them.

## 5. Hide from report view

Hide every surrogate key column (`*_key`) and every `sha256` / `source_url`
column. A key column in a slicer is noise, and a user who drags `date_key` into
a visual instead of `dim_date[date]` gets a broken axis with no error.

Hide `fact_*` tables' raw numeric columns too — force everyone through the
measures, so nobody accidentally drops `units` into a visual and sums across
four overlapping coverages.

## 6. Set the coverage default

On every page that uses housing volumes, add a `dim_coverage[coverage_name]`
slicer, set it to single-select, and default it to **Centres 10,000 and over**.

Add the `[Coverage Warning]` measure as a card above the visuals. If a user
multi-selects coverages, the volume measures return blank and the card explains
why — the alternative is a number that is silently four times too large.

## 7. Sort orders

For each, `Column tools → Sort by column`:

| Column | Sort by |
|---|---|
| `dim_date[month_name]` | `dim_date[month]` |
| `dim_geography[geo_name]` | `dim_geography[sort_order]` |
| `dim_dwelling_type[dwelling_type_name]` | `dim_dwelling_type[sort_order]` |
| `dim_construction_stage[stage_short]` | `dim_construction_stage[stage_order]` |
| `dim_arrears_region[region_name]` | `dim_arrears_region[sort_order]` |

## 8. Repointing at Azure SQL

The Azure deployment in [`sql/azure/`](../sql/azure/) creates the identical
schema — same table names, column names and semantics. To switch:

`Transform data → Data source settings → Change Source` → SQL Server →
your server, database, **Import** mode.

Because the table and column names match exactly, the relationships and every
measure survive the switch untouched. That symmetry is the reason the two DDL
dialects are maintained deliberately rather than generated loosely.
