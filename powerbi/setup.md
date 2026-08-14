# Building the dashboard in Power BI

You need **Power BI Desktop** (free from the Microsoft Store), and you need to
have run `python run_all.py` first so `data/exports/` has files in it.

There are two ways to do this. **Try the quick way first.**

---

# The quick way (1 minute)

```bash
python make_powerbi_project.py
```

Then open **`powerbi/CMHC Housing.pbip`**.

That script writes out the whole Power BI model as text files — all 20 tables,
19 relationships, hidden ID columns, sort orders, the date table marking, and
all 37 measures. Power BI reads them and everything is already wired up.

It reads the measures straight out of `measures.dax`, so if you change a
measure, re-run the script and it updates.

**The first time you open it**, Power BI may ask permission to read the Parquet
files — click **Continue**, then set the privacy level to **Organizational** (or
just **Ignore privacy levels**) and it'll load.

If it opens and you can see the tables in the Data pane on the right, skip to
[step 6](#6-build-the-pages) and start building visuals.

**If Power BI won't open it**, no problem — do it by hand below. The manual
route always works.

---

# The manual way (about 15 minutes)

## 1. Load the files

`Home → Get Data → More… → Folder → Connect`

Browse to the `data/exports` folder inside this project and click OK.

You'll see a list of `.parquet` files. **Click the arrow next to "Combine" and
choose "Load"** — not Combine.

> Combine tries to stack all the files into one table, which would mash
> unrelated tables together. We want them kept separate.

If you accidentally hit Combine, undo and start again.

Once loaded, rename each query to match its filename (`dim_date`,
`fact_housing_activity`, and so on) if Power BI didn't already.

---

## 2. Tell Power BI which table holds the dates

Click `dim_date` in the Data pane, then:

`Table tools → Mark as date table → Date column: date → OK`

**Don't skip this.** Every "compared to last year" calculation depends on it,
and without it they quietly give wrong answers around year boundaries instead
of showing an error.

---

## 3. Connect the tables

`Model view` (the third icon down the left side).

Power BI usually guesses most of these correctly. Check them against this list
and drag to create any that are missing. Every one goes **from the dimension to
the fact**, and should be **One to many**, filtering in **one direction**.

| Drag from | To | On column |
|---|---|---|
| dim_date | fact_housing_activity | `date_key` |
| dim_geography | fact_housing_activity | `geography_key` |
| dim_dwelling_type | fact_housing_activity | `dwelling_type_key` |
| dim_construction_stage | fact_housing_activity | `stage_key` |
| dim_coverage | fact_housing_activity | `coverage_key` |
| dim_date | fact_market_absorption | `date_key` |
| dim_geography | fact_market_absorption | `geography_key` |
| dim_dwelling_type | fact_market_absorption | `dwelling_type_key` |
| dim_date | fact_unoccupied_housing | `date_key` |
| dim_geography | fact_unoccupied_housing | `geography_key` |
| dim_dwelling_type | fact_unoccupied_housing | `dwelling_type_key` |
| dim_date | fact_mortgage_arrears | `date_key` |
| dim_arrears_region | fact_mortgage_arrears | `arrears_region_key` |
| dim_date | fact_mortgage_originations | `date_key` |
| dim_credit_product | fact_mortgage_originations | `credit_product_key` |
| dim_date | fact_price_index | `date_key` |
| dim_geography | fact_price_index | `geography_key` |
| dim_price_component | fact_price_index | `price_component_key` |
| dim_date | fact_mortgage_rate | `date_key` |

**One important "don't":** do NOT connect `dim_geography` to
`fact_mortgage_arrears`. The arrears data uses 8 regions that don't line up with
provinces (Atlantic is four provinces in one row), which is exactly why
`dim_arrears_region` is a separate table. Connecting them would produce numbers
that look fine and are wrong.

The `pipeline_health`, `what_changed`, `arrears_trend` and `check_results`
tables are self-contained — leave them unconnected.

---

## 4. Add the calculations

Make a home for them first:

`Home → Enter data → name it "_Measures" → Load`

That creates a table with one empty column. Right-click that column and delete
it — you just want the table as a container.

Now open [`measures.dax`](measures.dax). For each measure:

1. Right-click `_Measures` → **New measure**
2. Paste the measure (the name, the `=`, and the formula)
3. Press Enter

There are about 30. It's tedious, but it's a one-time job, and pasting them one
at a time means you'll notice if one has a typo.

Start with these five if you want something on screen quickly:
`Housing Starts`, `Housing Starts Change %`, `Arrears Rate`, `Backlog Months`,
`Coverage Warning`.

---

## 5. Tidy up before building visuals

**Hide the ID columns.** In the Data pane, right-click every column ending in
`_key` and choose **Hide in report view**. They're plumbing — nobody should drag
`date_key` onto a chart.

**Set the sort orders** so months and places don't come out alphabetically.
Click the column, then `Column tools → Sort by column`:

| Click this column | Sort it by |
|---|---|
| `dim_date[month_name]` | `dim_date[month]` |
| `dim_geography[geo_name]` | `dim_geography[sort_order]` |
| `dim_dwelling_type[dwelling_type_name]` | `dim_dwelling_type[sort_order]` |
| `dim_construction_stage[stage_short]` | `dim_construction_stage[stage_order]` |
| `dim_arrears_region[region_name]` | `dim_arrears_region[sort_order]` |

---

## 6. Build the pages

Suggested layout. Nothing here is mandatory — but the coverage slicer is.

### Page 1 — What changed

- **Card:** `Headline` (writes the answer out as a sentence)
- **Cards:** `Housing Starts`, `Housing Starts Change %`, `Arrears Rate`
- **Line chart:** `Housing Starts 12 Months` by `dim_date[date]`
- **Bar chart:** `Housing Starts Change` by `dim_geography[geo_name]`, sorted by value
- **Slicer:** `dim_coverage[coverage_name]` — set to single-select, default to
  *Towns of 10,000 and over*
- **Card:** `Coverage Warning`

> The bar chart deliberately ranks by *how many homes* changed, not by
> percentage. Percentages always put the smallest places on top — a 100% rise in
> Charlottetown is 29 houses.

### Page 2 — Where are the bottlenecks

- **Column chart:** `Housing Starts 12 Months`, `Under Construction`,
  `Housing Completions 12 Months` side by side
- **Line chart:** `Backlog Months` over time
- **Bar chart:** `Backlog Months` by `dim_geography[geo_name]`
- **Table:** geo name, `Backlog Months`, `Backlog Months Change`, `Completion Ratio`

> **Set the coverage slicer on this page to "Big metro areas only" and leave it
> there.** CMHC stopped publishing completions and under-construction for whole
> provinces after December 2022, so these measures come out blank on the other
> coverage. That's the data, not a bug — but put a text box on the page saying
> so, or it looks broken.

### Page 3 — Delinquency

- **Line chart:** `Arrears Rate` and `Arrears Rate 3 Month Average`
- **Line chart, two axes:** `Arrears Rate` against `Mortgage Rate 18 Months Ago`
- **Bar chart:** `Arrears Rate` by `dim_arrears_region[region_name]`
- **Table:** region, `Arrears Rate`, `Arrears Rate Change (points)`
- **Card:** `dim_arrears_region[covers_provinces]`

> Always show `covers_provinces`. Someone who filters to Nova Scotia and sees a
> figure labelled "Atlantic" needs to know why, on the page — not in a footnote.

> The two-axis chart is the interesting one. Arrears rising while rates are flat
> suggests job losses. Arrears rising about 18 months after rates jumped suggests
> people struggling after renewing at a higher payment. Same-shaped line, very
> different story.

### Page 4 — Data quality

- **Card:** `Data Quality Status`
- **Cards:** `Checks Run`, `Check Pass Rate`, `Data Through`
- **Table:** `check_results` — check name, detail, our value, published value

> This page isn't decoration. Being able to show that 21,245 comparisons were run
> against the published figures is what makes someone believe the rest of the
> dashboard. It also means that when a number gets challenged, the answer is
> already on screen.

---

## 7. Small things that make it look finished

**Put the coverage in every chart title.** "Housing starts, towns of 10,000+,
12-month total" — not just "Housing starts". Without it, a screenshot of the
dashboard can't be checked by anyone.

**Leave blanks blank.** A blank means "not published", not zero. Filling them
with zeros draws a line straight down to the axis and invents a crash that
didn't happen.

**Don't use red/green on the arrears page.** Arrears rising from a record low
isn't automatically a disaster, and colouring it red decides that for the reader.

---

## Updating it later

```bash
python run_all.py
```

Then in Power BI: `Home → Refresh`. The Parquet files get overwritten in place,
so everything you built stays exactly as it was.

---

## If something goes wrong

**Charts are empty** — check the coverage slicer has exactly one thing selected.

**"Compared to last year" numbers look odd** — you probably skipped step 2
(Mark as date table).

**Numbers look about twice as big as they should** — either the coverage slicer
has both options selected, or a measure is missing its
`dim_geography[is_aggregate] = FALSE()` filter.

**A place appears twice in a slicer** — you connected `dim_geography` to
`fact_mortgage_arrears`. Delete that relationship.
