# Canadian Mortgage & Housing Analytics

A data warehouse and Power BI dashboard built on real Canadian housing data,
covering 1990 to 2026.

It answers two questions about the housing market:

- **What changed?** Which provinces and cities moved the numbers this month
- **Where are the bottlenecks?** Where are homes being started but not finished

All the data is real and public — from CMHC, Statistics Canada, the Bank of
Canada, and the Canadian Bankers Association. Nothing is made up.

```
7 data sources        610,722 rows of facts       21,245 automated checks
1990 → 2026           7 fact tables, 8 dimensions  99.99% match the published numbers
```

---

## Setup

### 1. Install Python packages

You need Python 3.10 or newer.

```bash
pip install -r requirements.txt
```

That installs six things: `duckdb` (the database), `pandas`, `pyarrow`,
`requests` (downloading), `pdfplumber` (reading the arrears PDF), and `pytest`.

### 2. Run the pipeline

```bash
python run_all.py
```

Takes about a minute. It downloads roughly 100 MB of data, builds the database,
checks the numbers, and writes files for Power BI.

If you want to re-run it later without downloading everything again:

```bash
python run_all.py --skip-download
```

### 3. Look at the data

```bash
python explore.py
```

This prints answers to six real questions. It's the fastest way to see whether
things worked, and a good place to start writing your own queries.

### 4. Run the tests

```bash
python -m pytest test_project.py -v
```

16 tests. The first half tests the place-name logic and runs on its own; the
second half checks the database and skips if you haven't built it yet.

### 5. Build the dashboard

Open Power BI Desktop and follow [`powerbi/setup.md`](powerbi/setup.md). It's
about 15 minutes of clicking, all written out step by step.

---

## What the files do

Every file is at the top level so you can open them in order and read straight
through.

| File | What it does |
|---|---|
| `config.py` | Every setting and data source, in one place |
| `extract.py` | **Step 1** — downloads the data |
| `geography.py` | Sorts out place names (the trickiest bit — see below) |
| `build.py` | **Step 2** — builds the database tables |
| `check.py` | **Step 3** — checks our numbers against the published ones |
| `export.py` | **Step 4** — writes files for Power BI |
| `run_all.py` | Runs steps 1–4 in order |
| `explore.py` | Example questions you can ask the data |
| `test_project.py` | Tests |
| `sql/schema.sql` | The tables |
| `sql/views.sql` | Saved queries for the two main questions |
| `sql/azure_schema.sql` | The same tables written for Azure SQL Server |
| `powerbi/measures.dax` | The calculations Power BI uses |
| `powerbi/setup.md` | How to build the dashboard |

Want to understand how it all fits together?
Read **[HOW-IT-WORKS.md](HOW-IT-WORKS.md)** — it walks through the design
decisions and the mistakes that were caught along the way.

---

## Where the data comes from

| Source | What it gives us |
|---|---|
| CMHC 34-10-0143 | Housing starts, under construction, completions — towns of 10,000+ |
| CMHC 34-10-0154 | The same three, for 37 big metro areas |
| CMHC 34-10-0149 | Finished homes: how many sold, how many still empty |
| CMHC 34-10-0162 | Finished homes nobody has moved into yet |
| CMHC 34-10-0145 | The 5-year mortgage rate |
| Statistics Canada 18-10-0205 | New housing price index |
| Bank of Canada 10-10-0006 | New mortgage money lent each month |
| Canadian Bankers Association | Mortgages 90+ days behind on payments, back to 1995 |

The arrears data is the interesting one. It's the only public source of
mortgage delinquency by region in Canada, and it's only published as a PDF —
so `extract.py` reads the numbers out of the PDF text.

---

## The one design decision worth knowing about

**Statistics Canada puts totals in the same list as the things they're totals
of.** The place-name column contains "Ontario" and "Toronto" and "Canada" and
"Atlantic provinces", all mixed together.

If you just write `SUM(units) GROUP BY month`, you get roughly double the real
answer, because "Canada" gets added on top of all the provinces that make it up.

So `geography.py` looks at every place name and works out whether it's a real
place or a total. Totals get flagged, and every calculation leaves them out.

There are some genuinely nasty cases:

```
Ottawa-Gatineau, Ontario part, Ontario/Quebec     <- real place
Ottawa-Gatineau, Quebec part, Ontario/Quebec      <- real place
Ottawa-Gatineau, Ontario/Quebec                   <- the total of those two
British Columbia excluding Vancouver              <- overlaps BC
Saint John, Fredericton, and Moncton, New Brunswick   <- three cities in one row
```

There's a test for each of these in `test_project.py`.

---

## How we know the numbers are right

`check.py` runs **21,245 comparisons** every time the pipeline runs.

The idea: take a number we calculated by adding up detail rows, and compare it
against the same number the publisher printed separately. If they disagree, one
of us is wrong.

For example, the Canadian Bankers Association publishes arrears for 8 regions
*and* a Canada total. Adding up our 8 regions has to give their Canada number.
It does, exactly, in 313 of 377 months.

```
[OK  ] arrears add up to Canada          377 comparisons   worst gap 2.2787%
[OK  ] arrears rate matches published  3,012 comparisons   worst gap 0.0313pp
[OK  ] house types add up to total    17,417 comparisons   worst gap 0.0000%
[OK  ] provinces add up to Canada        438 comparisons   worst gap 0.0000%
[OK  ] every row matched a real place      1 comparisons   worst gap 0.0000%
--------------------------------------------------------------------------
21,245 comparisons | 21,242 matched (99.9859%) | 3 known source problems | 0 real failures
```

Three comparisons don't match. All three turned out to be mistakes in the
published data, not ours — they're listed in `check.py` with the evidence.

**If the checks fail, `run_all.py` stops and does not export.** Power BI keeps
showing the last set of numbers we know were right. A dashboard that's quietly
wrong is worse than one that's obviously out of date, because people believe it.

---

## What the data actually says

Two findings from `explore.py`, straight out of the warehouse:

**Building has slowed down.** Across all big metro areas, "backlog months" is
how long it would take to finish everything currently under construction:

| June | Started (12m) | Finished (12m) | Under construction | Backlog months |
|---|---:|---:|---:|---:|
| 2015 | 149,505 | 157,462 | 177,388 | **13.5** |
| 2021 | 203,678 | 170,452 | 278,868 | **19.6** |
| 2026 | 204,969 | 196,221 | 356,751 | **21.8** |

Starts and completions have been roughly flat since 2021, but the number of
homes stuck under construction has doubled since 2015. Halifax is worst at 57
months.

**Mortgage arrears are climbing off a record low.** Nationally they bottomed at
0.14% in June 2022 and reached 0.29% by May 2026. Ontario has risen fastest,
up 0.11 percentage points in a year.

---

## Attribution

Data is published by Statistics Canada under the
[Statistics Canada Open Licence](https://www.statcan.gc.ca/en/reference/licence),
and by the Canadian Bankers Association. This is a personal learning project and
isn't affiliated with any of them.
