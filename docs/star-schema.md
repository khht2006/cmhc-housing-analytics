# Star schema design

## Shape: a fact constellation, not a single star

Seven fact tables share a set of conformed dimensions. They share `dim_date` and
`dim_geography` in particular, which is what lets a single Power BI slicer filter
housing starts, arrears and price index simultaneously.

```
                          ┌──────────────┐
                          │   dim_date   │
                          └──────┬───────┘
                                 │
   ┌────────────────┬────────────┼────────────┬──────────────────┐
   │                │            │            │                  │
┌──┴───────────┐ ┌──┴─────────┐ ┌┴──────────┐ ┌┴───────────────┐ ┌┴──────────────┐
│fact_housing_ │ │fact_market_│ │fact_      │ │fact_mortgage_  │ │fact_price_    │
│activity      │ │absorption  │ │mortgage_  │ │originations    │ │index          │
│              │ │            │ │arrears    │ │                │ │               │
└──┬─────┬───┬─┘ └──┬────┬────┘ └────┬──────┘ └───────┬────────┘ └──┬─────┬──────┘
   │     │   │      │    │           │                │             │     │
   │     │   │      │    │           │                │             │     │
┌──┴─────┴───┴──────┴────┴───────────┴────┐  ┌────────┴─────────┐ ┌─┴─────┴──────┐
│           dim_geography                 │  │dim_credit_product│ │dim_price_    │
│  Country → Province → CMA               │  └──────────────────┘ │component     │
│  + cba_region bridge for arrears        │                       └──────────────┘
└─────────────────────────────────────────┘
   ▲                    ▲                 ▲
   │                    │                 │
┌──┴──────────────┐ ┌───┴──────────┐ ┌────┴─────────┐
│dim_dwelling_type│ │dim_construc- │ │ dim_coverage │
│                 │ │ tion_stage   │ │              │
└─────────────────┘ └──────────────┘ └──────────────┘

Every fact also carries source_key → dim_source (lineage).
```

## The three modelling problems this data forces

### 1. Overlapping geographic universes → `dim_coverage`

CMHC publishes housing starts for three *different populations of places*:

| Source table | Population | GEO members |
|---|---|---|
| 34-10-0154 | Census metropolitan areas | 37 CMAs |
| 34-10-0151 | All centres 10,000+ | Canada + 10 provinces |
| 34-10-0158 | All areas (incl. rural), seasonally adjusted | Canada + provinces + regions |

These are **not** a hierarchy. CMAs do not tile a province; "centres 10,000+"
excludes rural; "all areas" includes everything but is seasonally adjusted to an
annual rate and so is not comparable in level to the raw counts.

Loading all three into one fact table with only a geography key would silently
triple-count. The fix is a `dim_coverage` degenerate dimension on the grain:

> **fact_housing_activity grain** = month × geography × dwelling type ×
> construction stage × **coverage**

The fact is additive *within* a coverage slice. The Power BI model defaults the
coverage slicer to "Centres 10,000 and over" so a user can never accidentally sum
across incompatible universes, and the DAX measures guard it explicitly.

### 2. StatCan mixes aggregates into leaf rows → `is_aggregate`

`GEO` on 34-10-0158 has 13 members for 10 provinces, because `Canada` and
`Atlantic provinces` are stored as rows *beside* their own components. Any naive
`SUM(VALUE) GROUP BY REF_DATE` double-counts by roughly 2×.

`dim_geography.is_aggregate` marks those rows. Facts load them (they are the
publisher's own figures and are useful as reconciliation controls) but every
measure filters `is_aggregate = 0` by default, and the reconciliation suite uses
the aggregate rows as the independent control totals to check leaf sums against.

### 3. Arrears are published at a coarser grain than everything else

CBA reports 8 regions, not 10 provinces:

| CBA region | Provinces covered |
|---|---|
| ATLANTIC | NL, PE, NS, NB |
| QUEBEC | QC |
| ONTARIO | ON |
| MANITOBA | MB |
| SASKATCHEWAN | SK |
| ALBERTA | AB + NT + NU |
| BRITISH COLUMBIA | BC + YT |
| TERRITORIES | reported separately, total only |

Two wrong ways to handle this: (a) invent a province-level split by allocating
Atlantic across NL/PE/NS/NB — that fabricates precision the source does not have;
(b) collapse everything else to 8 regions — that throws away real CMA detail.

The model instead puts `cba_region` on `dim_geography` as a **roll-up attribute**.
`fact_mortgage_arrears` joins to the geography rows where `geo_level = 'CBA
Region'`. Housing facts join at province/CMA level. A user filtering to
"Nova Scotia" gets housing starts for Nova Scotia and an arrears figure labelled
*Atlantic* — with the label making the coarser grain visible rather than hiding it.

## Grain and measures per fact

| Fact | Grain | Measures | Rows |
|---|---|---|---|
| `fact_housing_activity` | month × geo × dwelling × stage × coverage | `units` | ~560K |
| `fact_market_absorption` | month × geo × dwelling | `absorptions`, `unabsorbed_inventory`, `unoccupied_units` | ~286K |
| `fact_price_index` | month × geo × price component | `index_value` | ~65K |
| `fact_household_credit` | month × credit product × seasonality | `balance_dollars` | ~44K |
| `fact_mortgage_originations` | month × credit product | `funds_advanced`, `outstanding_balance`, `effective_rate` | ~16K |
| `fact_mortgage_arrears` | month × CBA region | `total_mortgages`, `mortgages_in_arrears`, `arrears_rate_pct` | ~3.4K |
| `fact_rate_environment` | month | `conventional_5yr_rate` | ~900 |

## Surrogate keys

All dimensions use integer surrogate keys, not natural keys.

`dim_date` uses a smart key (`20260501` for 2026-05-01) because date keys are the
one case where a readable surrogate is worth it — it makes partition pruning and
manual query debugging far easier, and the value is genuinely immutable.

Every other dimension uses a meaningless `IDENTITY` / sequence key. Geography
names in particular are unstable across StatCan vintages ("Apartment and other
units" vs "Apartment and other unit types" appear in sibling tables), so binding
facts to a name would break the model on a routine publisher revision.

## Late-arriving and unknown members

Each dimension seeds a row with key `-1` = `Unknown`. The loader resolves fact
rows to `-1` rather than dropping them when a dimension member is missing, and
the quality suite fails the run if any fact has more than 0.1% unknown keys.
Silently dropping rows is how reconciliation variance appears from nowhere three
months later.
