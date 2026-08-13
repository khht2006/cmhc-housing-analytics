# How it works

A walkthrough of the whole project, in the order things actually happen. Read
this alongside the code — every section names the file it's describing.

If someone asks you to explain this project, this document is the answer.

---

## The big picture

```
   Statistics Canada API              CBA website (a PDF)
            |                                |
            +----------------+---------------+
                             |
                        extract.py              STEP 1  download
                             |
                        data/raw/               (the original files, untouched)
                             |
                     geography.py + build.py    STEP 2  organise
                             |
                     data/cmhc.duckdb           (the star schema)
                             |
                         check.py               STEP 3  verify
                             |
                        export.py               STEP 4  export
                             |
                     data/exports/*.parquet
                             |
                         Power BI               the dashboard
```

---

## Step 1: Getting the data (`extract.py`)

### Statistics Canada

They have a proper API, which makes this easy. Every table has an 8-digit
product ID — the table number `34-10-0143-01` is just `34100143` with dashes.

Getting a table is two requests:

1. Ask the API where the file is. It replies with JSON containing a URL.
2. Download that URL, which is a zip containing two CSVs — the data, and a
   metadata file describing the columns.

```python
response = requests.get(f"{STATCAN_API}/{pid}/en")
file_url = response.json()["object"]
zip_bytes = requests.get(file_url).content
```

Nothing is cleaned at this stage. The raw files are saved exactly as published,
so if a number ever looks wrong we can go back and see what the source actually
said.

### The arrears PDF

This one is genuinely awkward, and it's the most interesting part of the file.

Canada has no API for mortgage delinquency data by region. The only public
source going back decades is a PDF the Canadian Bankers Association publishes
each month. Inside it, each region gets a page laid out in two side-by-side
columns so 30 years fits on one page:

```
As at:   Total      Arrears   %       As at:   Total      Arrears   %
1995-01  2,184,443  11,014    0.50%   2011-01  4,192,307  18,702    0.45%
1995-02  2,187,413  10,907    0.50%   2011-02  4,192,738  18,624    0.44%
```

When you pull text out of a PDF, that becomes one long line containing *both*
halves. So instead of splitting the line up, we use a regular expression that
finds every "date, number, number, percent" group anywhere on the line, and turn
each match into its own row:

```python
ROW_PATTERN = re.compile(r"(\d{4}-\d{2})\s+([\d,]+)(?:\s+([\d,]+))?(?:\s+([\d.]+)%)?")
```

The two `(?: ... )?` groups are optional on purpose. The CBA hides the arrears
count for the Territories because the numbers are small enough to identify
individual people, so those rows only have a date and a total.

**Result:** 3,393 rows — 9 regions × 377 months, exactly what you'd expect.

Finding the file is its own small problem: the URL has the month in it
(`stat-mortgages-arrears-may-2026-en.pdf`), and the CBA publishes a few months
behind. So we start at the current month and walk backwards until one exists.

---

## Step 2: Organising it (`geography.py`, `build.py`, `sql/schema.sql`)

### What a star schema is

The data gets split into two kinds of table:

**Dimension tables** are the things you filter and group by — dates, places,
house types. They're small and full of descriptive text.

**Fact tables** are the numbers you add up. They're big, and almost entirely
made of ID numbers pointing at dimensions.

```
                        dim_date
                            |
    dim_geography  ---  fact_housing_activity  ---  dim_coverage
                            |
                   dim_dwelling_type
                            |
                dim_construction_stage
```

It's called a star because of the shape. The point is that fact tables stay
narrow and fast, descriptive text isn't repeated a million times, and — most
usefully — several fact tables can share the same dimensions, so one date filter
controls all of them at once.

### The hard part: place names

This is the bit worth understanding, because getting it wrong makes every total
wrong in a way that looks completely plausible.

Statistics Canada puts totals in the same list as the things they're totals of.
The `GEO` column contains all of this mixed together:

| Name | What it really is |
|---|---|
| `Ontario` | a province |
| `Toronto, Ontario` | a city |
| `Canada` | the total of all provinces |
| `Atlantic provinces` | the total of NL + PE + NS + NB |
| `British Columbia excluding Vancouver` | BC minus one city |
| `Ottawa-Gatineau, Ontario/Quebec` | the total of the two rows below it |
| `Ottawa-Gatineau, Ontario part, Ontario/Quebec` | the Ontario half |
| `Ottawa-Gatineau, Quebec part, Ontario/Quebec` | the Quebec half |

`SUM(units) GROUP BY month` gives you roughly **double** the correct answer,
because Canada gets added on top of the provinces inside it.

`geography.py` runs each name through rules in order, most specific first, and
returns two useful things: what kind of place it is, and whether it's a total.

```python
if name == "Canada":                    # the whole country
if name in KNOWN_TOTALS:                # "Atlantic provinces" etc.
if name in PROVINCES:                   # a real province
if " excluding " in name:               # "BC excluding Vancouver"
if ", and " in name:                    # "Saint John, Fredericton, and Moncton"
if "," in name and province is not None: # a city
else:                                   # Unknown - don't guess
```

That last line matters. An earlier version treated *any* name with a comma as a
city. That meant an unrecognised name would silently become a city with no
province, and disappear from every provincial total with no warning. Now it
comes back as `Unknown`, which shows up as a warning when the pipeline runs.

### The other trap: two overlapping datasets

CMHC publishes housing starts for two different groups of places:

- towns of 10,000 people or more
- the 37 biggest metro areas

The metro areas are **inside** the towns figure. They're not two halves of
something — one contains the other. So if you load both into one table and add
them up, you count the same houses twice.

The fix is `dim_coverage`. Every row in `fact_housing_activity` records which
group it came from, and the Power BI measures refuse to return a number if more
than one is selected. You get an explanation instead of a wrong total.

### Two rules used in every query in `build.py`

**`LEFT JOIN`, never plain `JOIN`, onto dimensions.**
A plain `JOIN` silently deletes fact rows whose dimension is missing. With
`LEFT JOIN` plus `COALESCE(..., -1)` they land on the "Unknown" row instead —
so we keep the row *and* can count how many went wrong. Rows disappearing
silently is how you end up with a number that's mysteriously 3% too low.

**`TRY_CAST`, never plain `CAST`, on numbers.**
Statistics Canada puts blank strings and footnote letters in number columns.
`CAST` would crash the entire load on one bad cell. `TRY_CAST` gives `NULL`,
which is the right answer anyway — "not published" is genuinely different from
"zero".

---

## Step 3: Checking the numbers (`check.py`)

This is what makes the project trustworthy rather than just functional.

The rule: **a check must compare something we calculated against something the
publisher printed separately.** Recalculating a number from the rows it came
from proves nothing.

Two kinds work:

**Cross-check** — the publisher printed the parts *and* the total. The CBA
prints arrears for 8 regions and a Canada row, so our 8 regions must add up to
their Canada number. This tests the whole PDF reader in one go: if a column got
misread, the total wouldn't match.

**Roll-up** — the publisher put a total in the same table as the pieces.
Statistics Canada does this constantly. Our provinces must add up to their
"Canada" row. This is the check that catches a mistake in `geography.py` — if
"Atlantic provinces" got treated as a real province, the total would overshoot
by the whole Atlantic region.

### The mistake I made here, and the fix

The first version used one rule for everything: fail if the two numbers differ
by more than 1%.

That produced 12 false alarms on the arrears rate. Here's one:

- our calculated rate for Ontario: **0.0645%**
- the rate CBA printed: **0.06%**

As a percentage difference that's 7.5% — a screaming failure. But the actual
gap is **0.0045 of a percentage point**, which is just the publisher rounding
to two decimal places.

The problem is that percentage differences stop meaning anything when the
number itself is a tiny percentage, because you're dividing by something near
zero.

So there are now two kinds of tolerance:

| What's being measured | Tolerance | Why |
|---|---|---|
| Counts, dollars | 1% difference | scale doesn't matter, 1% of a big number is still proportionate |
| Rates, percentages | 0.02 percentage points | dividing by a near-zero number makes percentage differences explode |

Of 3,012 arrears rate comparisons, **3,000 are within CBA's own rounding**.

### Known problems in the source data

Three comparisons still fail. All three turned out to be the publisher's
mistake, not ours. For example, in April 1999 the CBA's own Canada row jumps to
2,870,113 and drops back to 2,824,255 the next month, while the 8 regions move
smoothly through 2,794,209 → 2,804,713 → 2,824,255. Their national row is the
odd one out.

These are listed in `KNOWN_SOURCE_PROBLEMS` with the evidence. They get printed
every run but don't fail the pipeline.

**The tempting shortcut is to widen the tolerance until nothing fails.** Don't.
Then the check never catches anything real either, and you've deleted the only
thing telling you when you break something.

### The gate

`run_all.py` runs the checks **before** exporting, and stops if any fail:

```
extract → build → check → export
                    |
                    +-- failed? stop here, don't export
```

Power BI keeps last month's numbers, which we know were right.

---

## Step 4: Exporting for Power BI (`export.py`)

Power BI can't open a DuckDB file directly, so every table is written out as a
Parquet file. Parquet stores data by column instead of by row, which suits the
"add up one column across a million rows" kind of question a dashboard asks.
Power BI reads it natively — no drivers — and unlike CSV it remembers what type
each column is.

The tables are exported **separately, exactly as they are**. It's tempting to
join everything into one big flat table first, but that's a mistake: you lose
the ability to filter several fact tables with one slicer, and the file gets
much bigger because every descriptive value is repeated on every row.

---

## The Power BI side (`powerbi/measures.dax`)

Three rules run through all the calculations. Each exists because breaking it
gives a number that looks fine and is wrong.

**1. To average a rate, divide the totals — don't average the rates.**

Canada's arrears rate is *not* the average of the 8 regional rates. Ontario has
2.1 million mortgages and PEI has a few thousand, so averaging treats them as
equally important. Add up all the arrears, add up all the mortgages, divide.

```dax
Arrears Rate = DIVIDE ( [Mortgages In Arrears], [Total Mortgages] )
```

**2. Never add up two coverages.** Covered above — the measures blank out and
show an explanation instead.

**3. Some numbers are counts at a moment, not monthly totals.**

"Under construction" is how many homes are being built *right now*. The same
house is still under construction next month, so adding up 12 months counts it
12 times. Instead we take the most recent month:

```dax
Under Construction =
CALCULATE ( SUM ( fact_housing_activity[units] ), ..., LASTDATE ( dim_date[date] ) )
```

Housing starts are different — that's how many *began* that month, so adding 12
months up is exactly right. Telling these two apart is the single most common
mistake in dashboards like this.

---

## Why the Azure file exists

`sql/azure_schema.sql` creates the same tables on Azure SQL Server. The project
runs locally on DuckDB because it's a single file with no server to set up, but
in a real job the warehouse would live on a server.

The table and column names are identical on purpose, so the Power BI report can
be pointed at Azure by changing the connection — every relationship and
calculation keeps working.

The only real difference worth explaining is one line:

```sql
CREATE CLUSTERED COLUMNSTORE INDEX CCI_housing_activity ON fact_housing_activity;
```

A columnstore index stores each column separately instead of each row together.
That fits this table because reports ask "add up units for Ontario in 2026" —
touching two or three columns out of six, across hundreds of thousands of rows.
Normal row storage would read all six columns of every row to answer that. It
also compresses well here, since the table is nearly all repeated integer IDs.

The small tables don't get one — columnstore needs around 100,000 rows before
it's worth it.

---

## Things that went wrong while building this

Worth knowing, because "what went wrong" is a normal interview question and
these are all real.

**A test caught a silent bug.** The place-name code treated any name with a
comma as a city. `test_unknown_names_are_not_guessed_at` failed, which is how
I found that an unrecognised name would vanish from provincial totals without
any error.

**Two tables looked identical but weren't.** Tables 34-10-0143 and 34-10-0151
have almost the same title. Comparing them cell by cell showed one is always
bigger — the titles turned out to be "towns of 10,000+" versus "towns of
50,000+". Loading both as if they were the same thing would have doubled
everything. (34-10-0151 isn't in the final version, but that's how the coverage
idea came about.)

**A limitation I couldn't fix, only document.** CMHC stopped publishing
completions and under-construction numbers for whole provinces after December
2022. They still publish them for the big metro areas. So the bottleneck
analysis only works on metro coverage for recent years — that's the data, not a
bug, and it's noted in the DAX and the Power BI setup guide.

---

## If you want to change something

| Goal | Where to go |
|---|---|
| Add a data source | `config.py`, then a `build_fact_*` function in `build.py` |
| Fix a place name | `geography.py`, and add a test in `test_project.py` |
| Change how far back it goes | `START_MONTH` in `config.py` |
| Add a check | `check.py` — write the query, add it to the `CHECKS` list |
| Change the tolerance | `MAX_PERCENT_DIFFERENCE` / `MAX_RATE_DIFFERENCE` in `config.py` |
| Add a saved query | `sql/views.sql`, then add it to `export.py` |
| Add a dashboard calculation | `powerbi/measures.dax` |
