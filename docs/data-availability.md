# Data availability caveats

Things about the source data that change what analysis is possible. Each was
found by looking, not by reading documentation, and each would otherwise show up
as a mysteriously empty chart.

## 1. Completions and under-construction stopped at province level after 2022-12

The single most consequential caveat.

| Coverage | Starts | Completions | Under construction |
|---|---|---|---|
| Census metropolitan areas (34-10-0154) | 2026-06 | **2026-06** | **2026-06** |
| Centres 10,000+ (34-10-0143) | 2026-06 | 2022-12 | 2002-06 |
| Centres 50,000+ (34-10-0151) | 2026-06 | 2022-12 | 2022-12 |
| All areas SAAR (34-10-0158) | 2026-06 | n/a | n/a |

CMHC continues to publish all three construction stages for census metropolitan
areas, but only housing starts at province level.

**Consequence:** every bottleneck metric — completion ratio, backlog months — is
only computable on **CMA coverage** for recent periods. A user who selects
"Centres 10,000 and over" and looks at backlog months gets blanks after 2022,
and that is correct behaviour rather than a bug.

This is why `dim_coverage` matters for more than double-counting: the coverage
choice determines which *metrics exist*, not just how they aggregate.

## 2. The four housing universes are nested, not hierarchical

```
All areas  ⊃  centres 10,000+  ⊃  centres 50,000+  ⊃  selected CMAs
```

Verified empirically with `python -m src.extract.compare_universes`: 34-10-0143
exceeds 34-10-0151 in every one of 37,822 differing overlapping cells. The
titles are nearly identical ("in centres 10,000 and over" vs "in all centres
of 50,000 and over") and would mislead anyone who trusted them.

CMAs do **not** tile a province. Summing all CMAs in Ontario does not give
Ontario, because centres below the CMA threshold and rural areas are excluded.
Any "CMA share of province" measure must state this.

Additionally, "All areas (SAAR)" is **seasonally adjusted at an annual rate**.
Its level is roughly 12× a monthly count and is not comparable to the raw series
— `dim_coverage.is_annualised` flags this.

## 3. Arrears geography is 8 CBA regions, not 10 provinces

| CBA region | Provinces covered |
|---|---|
| Atlantic | NL, PE, NS, NB |
| Quebec | QC |
| Ontario | ON |
| Manitoba | MB |
| Saskatchewan | SK |
| Alberta | AB **+ NT + NU** |
| British Columbia | BC **+ YT** |
| Territories | reported separately, total mortgages only |

Modelled honestly via a separate `dim_arrears_region` rather than allocating
Atlantic across four provinces, which would invent precision the source does not
have. `dim_arrears_region.covers_provinces` is exposed in the report so the user
can see the mapping.

The CBA panel is 9 banks (BMO, CIBC, National Bank, RBC, Scotiabank, TD,
Manulife from 2004, Laurentian from 2010, Equitable from 2020). It excludes
credit unions and most non-bank lenders, so **the true national arrears rate is
somewhat higher than reported**. Consistent over time, so trends are sound;
levels should be read as "arrears among major-bank mortgages".

## 4. Suppressed cells are NULL, never zero

CBA suppresses the Territories arrears count for confidentiality (small counts).
StatCan suppresses some CMA-level cells the same way.

Stored as `NULL` with `is_suppressed = TRUE`, never as 0. A suppressed count
recorded as zero would drag a national average down and read as an improvement
in credit quality that never happened. `tests/test_warehouse.py` asserts this.

## 5. StatCan revises history silently

Seasonal adjustment factors get re-estimated and preliminary months restated
without a version marker. This is why the pipeline is **full truncate-and-reload
rather than incremental append** — an append-only load would slowly drift away
from the published figures with nothing to indicate it.

`ops.source_vintage` records a SHA-256 of every downloaded file, so two months'
reports can be proven to have been built from different source vintages.

## 6. History starts 1990-01 by choice

34-10-0143 goes back to 1948 and the 5-year mortgage rate to 1951. `dim_date`
starts at 1990-01 because the other facts (arrears 1995, household credit 1990,
NHPI 1981, BoC lending 2013) do not extend further, and a date dimension full of
months where only one fact has data makes for a poor slicer.

Change the window with `python -m src.load.build_warehouse --start 1948-01`.
Facts INNER JOIN `dim_date`, so extending the dimension is what unlocks the
older history.

## 7. Label variants across sibling tables

The same concept is spelled differently in different CMHC tables:

- `Apartment and other units` vs `Apartment and other unit types`
- `Single detached units` vs `Single-detached units`
- `Montréal excluding St-Jérôme` vs `Montréal excluding Saint-Jérôme`

`dim_dwelling_type.dwelling_category` conforms the first two. The geography
classifier folds accents for matching but stores the published spelling verbatim.
`src/transform/dimensions.py` logs a warning for any dwelling label not in its
mapping, so a new variant surfaces in the run log rather than silently becoming
its own category.
