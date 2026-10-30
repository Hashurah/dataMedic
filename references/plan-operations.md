# Plan operations

`plan.json` is the single source of truth for what `clean` does. Edit it, then run `dm clean OUT`; every run starts again from the original file, so edits are always safe.

## Plan structure

```json
{
  "dataset": {"file": "...", "sha256": "..."},
  "load_options": {"sheet": "Orders"},
  "settings": {"seed": 42, "config": {}},
  "roles": {"identifier": [], "measure": [], "category": [], "time": [], "target_candidate": [],
            "split": [], "geo": [], "sensitive": []},
  "review_required": ["OP-004"],
  "operations": [
    {"op_id": "OP-001", "op": "convert_type", "params": {"column": "order_date", "to": "date", "date_order": "dmy"},
     "issue": "dates stored as text", "rationale": "...", "evidence": {}, "enabled": true,
     "needs_review": false, "decided_by": "auto"}
  ],
  "constraints": [{"column": "order_id", "rule": "unique", "why": "..."}]
}
```

When you change an operation, set `"decided_by": "agent"` (or `"user"` if the user decided) and update `"rationale"` with the reason. The audit report shows both. Disabled operations stay in the log as "not applied", so open decisions remain visible.

The plan records the input file's sha256. `clean` refuses to run a plan against a different version of the file.

## Execution order

Operations run in this order regardless of their position in the file (ties keep file order):

1. `rename_columns`
2. `drop_rows`
3. `trim_whitespace`
4. `standardize_missing`
5. `convert_type`
6. `standardize_categories`
7. `replace_values`
8. `set_missing_where`
9. `drop_columns`
10. `drop_duplicates`
11. `impute`
12. `winsorize`
13. `flag_rows`

The order matters. Text is trimmed and standardised before duplicates are compared, so " ACME" and "Acme" match. Duplicates are removed before imputation, so copies do not bias the fill values. Flags are evaluated on the final values.

## Operations

| Operation | Parameters | Effect |
|---|---|---|
| `rename_columns` | `mapping` {old: new} | Renames columns. Use sparingly; names are kept as given unless they break lookups. |
| `drop_rows` | `reason`, and one of `empty: true`, `row_ids: [..]`, `rule: {..}` | Moves rows to `audit/removed_records.csv`. Only for rows with no information or a documented reason. |
| `trim_whitespace` | `columns`, `collapse_internal` (bool) | Strips surrounding spaces; optionally collapses repeated inner spaces. |
| `standardize_missing` | `column`, `tokens` | Replaces exact (trimmed) placeholder tokens with missing. |
| `convert_type` | `column`, `to` (`number`, `integer`, `date`, `datetime`, `boolean`, `string`), `decimal` (`.` or `,`), `date_order` (`dmy`, `mdy` or null), `allow_compact` (accept 20240131), `unit`, `on_error` (`set_missing` or `abort`) | Parses text. Values that fail become missing, are flagged as "conversion failure", and keep their original text in the change log. Integer conversion falls back to number if decimals appear. Mixed time-zone awareness stops the conversion. |
| `standardize_categories` | `column`, `mapping` {variant: canonical} | Merges label variants. |
| `replace_values` | `column`, `mapping`, `reason` | Corrections backed by independent evidence (a lookup table, the user's confirmation). Never for guesses. |
| `set_missing_where` | `column`, `rule`, `reason`, `classification` | Sets values matching the rule to missing and flags the rows. For impossible values and missing-value codes. |
| `drop_columns` | `columns`, `reason` | Moves columns to `audit/removed_columns.csv`. For empty or clearly irrelevant columns (and say why). |
| `drop_duplicates` | `reason`, `subset` (null = all columns), `keep` (`first`, `last`, `most_complete`), `ignore_case` | Removes duplicates; each removed row records `retained_row_id`. |
| `impute` | `column`, `method`, plus method parameters (below), `add_indicator` (default true), `fit_on` | Fills missing values; adds `<column>_was_missing`. |
| `winsorize` | `column`, `lower_q`, `upper_q`, `fit_on` | Clips to quantiles. Never automatic; needs a documented analytical reason. |
| `flag_rows` | `rule`, `reason`, `column`, `classification` | Lists rows in `audit/flagged_records.csv`. Changes nothing. |

### Imputation methods

| `method` | Extra parameters | Use when |
|---|---|---|
| `mean` | none | numeric, roughly symmetric, low missing share, no pattern in the missingness |
| `median` | none | numeric and skewed or with outliers |
| `mode` | none | categorical, very low missing share |
| `constant` | `value` | the value is known from the domain (a missing discount that means 0) or for an explicit "Missing" category |
| `group_median`, `group_mean`, `group_mode` | `group_by` (column or list) | missingness or values depend on a group (MAR); groups without data fall back to the overall value, which is logged |
| `interpolate` | `order_by` (date or sequence column), `limit` | evenly spaced time series; time-weighted; never extrapolates past the first or last observed value |
| `ffill`, `bfill` | `order_by`, `limit` | carrying a status or slowly changing value forward (or back) in time |
| `knn` | `features` (numeric columns), `n_neighbors` | several correlated numeric columns; needs scikit-learn; features are standardised |
| `iterative` | `features` | multivariate model-based imputation; needs scikit-learn; seeded |

`fit_on: {"column": "split", "values": ["train"]}` computes the statistics on those rows only and applies them everywhere. It is set automatically when a train/test split column is detected, to prevent leakage.

### Rules (for `set_missing_where`, `flag_rows`, `drop_rows`)

| Rule | Matches |
|---|---|
| `{"lt": x}`, `{"gt": x}`, `{"outside": [lo, hi]}` | numeric comparisons |
| `{"not_integer": true}` | values with decimals |
| `{"values": [..]}` | exact values (compared as canonical text) |
| `{"future": true, "grace_days": 1}` | dates after today |
| `{"before": "1900-01-01"}` | dates before a date |
| `{"date_order": ["start_col", "end_col"]}` | rows where the end is before the start |
| `{"total_mismatch": ["qty", "price", "total"], "tolerance": 0.01}` | rows where qty x price differs from total by more than 1% |
| `{"duplicate_key": ["col", ..]}` | rows sharing the same non-empty values in these columns |
| `{"outlier": "iqr" / "iqr_log" / "mad" / "z", "k": 1.5}` | statistical outliers (`iqr_log` applies IQR on the log scale for positive skewed data) |
| `{"row_ids": [..]}` | specific rows by `dm_row_id` |

Missing values never match a rule.

## Constraints

Constraints are assertions that `validate` enforces on the cleaned data:

```json
{"column": "order_id", "rule": "unique"}
{"column": "age", "rule": "range", "min": 0, "max": 120}
{"column": "status", "rule": "allowed", "values": ["open", "paid", "void"]}
{"column": "email", "rule": "not_null"}
{"columns": ["start_date", "end_date"], "rule": "date_order", "severity": "warning"}
```

Add the constraints the domain implies. A failed constraint is a critical failure unless you give it `"severity": "warning"`, which you should do only when violations are known and flagged rather than fixed (for example dates out of order that were flagged for review).
