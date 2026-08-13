# Canadian Mortgage & Housing Analytics

A SQL star-schema warehouse and Power BI model over 36 years of public CMHC,
Statistics Canada, Bank of Canada and Canadian Bankers Association data —
built to answer two questions about the Canadian housing market: **what
changed**, and **where are the bottlenecks**.

Everything here runs on **real, currently-published data**. No synthetic rows.

```
  10 source tables          698,102 fact rows        25,752 reconciliation
  1990-01 → 2026-06         7 facts, 9 dimensions    comparisons, 0 breaches
```

---

## What it does

| Stage | What happens | Entry point |
|---|---|---|
| **Extract** | Pulls 10 StatCan tables via the Web Data Service and parses the CBA arrears PDF | `src/extract/` |
| **Transform** | Classifies 90 geography labels, recovers hierarchies from StatCan metadata, builds conformed dimensions | `src/transform/` |
| **Load** | Rebuilds the star schema in one transaction | `src/load/build_warehouse.py` |
| **Reconcile** | 25,752 comparisons against independently published control totals | `src/quality/reconciliation.py` |
| **Export** | Writes the star to Parquet for Power BI | `src/load/export_powerbi.py` |
| **Automate** | Monthly refresh, gated on reconciliation | `pipeline/refresh.py` |

## Quick start

```bash
pip install -r requirements.txt
python -m pipeline.refresh          # full refresh: ~40s cold, ~15s cached
python -m pytest tests/ -q          # 39 structural tests
```

Then open Power BI Desktop and follow [`powerbi/model-setup.md`](powerbi/model-setup.md).

To schedule the monthly run:

```powershell
.\pipeline\schedule_task.ps1
```

---

## Sources

Every source is public, currently published, and verified live against the
StatCan catalogue (`python -m src.extract.discover_sources`).

| Table | Publisher | Coverage | Rows |
|---|---|---|---|
| 34-10-0154 | CMHC | Housing activity, 37 CMAs, 1972– | 342,360 |
| 34-10-0162 | CMHC | Newly completed & unoccupied, 1992– | 187,698 |
| 34-10-0143 | CMHC | Housing activity, centres 10,000+, 1948– | 150,606 |
| 34-10-0149 | CMHC | Absorptions & unabsorbed inventory, 1988– | 98,712 |
| 34-10-0151 | CMHC | Housing activity, centres 50,000+, 1988– | 70,785 |
| 18-10-0205 | Statistics Canada | New housing price index, 1981– | 65,520 |
| 36-10-0639 | Statistics Canada | Household credit liabilities, 1990– | 43,700 |
| 10-10-0006 | Bank of Canada | Funds advanced & balances, 2013– | 16,422 |
| 34-10-0158 | CMHC | Provincial starts (SAAR), 1990– | 5,694 |
| 34-10-0145 | CMHC | 5-year conventional mortgage rate, 1951– | 906 |
| Arrears PDF | Canadian Bankers Association | Mortgages 90+ days in arrears, 1995– | 3,393 |

**982,403 raw rows** in, **698,102 fact rows** after filtering to 1990+.

The CBA arrears series is the only public, long-history, sub-national mortgage
delinquency data in Canada. It ships as a PDF, which is why
[`src/extract/cba_arrears.py`](src/extract/cba_arrears.py) exists.

---

## The model

A **fact constellation**: 7 facts sharing conformed dimensions, so one date or
geography slicer filters housing starts, arrears and prices together.

Full design rationale in [`docs/star-schema.md`](docs/star-schema.md).

### Three problems this data forces, and how the model answers them

**1. Four overlapping statistical universes.** CMHC publishes housing starts for
*all areas*, *centres 10,000+*, *centres 50,000+* and *selected CMAs*. These are
nested, not a hierarchy — verified empirically rather than assumed from the
titles ([`src/extract/compare_universes.py`](src/extract/compare_universes.py)).
Loading them into one fact keyed only on geography would count the same dwelling
up to four times, so **coverage sits on the fact grain** and measures refuse to
return a number when more than one coverage is selected.

**2. Publisher roll-ups sit beside their own children.** `GEO` on 34-10-0158 has
13 members for 10 provinces, because `Canada` and `Atlantic provinces` are rows
next to their components. `dim_geography.is_aggregate` marks them; measures
exclude them; the reconciliation suite uses them as independent control totals.

**3. Arrears are published at a coarser grain than everything else.** CBA reports
8 regions, not 10 provinces — Atlantic is combined, Yukon folds into BC, NWT and
Nunavut into Alberta. Rather than fabricate a provincial split, arrears get their
own `dim_arrears_region`, with `dim_geography.cba_region` as the bridge. A user
filtering to Nova Scotia sees housing starts for Nova Scotia and an arrears
figure **labelled Atlantic** — the coarser grain is made visible, not hidden.

---

## Reconciliation

The point is not "the numbers look plausible". Every check compares a figure the
warehouse computes by aggregating leaf rows against a figure the publisher
printed **independently**.

```
  [PASS] arrears_cross_foot_total_mortgages       377 checks | tol 1%      | 0 new
  [PASS] arrears_rate_matches_published         3,012 checks | tol 0.02pp  | 0 new
  [PASS] dwelling_components_sum_to_total       21,045 checks | tol 1%      | 0 new
  [PASS] facts_resolved_to_known_dimensions          4 checks | tol 0.1%    | 0 new
  [PASS] housing_starts_provinces_sum_to_canada  1,314 checks | tol 1%      | 0 new
  ------------------------------------------------------------------------------
  TOTAL: 25,752 comparisons | 99.9845% within tolerance | 4 known publisher
         anomalies | 0 NEW breaches
```

Two tolerance types, because using one for both is a bug:

- **Volume measures** (unit counts, dollars) → relative tolerance, 1%
- **Ratio measures** (rates, indexes) → absolute tolerance, percentage points

An early version applied 1% relative tolerance to the arrears rate and produced
12 false alarms — Ontario's derived `0.0645%` against a published `0.06%` is a
7.5% relative gap but **0.0045 percentage points**, which is pure display
rounding. Relative tolerance is meaningless for a measure that is itself a small
percentage.

The 4 remaining exceptions are **documented publisher errors**, each with
evidence, listed in `KNOWN_ANOMALIES`. They are reported every run but do not
fail the pipeline. Anything not on that list failing is a genuine regression.
See [`docs/reconciliation.md`](docs/reconciliation.md).

**The export stage does not run if reconciliation finds a new breach.** Power BI
keeps last month's verified numbers rather than picking up figures the pipeline
cannot vouch for.

---

## Things that would have shipped wrong without a constraint

Three real bugs, each caught by something declared rather than something noticed:

**A primary key caught a grain violation.** The load failed on
`duplicate key "19900101, 1, Raw data"`. StatCan flattens hierarchical dimensions
to the *leaf label only*, so `Non-banks` appears six times in 36-10-0639 under
six different parents. Joining on the label silently merges six unrelated series.
The fix was to join on `COORDINATE`'s member ID and recover the ancestry from the
metadata file ([`src/transform/statcan_metadata.py`](src/transform/statcan_metadata.py)),
turning `Non-banks` into `Mortgage loans > Residential mortgages > Non-banks`.

**The same fix exposed a latent double-count.** 30 of 102 credit products are
publisher subtotals stored beside their own children. Now flagged `is_leaf`.

**A unit test caught silent bucketing.** Any comma-containing geography label was
falling into the CMA branch even when no province resolved — so an unmapped label
would have been filed as a CMA with a NULL province and vanished from provincial
roll-ups without warning.

---

## Data availability caveats

Documented in [`docs/data-availability.md`](docs/data-availability.md). The one
that most affects analysis:

**CMHC stopped publishing completions and under-construction at province level
after 2022-12.** Only starts continue there. All three stages continue for census
metropolitan areas. So current bottleneck analysis must run on CMA coverage —
the coverage choice determines which metrics exist at all, not just how they sum.

---

## What the data says

From [`src/quality/smoke_insights.py`](src/quality/smoke_insights.py), run
against the live warehouse:

**The construction pipeline is stalling.** All-CMA backlog — months to clear
units already under construction at the current completion rate:

| June | Starts (12m) | Completions (12m) | Under construction | Backlog months |
|---|---:|---:|---:|---:|
| 2015 | 149,505 | 157,462 | 177,388 | **13.5** |
| 2018 | 178,034 | 156,395 | 223,724 | **17.2** |
| 2021 | 203,678 | 170,452 | 278,868 | **19.6** |
| 2026 | 204,969 | 196,221 | 356,751 | **21.8** |

Starts and completions are both roughly flat since 2021, but units under
construction have doubled since 2015. Halifax sits at **57 months** of backlog.

**Arrears are rising off a historic low.** National 90+ day arrears bottomed at
0.14% in 2022-06 and reached 0.29% by 2026-05 — Ontario is up 0.11pp
year-over-year, the largest move of any region.

---

## Azure deployment

[`sql/azure/`](sql/azure/) deploys the identical schema to Azure SQL Database —
same table names, column names, and semantics as the DuckDB build, so the Power
BI model repoints with a connection-string change rather than a remodel.

The Azure DDL carries what only matters in a server engine: clustered columnstore
indexes on the large facts (~8-10× compression on narrow integer-key fact tables,
and the truncate-and-reload pattern avoids delta-store fragmentation), rowstore
for the small ones, and trusted foreign keys so the optimiser can eliminate joins
Power BI does not need.

```
sql/azure/01_schemas.sql      schemas + run control + reconciliation results
sql/azure/02_dimensions.sql   9 dimensions, Unknown members seeded at key -1
sql/azure/03_facts.sql        7 facts, indexing strategy documented inline
```

---

## Layout

```
config/sources.yml            every source declared once; doubles as lineage doc
src/extract/                  StatCan WDS client, CBA PDF parser, investigation tools
src/transform/                geography classifier, metadata hierarchy parser, dims, facts
src/load/                     warehouse build, views, Parquet export
src/quality/                  reconciliation suite, breach investigation, insight smoke tests
sql/azure/                    T-SQL deployment
sql/duckdb/                   DuckDB deployment + analytical views
powerbi/measures.dax          DAX library with the four additivity rules
powerbi/model-setup.md        relationships, date table, coverage guard
pipeline/refresh.py           orchestrator, gated on reconciliation
tests/                        39 tests: geography, coverage, warehouse integrity
docs/                         design rationale
```

### Investigation tools

Kept in the repo because the reasoning matters as much as the result:

```bash
python -m src.extract.discover_sources      # search the live StatCan catalogue
python -m src.extract.profile_raw           # row counts, dimensions, period ranges
python -m src.extract.list_geo              # every distinct GEO label
python -m src.extract.inspect_table <alias> # find what breaks an assumed grain
python -m src.extract.compare_universes     # are two tables the same population?
python -m src.quality.investigate_breaches  # our bug, or publisher quirk?
python -m src.quality.smoke_insights        # do the views answer real questions?
```

---

## Licence and attribution

Source data is published under the
[Statistics Canada Open Licence](https://www.statcan.gc.ca/en/reference/licence)
and by the Canadian Bankers Association. This project is not affiliated with or
endorsed by CMHC, Statistics Canada, the Bank of Canada or the CBA.
