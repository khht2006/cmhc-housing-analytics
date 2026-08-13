# Reconciliation

## What counts as a reconciliation check

Not "the numbers look plausible". Every check compares a figure the **warehouse
computes** by aggregating leaf rows against a figure the **publisher printed
independently**. A check that recomputes a number from the same rows it came
from proves nothing.

Two kinds qualify:

**Cross-foot** — the publisher printed both the parts and the total. CBA prints
per-region arrears *and* a CANADA row; summing our 8 region rows must reproduce
their national row. This validates the PDF parser end to end: a misaligned column
or a dropped row breaks the sum immediately.

**Roll-up** — the publisher printed an aggregate beside the leaves in the same
table. StatCan does this constantly; `Canada` sits next to the provinces. Our
leaf sum must reproduce it. This is what fails loudly if
`dim_geography.is_aggregate` is wrong — treating `Atlantic provinces` as a leaf
would overshoot Canada by roughly the entire Atlantic total.

## Current results

```
  [PASS] arrears_cross_foot_total_mortgages       377 checks | tol 1%      | 1 known | 0 NEW
  [PASS] arrears_rate_matches_published         3,012 checks | tol 0.02pp  | 2 known | 0 NEW
  [PASS] dwelling_components_sum_to_total      21,045 checks | tol 1%      | 0 known | 0 NEW
  [PASS] facts_resolved_to_known_dimensions         4 checks | tol 0.1%    | 0 known | 0 NEW
  [PASS] housing_starts_provinces_sum_to_canada 1,314 checks | tol 1%      | 1 known | 0 NEW
  --------------------------------------------------------------------------------------
  TOTAL: 25,752 comparisons | 25,748 within tolerance (99.9845%)
         | 4 known publisher anomalies | 0 NEW breaches
```

## Two tolerance types, and why one is not enough

The first version of this suite applied a 1% **relative** tolerance to every
check, including the arrears rate. It produced 12 false alarms.

Ontario, 2021-11: our derived rate is `0.0645%`, CBA printed `0.06%`.

- As a **relative** variance: 7.5% — a screaming failure
- As an **absolute** variance: **0.0045 percentage points** — pure display
  rounding by the publisher

Relative tolerance is meaningless for a measure that is itself a small
percentage, because the denominator approaches zero. So:

| Measure kind | Tolerance | Reason |
|---|---|---|
| Volume (unit counts, dollars) | relative, 1% | scale-free; 1% of a big number is still proportionate |
| Ratio (rates, indexes) | absolute, percentage points | denominator near zero makes relative variance explode |
| Row conservation | relative, 0.1% | unknown keys mean a broken mapping, not publisher noise |

Of 3,012 arrears-rate comparisons, **3,000 fall within CBA's own 2-decimal
rounding** (≤0.005pp). Ten more sit between 0.005pp and 0.02pp, consistent with
CBA computing the printed rate from unrounded internals while the printed counts
are restated separately. Two exceed 0.02pp and are genuine publisher errors.

## Known publisher anomalies

Listed explicitly in `src/quality/reconciliation.py::KNOWN_ANOMALIES` with
evidence, rather than hidden by loosening a threshold. They are reported on
every run but do not fail the pipeline. **Anything not on this list failing is a
genuine regression.**

### 1999-04 — arrears cross-foot, 2.28%

CBA's own `CANADA` row spikes and falls back while the 8 regional rows move
smoothly:

| Month | Sum of 8 regions | CBA's CANADA row | Difference |
|---|---:|---:|---:|
| 1999-03 | 2,794,209 | 2,794,209 | 0 |
| **1999-04** | **2,804,713** | **2,870,113** | **-65,400** |
| 1999-05 | 2,824,255 | 2,824,255 | 0 |

The national row is the outlier. Our sum of the parts follows the trend exactly.
Across all 377 months: **313 tie exactly**, 63 differ by under 0.5%, 1 exceeds 1%.

### 1999-07 / Atlantic — arrears rate, 0.0227pp

Same 1999 restatement episode. Atlantic `total_mortgages` spikes to 233,386
(221,181 the month before, 224,470 the month after); the printed 0.50% is
consistent with the pre-restatement denominator. Every adjacent month rounds
correctly: 0.5236→0.52, 0.5168→0.52, 0.5083→0.51, 0.5054→0.51.

### 2013-03 / Quebec — arrears rate, 0.0313pp

CBA printed `0.30%` where its own counts (2,731 / 824,269) give `0.3313%`.
A transcription error in the source PDF.

### 1995-03 / Centres 50,000+ — starts roll-up, 1.03%

CMHC's published Canada total (4,563) exceeds the sum of its own 10 provincial
rows (4,516) by 47 units. No province row is suppressed, so the gap is
publisher-side. 1 breach in 1,314 comparisons.

## Investigating a new breach

```bash
python -m src.quality.investigate_breaches
```

The question to answer is always the same: **our bug, or the publisher's?** The
distinction matters — a pipeline bug must be fixed, a publisher inconsistency
must be documented and tolerated. Loosening a threshold to make a breach go away
destroys the gate's value, because it will then never fire when something real
breaks.

Diagnostic pattern that has worked every time so far: look at the neighbouring
months. A genuine parser bug produces a *systematic* error — every month, or
every row of a region. A publisher restatement produces a *single-month spike
that reverts*, with the rest of the series tying exactly.

## The gate

`pipeline/refresh.py` runs reconciliation **before** the Power BI export, and
skips the export entirely if a new breach appears:

```
  1. extract → 2. build → 3. views → 4. reconcile → 5. export
                                          │
                                          └── new breach? stop here, exit 2
```

Power BI keeps last month's verified numbers rather than picking up figures the
pipeline cannot vouch for. A refresh that silently publishes unreconciled numbers
is worse than one that fails loudly, because a wrong dashboard gets believed and
acted on.

Exit codes, surfaced by Task Scheduler as "Last Run Result":

| Code | Meaning |
|---|---|
| 0 | success, export written |
| 1 | unhandled error |
| 2 | reconciliation breach, export deliberately skipped |

## Adding a check

1. Write a function returning a frame of
   `check_name, check_grain, warehouse_value, control_value`.
2. Register it in `CHECKS` with the right `tolerance_type` and a `rationale`
   explaining the choice.
3. Run it. If it produces zero comparisons the runner warns — a check that
   exercises nothing is worse than no check, because it reads as a pass.

The control value must come from somewhere the warehouse did not compute. If you
cannot point at a published figure it corresponds to, it is a unit test, not a
reconciliation — put it in `tests/test_warehouse.py` instead.
