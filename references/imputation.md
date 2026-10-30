# Imputation: choosing a method, checking it, recording the evidence

Read with `missing-data-methodology.md`, which covers whether to fill at all. This file covers how a method is chosen, how it is checked, and what is recorded for each filled cell.

## 1. Method selection

The planner proposes a method from the column's type, role, distribution, missing share, related columns and time structure (rules in `missing-data-methodology.md`). Then it tests that proposal against the alternatives the table allows:

| Column | Candidates compared |
|---|---|
| numeric | mean (the reference), median, group median and group mean for up to three grouping columns (the one linked to the missingness first), time interpolation if rows are evenly spaced in time, KNN and iterative imputation on up to six other numeric columns (with scikit-learn) |
| discrete numeric scale (ratings, codes) | median, group median, mode. Never KNN or iterative, which would invent levels nobody chose. |
| categorical | mode, group mode for up to three grouping columns |

The comparison runs on the table after every other enabled step (placeholders, types, invalid values and missing-value codes), so it measures errors on the values imputation will actually see.

### Masked-value comparison

1. Take the observed values of the column (training rows only, when a split column exists).
2. Hide a random 20% of them (at most 2,000) and set them to missing.
3. Fill them with each candidate, using the cleaning engine itself. Each method sees only the values that are still visible, so the hidden values cannot leak into the fill.
4. Compare the fills with the hidden truth.
5. Repeat three times with different random masks.

Numeric results:

- **MAE**, **RMSE** and **bias** (mean of fill minus truth).
- **Normalised MAE**: MAE divided by the column's standard deviation, so columns can be compared. Filling with the mean gives about 0.8 on normal data.
- **Spread ratio**: the standard deviation of the fills divided by that of the truth. 0 means a single value was used for every cell, which flattens the distribution.
- **Coverage**: the share of hidden values the method could fill at all. Interpolation cannot fill the ends of a series.
- **MAE by subgroup**, for the main grouping column.

Categorical results: **accuracy** and **macro-F1**.

The proposal is replaced only when another method is clearly better:

- its normalised MAE is at least 10% lower, with no larger bias (`imputation_comparison.switch_if_error_lower_by`);
- or, for categories, its accuracy is at least 0.10 higher.

A method that fills fewer than 95% of hidden values is not chosen. Highest completeness is never the reason.

`dm compare OUT COLUMN [--group-by G ...] [--time DATE]` reruns the comparison for one column after you edit the plan, and writes `analysis/imputation_comparison_<column>.json`.

### What the comparison cannot show

- Random masking imitates values that are **missing completely at random**. If the real values are missing because of what they are (high incomes left blank), every method can look good here and still be biased on the real gaps. Nothing in the observed data can detect that.
- The errors are measured on values that *were* observed. These can differ systematically from the missing ones.
- Report the comparison as "on hidden known values, method X was most accurate". Never claim it shows the filled values are correct.

## 2. Evidence records, not confidence scores

`audit/imputation_evidence.csv` has one row for every filled cell:

- `op_id`, `dm_row_id`, `column`, `method`, `imputed_value`;
- `support_n`: the observations the estimate rests on (group size for group methods, the two neighbours for interpolation, k for KNN);
- `group`, `group_n`, `dispersion` (IQR and sd of the supporting values, or the share of the modal category);
- `gap_length` (consecutive missing values in the run, for time-series methods);
- `uses_future_values` (true for interpolation and backward fill);
- `fallback_used` (true when a group had no data and the overall value was used);
- `source` (the documented source for rules and fixed values);
- the masked-value result for the method actually used: `validation_metric`, `validation_value`, `validation_baseline`, `validation_n`, `validated`;
- `evidence_level`, `suitable_for_downstream`, `limitations`.

There is deliberately no per-cell "confidence: 0.85". A number like that would look precise while resting on assumptions nobody can check. The record lists the inputs to the judgement instead, and the qualitative level summarises them.

### Evidence levels

Thresholds are in `config/default.json` under `imputation_evidence`. Change them in a copy and pass `--config`.

| Level | When |
|---|---|
| **strong supporting evidence** | a documented business rule (`apply_rule`) or a documented fixed value; or a statistical method whose masked normalised MAE is at most 0.5 (it explains at least half the typical deviation) with at least 30 supporting observations; or, for categories, masked accuracy of at least 0.8 with at least 30 observations |
| **moderate supporting evidence** | masked normalised MAE below 0.9 (better than knowing nothing) with at least 10 observations, or accuracy of at least 0.5; or not validated but at least 10 supporting observations; or interpolation over a short gap |
| **limited supporting evidence** | masked error near or above the mean's; a group too small, so the overall value was used; a run of consecutive missing values longer than `max_gap_for_strong` (1); or a `lookup` mapping whose agreement was not measured |
| **insufficient evidence** | fewer than 3 supporting observations, or a fixed value with no stated source |
| not an estimate | an explicit label such as "Missing" or "Unknown"; it marks the gap rather than filling it |

The cut-offs:

- **0.5** for a normalised MAE: the fill is off by less than half a standard deviation on average, a useful estimate.
- **0.9**: a little better than filling with the mean, so something is gained.
- **30 observations**: the conventional point where a mean or median stabilises.
- **10**: the minimum for a group statistic to mean anything.

These are working judgements, documented so they can be changed, not universal laws.

`suitable_for_downstream` turns the level into advice:

- **strong**: use the value, and keep the indicator in models.
- **moderate**: fine for description; for inference, keep the indicator and check how sensitive the result is.
- **limited**: description only.
- **insufficient**: treat the value as missing.

Every imputed value keeps its `<column>_was_missing` indicator unless the plan turns it off. An imputed value is an estimate, never the original. Say so wherever filled data feeds a result.

## 3. Multiple imputation

Single imputation understates uncertainty: standard errors computed on filled data are too small. When the goal is statistical inference (confidence intervals, p-values, model coefficients) and more than a few percent of an analysed variable is imputed, the defensible approach is multiple imputation:

1. Create m filled datasets (m ≥ 5; more when much is missing) with a stochastic method, such as scikit-learn's `IterativeImputer(sample_posterior=True)` with different seeds, or statsmodels' `MICE`.
2. Run the same analysis on each.
3. Pool the results with Rubin's rules: the mean of the estimates, plus within- and between-imputation variance.

dataMedic does **not** run multiple imputation automatically. Its cleaned file is one dataset with indicators, which is right for description, dashboards and most exploratory work.

When the user needs inference on imputed variables, do it as your own analysis step on `cleaned_data/` or the original data, record the m, the method and the pooled results in `analysis/`, and say why. When multiple imputation is not used, state that standard errors involving imputed values are optimistic.
