# Report specification

Five pages. Each answers one question and states the answer in words, not only
in a chart — a dashboard that makes the reader do the interpreting has moved the
work rather than done it.

---

## Page 1 — What changed

**Question:** what moved this month, and who moved it?

| Element | Visual | Measure |
|---|---|---|
| Headline | Card | `[What Changed Headline]` |
| KPI row | 4 cards | `[Housing Starts]`, `[Housing Starts YoY %]`, `[Arrears Rate]`, `[Arrears Rate YoY (pp)]` |
| National trend | Line | `[Housing Starts 12M]` by `dim_date[date]` |
| Who moved it | Bar, sorted by `[Movement Rank]` | `[Housing Starts YoY]` by `dim_geography[geo_name]` |
| Contribution | Bar | `[Contribution to National Change]` |
| Coverage guard | Card | `[Coverage Warning]` |

Slicers: `dim_coverage[coverage_name]` (single-select, default **Centres 10,000
and over**), `dim_date[year]`, `dim_geography[geo_level]`.

**Why contribution and not growth rate.** Ranking by percentage change always
surfaces the smallest places — a 106% rise in Manitoba is 473 dwellings while
a 46% fall in BC is 2,525. The bar chart ranks by absolute movement so a large
decline ranks as prominently as a large rise, which is what an executive asking
"what changed?" actually wants to see.

---

## Page 2 — Where are the bottlenecks

**Question:** is supply being built, and is it clearing?

| Element | Visual | Measure |
|---|---|---|
| Pipeline funnel | Column | `[Housing Starts 12M]`, `[Units Under Construction]`, `[Housing Completions 12M]` |
| Backlog trend | Line | `[Backlog Months]` by date |
| Backlog by market | Bar | `[Backlog Months]` by `dim_geography[geo_name]` |
| Deterioration | Table with conditional formatting | `[Backlog Months]`, `[Backlog Months YoY]`, `[Completion Ratio 12M]` |

**Pin the coverage slicer to "Census metropolitan areas" on this page** and label
it. CMHC stopped publishing completions and under-construction at province level
after 2022-12, so every measure here goes blank on the other coverages — see
[`../docs/data-availability.md`](../docs/data-availability.md).

**Reading the metrics.** `Completion Ratio 12M` below 1 means the pipeline is
filling faster than it empties. `Backlog Months` is the headline: at the current
completion rate, how many months to clear what is already started. Rising backlog
with flat completions is a *stalled pipeline*; falling completions with falling
starts is a *demand slowdown*. Both look like "completions are down" on a naive
chart, and they call for opposite responses.

---

## Page 3 — Delinquency

**Question:** is credit quality deteriorating, and where?

| Element | Visual | Measure |
|---|---|---|
| National rate | Line | `[Arrears Rate]` and `[Arrears Rate 3M Avg]` |
| Renewal shock | Line, dual axis | `[Arrears Rate]` vs `[Conventional 5Y Rate 18M Ago]` |
| By region | Bar | `[Arrears Rate]` by `dim_arrears_region[region_name]` |
| Movement | Table | `[Arrears Rate]`, `[Arrears Rate YoY (pp)]`, `[Mortgages In Arrears]` |
| Grain note | Card | `dim_arrears_region[covers_provinces]` |

**Always show `covers_provinces`.** A reader who filters to Nova Scotia and sees
an "Atlantic" arrears figure needs to know why, on the page, not in a footnote.

**The dual-axis chart is the analytical point.** Arrears rising while the 5-year
rate is flat is a labour-market story. Arrears rising 18-24 months after a rate
spike is a renewal-shock story. Same chart shape, different cause, different
response — putting the lagged rate on the same canvas makes the comparison
visible instead of arguable.

Use `[Arrears Rate]`, never `[Arrears Rate (Published)]`, for anything
aggregated above a single region. The published column is an average of printed
rates; the correct national figure is a ratio of sums. Both exist so the report
can show they agree at region level.

---

## Page 4 — Market context

**Question:** what conditions is this happening in?

| Element | Visual | Measure |
|---|---|---|
| Price index | Line | `[New Housing Price Index]` by geography |
| Price momentum | Line | `[NHPI YoY %]` |
| Originations | Column | `[Funds Advanced 12M]` |
| Rate on new lending | Line | `[Effective Rate (Weighted)]` |
| Product mix | Stacked bar | `[Funds Advanced]` by `dim_credit_product[rate_type]`, `[insurance_status]` |

Filter `dim_credit_product[is_leaf] = TRUE` at page level. 30 of 102 members are
publisher subtotals sitting beside their own children; without the filter, every
originations figure roughly doubles.

---

## Page 5 — Data quality

**Question:** should I trust these numbers?

| Element | Visual | Measure |
|---|---|---|
| Status | Card | `[Data Quality Status]` |
| Comparisons run | Card | `[Reconciliation Comparisons]` |
| Pass rate | Card | `[Reconciliation Pass Rate]` |
| Freshness | Card | `[Last Refresh]` |
| By check | Table | `vw_reconciliation_summary` |
| Lineage | Table | `dim_source`: publisher, table number, licence, extraction time |

**This page is not decoration.** Publishing the quality record next to the
numbers is what earns the trust. A user who can see that 25,752 comparisons ran
against independently published control totals, that four known publisher errors
are documented, and exactly which StatCan table each figure came from, will trust
the dashboard far more than one who is simply asked to.

It also changes the conversation when a figure is challenged: instead of "let me
go check", the answer is on screen.

---

## Cross-cutting

**Colour.** Housing volumes in one hue, delinquency in another. Never use
red/green for direction on the arrears page — rising arrears from a historic low
is not automatically "bad", and colouring it red pre-empts the reader's judgment.

**Blanks.** Blank means "not published", never zero. Leave blanks visible and
label the axis rather than filling them with zeros, which would draw a line
straight down through a suppressed cell and invent a collapse.

**Titles.** Every visual title states the measure, the coverage, and the grain,
e.g. *"Housing starts, centres 10,000+, 12-month rolling total"*. Without the
coverage in the title, a screenshot of this dashboard is unfalsifiable.
