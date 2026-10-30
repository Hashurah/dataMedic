# Time series: gaps, filling and leakage

## Before filling anything

`eda` reports these per date column; `profile` notes the sampling when choosing interpolation:

1. **The time column**, and whether timestamps are unique (within each series, if there are several).
2. **The expected step**: the most common interval. The series counts as regular if at least 80% of intervals equal it.
3. **Missing steps**: timestamps absent at that step, and the longest gaps.
4. **Duplicate timestamps**: more than one row at the same time in one series. Interpolation records how many and warns, because neighbours at the same time are ordered arbitrarily (by row id).
5. **Seasonality**: day of week for daily data, autocorrelation at the season lag when there are three or more cycles.
6. **Closures**: weekends, holidays and shutdowns produce gaps that are not missing data. A shop closed on Sunday has no Sunday sales, which is different from an unknown value. Never fill such periods. If the series has gaps of that kind, keep them, and if needed aggregate to a period where they do not matter.

## Missing dates

`eda` diagnoses every date column with missing values under `missing_dates` in `eda_results.json`. It fills nothing.

- **Why each date is missing**, from the audit trail: blank in the source, placeholder text, not readable as a date, ambiguous day/month order (both readings valid and different), or set missing by a rule (with the rule's reason). These need different fixes, so report them separately.
- **Whether missing dates cluster.** A Wald-Wolfowitz runs test along file order and along each numeric identifier (trailing numbers of codes such as TXN-00042 count): fewer, longer runs than chance suggest a batch or an ingestion outage. The longest run is reported against the longest expected by chance, with the missing share by tenth of the order and a chi-square test across tenths. A significant clustered result becomes a "missing data pattern" finding candidate.
- **When they happened.** Only if an order key tracks the date closely (rank correlation at least 0.9, `missing_dates.chronological_rho`), each missing date is placed approximately at its previous dated neighbour, and the missing share is given by period and by weekday with a chi-square test. Otherwise the report says the dates cannot be placed: when IDs are not issued in date order, a neighbour's date says nothing about the missing one, and filling it would invent the date.

## Filling a value inside the series

Filling a missing value at an existing timestamp is different from adding missing timestamps. dataMedic only does the first; it never invents rows for missing timestamps.

| Method | Uses | Good for | Not for |
|---|---|---|---|
| `interpolate` (time-weighted) | the observations before and after | smooth measurements, short gaps | forecasting, models evaluated in time order, step changes |
| `ffill` | only earlier observations | statuses and slowly changing values; anything where future information must not be used | measurements that move between readings |
| `bfill` | only later observations | a value known to apply from the next reading backwards | anything feeding a forecast |
| `group_median` by period or weekday | other periods | strongly seasonal series with long gaps (a typical Monday) | trends |
| `knn` / `iterative` on correlated columns | other columns in the same row | series with a closely related measured series (visits and sales) | when the related series is also missing |

The masked-value comparison (`imputation.md`) tests interpolation against the alternatives on hidden known values. In a weekly-seasonal series with a closely related column, a feature-based method can win.

### Long gaps

`max_gap` (in steps) leaves a whole run of consecutive missing values unfilled if it is longer than the limit. A long gap is never filled halfway. The planner sets `max_gap` from `time_series.max_gap_steps` (default 3).

A straight line across one to three missing readings is a fair local estimate. Across a week of missing daily data it hides whatever happened there: an outage, a promotion, a closure.

`max_gap_duration` (for example `"3D"`) does the same by elapsed time. It is better for irregular series. The run's `statistical_basis.left_missing_because_gap_too_long` counts what was left, and each filled cell records its `gap_length` in the evidence file.

### Groups

With several series in one table (stores, sensors, patients), use `group_by`. Each series is filled from its own neighbours only. Interpolating across a store boundary would mix two series.

### Future information

`interpolate` and `bfill` use later observations; the evidence records `uses_future_values: true`.

- That is acceptable when describing the past ("what were sales on the missing days?").
- It is **leakage** when the cleaned data trains or evaluates a model that predicts forward in time, because the training data would contain information from the future.

For forecasting work, use `ffill` or leave the gaps, and fit any statistics on the training period only (`fit_on`).

Report which kind of reconstruction was used: retrospective (uses both sides) or causal (uses only the past).

### Seasonal and state-space methods

Seasonal decomposition with interpolation of the residual, and Kalman smoothing (statsmodels `UnobservedComponents`, `SARIMAX`), are justified for long, strongly seasonal series with gaps longer than a few steps. They are not built in, because they need choices (season length, model order) that should be made and checked per series.

When you use them:

- do it as your own step;
- validate on hidden known values the same way as `impute_eval` (hide observed runs of similar length, not single points);
- record the model and the error in `analysis/`;
- write the filled values back through a `lookup` or `replace_values` step with a `source`, so they enter the audit trail.
