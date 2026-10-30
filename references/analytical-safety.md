# Analytical safety

Problems that make an analysis wrong even when every calculation is correct. Check the ones that apply before you write findings.

## Data leakage (modelling)

When the data will train a model, fit every preprocessing step (imputation values, scaling, encoding, feature selection, outlier bounds) on training rows only, then apply it to the test rows.

- dataMedic detects split columns (`split`, `set`, `fold` with values such as train and test) and sets `fit_on` for imputation automatically. Add `fit_on` to `winsorize` too.
- If no split exists yet, say that cleaning statistics were computed on all rows, and recommend fitting them inside a scikit-learn `Pipeline` at modelling time.
- `eda` flags possible target leakage: predictors almost perfectly correlated with the target, or categories that map to one target value. Ask whether the column is known before the outcome.

## Temporal leakage

For anything predictive over time, use only information available at prediction time. Do not interpolate or back-fill across the train/test time boundary. Do not use full-period statistics (such as the overall median) as features. Split by time, not at random.

## Survivorship bias

Data about what remains (current customers, surviving firms, completed orders) hides what dropped out. Ask what the dataset leaves out. Rows removed in cleaning are themselves a selection: compare them with retained rows when many were removed (the audit's `removed_records.csv` makes this possible).

## Selection bias and representativeness

- Who is in the data, and how were they chosen? Survey respondents, app users or customers who complained are not the population.
- State the population your findings describe ("customers who ordered in 2023"), and avoid generalising beyond it.
- Weighted surveys need their weights in every estimate.

## Sampling limitations

- `--sample` results describe the sample. Say so, with the sample size, and give intervals rather than point estimates for key figures.
- Small groups give unstable rates and low test power. Report n for every group.
- A non-significant result in a small sample is weak evidence of no effect.

## Simpson's paradox and confounding

An overall relationship can reverse within groups. dataMedic checks the strongest correlations within each grouping column and reports reversals. Before attributing a difference between groups to the grouping, ask what else differs between those groups (size, mix, period, region). Compare like with like: stratify, or compare within strata.

## Multicollinearity

Highly correlated predictors (abs(rho) ≥ 0.85, VIF above 5–10) make regression coefficients unstable and individually meaningless. Report the overlap, then keep one variable, combine them, or use a method that handles it. Never claim one of two near-duplicate variables "matters more".

## Multiple testing

Many tests produce chance findings. Use the BH q-values that `eda` reports. Count your own extra tests into the correction. Treat findings from exploratory slicing ("in the 25–34 age group on Tuesdays") as hypotheses for a follow-up, not conclusions.

## Distribution shift

If a measure's distribution changed between periods (the KS test in `eda`), check whether the definition, unit, source system or collection method changed before reporting a trend. Comparisons across a break in measurement are invalid.

## Non-independence

Repeated rows for the same customer, patient, machine or day are not independent observations. Aggregate to the right unit first, or use methods for clustered data. Otherwise the p-values are too small.

## Sensitive information

- Mark personal and confidential columns as sensitive in `plan.json` roles. Reports mask them.
- Do not print sensitive values in your messages, and do not quote individual records.
- Never send data to external services (APIs, web tools, online converters) without the user's explicit permission.
- Tell the user that `audit/` and `cleaned_data/` contain the real values.
