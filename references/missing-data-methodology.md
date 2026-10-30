# Missing data methodology

## 1. Find all the missing values

A value is missing if it is empty, whitespace, or a token that means "not recorded". dataMedic treats tokens in three tiers (config `missing_tokens`):

- **Always missing:** `NULL`, `NaN`, `#N/A`, `#VALUE!`, `<NA>` and similar. These are system spellings of missing in any column.
- **Missing in numeric or date columns:** `NA`, `N/A`, `None`, `-`, `?`, `unknown`, `TBD` and similar. In a column that is otherwise numbers or dates, a word cannot be a value.
- **Review in text columns:** the same words in a text or category column. "None" can be an allergy answer, "N/A" can mean "not applicable", which differs from "unknown", and "Unknown" can be a real category in a survey. These are proposed but disabled until reviewed.

Numeric codes (`-999`, `9999`) are detected when they repeat and sit far outside the rest of the data. They are set to missing automatically only when they are also impossible for the field (a weight of -999 kg); otherwise they wait for review. Zero is never treated as missing: a zero count or zero balance is information.

## 2. Describe the missingness

For each column: count, share, and whether it concentrates in some groups. dataMedic tests whether the missingness indicator is related to every other column:

- against categorical columns: chi-square (Fisher for 2x2 with small counts), effect size Cramér's V;
- against numeric columns: Mann-Whitney U comparing the column's distribution when the value is missing vs present, effect size rank-biserial r;
- Benjamini-Hochberg correction across all these tests, with a q-value below 0.01 and an effect of at least 0.1 required to call it a relationship.

The mechanism is then assessed, never asserted:

| Mechanism | Meaning | What the data can show |
|---|---|---|
| MCAR (missing completely at random) | unrelated to anything | consistent with "no relationship found", but never proven |
| MAR (missing at random) | related to other observed columns | supported when missingness is related to observed columns |
| MNAR (missing not at random) | related to the missing value itself (high earners skip the income question) | cannot be tested from the observed data; raise it when the domain makes it plausible |

Always report the uncertainty. "No relationship with the observed columns was found; MNAR cannot be ruled out."

`profile` also writes `profiling/missingness.json` (shown in the HTML report's Missing values section):

| Diagnostic | What it shows | Caveat |
|---|---|---|
| row patterns | which combinations of columns are missing together, and how often | descriptive |
| columns missing together | pairs with a Jaccard overlap of at least 0.5 | a shared cause (one form page, one source system) is likely |
| structural candidates | every row missing A is also missing B | often a skipped question by design ("car make" when "has car" is no), which is not lost data and must not be imputed |
| over time | missing rate per period, chi-square across periods | a jump usually marks a change in collection, not in the world |
| Little's MCAR test | one chi-square test of MCAR across the numeric columns (EM estimates) | assumes multivariate normality; low power with few rows or many patterns; rejecting it supports MAR or MNAR, not rejecting it does not prove MCAR |

## 3. Choose a treatment

| Situation | Default | Reason |
|---|---|---|
| Identifier column | leave missing | an invented ID is a false record link |
| Outcome / target column | leave missing | imputing the outcome invents results; for modelling, drop those rows from training instead |
| Missing share over 40% | leave missing; add an indicator if analysis needs one | the filled values would mostly report the fill value |
| Numeric, 30–40% | proposed, disabled | decide whether filling is defensible for the analysis at hand |
| Numeric, evenly spaced time series | time interpolation, no extrapolation, runs over 3 steps left missing | neighbours in time are the best evidence (see `time-series.md`) |
| Numeric, missingness related to a group (MAR) | group median or mean | a single overall value would bias the groups |
| Numeric, skewed (abs skew > 0.5) or with outliers | median | the mean is pulled by the tail |
| Numeric, roughly symmetric | mean | |
| Categorical, at most 2% missing, no pattern | mode | low impact |
| Categorical, otherwise | leave missing; an explicit "Missing" category is proposed | keeps the rows visible without guessing a label |
| Value known from the domain | `constant` (for example discount 0) | only when the meaning is certain; record why |
| Several correlated numeric columns | `knn` or `iterative` (scikit-learn) | uses the relationships; seeded and logged |

This table gives the first proposal. The planner then compares it with the alternatives on hidden known values and switches only to a clearly better method. The comparison, the rejected methods and the reason are recorded in the step (see `imputation.md`).

Never:

- fill with the minimum or with zero because it is convenient;
- impute identifiers;
- drop large parts of the dataset to avoid missing values (listwise deletion biases results unless the data is MCAR, and loses power even then);
- impute before splitting train and test data (see `analytical-safety.md`).

## 4. Keep the information that values were missing

Every imputation adds `<column>_was_missing` (true/false) unless you turn it off. The change log lists every filled cell, and `audit/imputation_evidence.csv` records what supports each estimate and how reliable it is (`imputation.md`). Analyses that are sensitive to imputation, such as variance, correlations and tests, can then be re-run on observed values only.

## 5. Check the effect

Validation compares each imputed column's distribution before and after. Single-value imputation (mean, median, mode) shrinks the variance and weakens correlations. A drop in standard deviation of more than 20% raises a warning; mention it when you report tests on that column.

## Sources

- Little, R. J. A., and Rubin, D. B. *Statistical Analysis with Missing Data*, 3rd ed. Wiley, 2019.
- van Buuren, S. *Flexible Imputation of Missing Data*, 2nd ed. CRC Press, 2018 (free online).
- pandas documentation, "Working with missing data"; scikit-learn documentation, "Imputation of missing values".
