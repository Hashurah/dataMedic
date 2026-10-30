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
     "needs_review": false, "decided_by": "auto", "risk": "safe", "depends_on": []}
  ],
  "constraints": [{"column": "order_id", "rule": "unique", "why": "..."}]
}
```

When you change an operation, set `"decided_by": "agent"` (or `"user"` if the user decided) and update `"rationale"` with the reason. The audit report shows both. Disabled operations stay in the log as "not applied", so open decisions remain visible.

The plan records the input file's sha256. `clean` refuses to run a plan against a different version of the file.

## Risk levels and run modes

Every operation has a `risk`. The planner sets it, and you may change it with a reason.

| Risk | Meaning | Default for |
|---|---|---|
| `safe` | cannot lose information: formatting, recognised placeholders, label case, lossless type conversion, removing empty rows or columns, flags | `rename_columns`, `trim_whitespace`, `standardize_missing`, `extract_currency`, `standardize_categories`, `flag_rows`, and conversions where every value parses |
| `moderate` | changes values in a documented, reversible way that a reader should know about | `convert_type` with failures, `fix_encoding`, `replace_values`, `set_missing_where`, `drop_duplicates`, `impute`, `apply_rule`, `convert_currency` |
| `high` | removes rows or columns with content, or reshapes a distribution | `drop_rows` (except empty rows), `drop_columns` (except empty columns), `quarantine_rows`, `winsorize` |

- `dm clean OUT` applies every enabled step.
- `dm clean OUT --safe-only` applies only `safe` steps.
- `dm clean OUT --max-risk moderate` applies steps up to that level.

Held-back steps are logged as "not applied (risk ... above this run's limit)". Validation replays with the same limit.

For an unfamiliar dataset, `dm profile` changes nothing. Run `--safe-only` first when the user wants a quick, conservative result, then enable higher-risk steps one decision at a time.

## Execution order and dependencies

Operations run in this fixed stage order, whatever their position in the file (ties keep file order). Each stage needs the ones before it:

1. `rename_columns`
2. `drop_rows`: only empty rows by default. Placeholders are not yet normalised, so a row of `N/A`s is not empty here. Add a `drop_rows` rule if such rows should go.
3. `trim_whitespace`
4. `fix_encoding`
5. `standardize_missing`: placeholders become missing before types are inferred.
6. `extract_currency`: currency markers are copied out before type conversion strips them.
7. `convert_type`: types come before any numeric rule or statistic.
8. `standardize_categories`
9. `replace_values`
10. `set_missing_where`: impossible values and missing-value codes become missing before duplicates are compared.
11. `drop_duplicates`: compares every recorded column, before any column is dropped and before rules or imputation fill values. Two rows that differ only in a column that is dropped later are not duplicates.
12. `drop_columns`
13. `apply_rule`: documented business rules fill or set values.
14. `quarantine_rows`: rows set aside here are not used to fit imputation.
15. `impute`: fitted on the remaining, deduplicated rows.
16. `convert_currency`: converts the final amounts, after invalid values are cleared and gaps filled.
17. `winsorize`
18. `flag_rows`: evaluated on the final values.

`"depends_on": ["OP-003"]` makes a step run only if those steps were applied. If a prerequisite is disabled, held back by the risk limit or failed, the step is logged as "not applied (depends on ...)".

A dependency on a step that runs later in the stage order fails with an explanation, and so does one on an unknown step. Because the order is fixed and dependencies may only point backwards, cycles cannot occur.

## Operations

| Operation | Parameters | Effect |
|---|---|---|
| `rename_columns` | `mapping` {old: new} | Renames columns. Use sparingly; names are kept as given unless they break lookups. |
| `drop_rows` | `reason`, and one of `empty: true`, `row_ids: [..]`, `rule: {..}` | Moves rows to `audit/removed_records.csv`. Only for rows with no information or a documented reason. |
| `trim_whitespace` | `columns`, `collapse_internal` (bool) | Strips surrounding spaces; optionally collapses repeated inner spaces. |
| `fix_encoding` | `columns` | Re-decodes UTF-8 text that was read as Windows-1252 (`cafÃ©` becomes `café`). Changes a value only if the round trip is exact and leaves no garbling markers. |
| `standardize_missing` | `column`, `tokens` | Replaces exact (trimmed) placeholder tokens with missing. |
| `extract_currency` | `column`, `to_column` (default `<column>_currency`), `symbols` {marker: code}, `unmarked` (default `"Unmarked"`) | Adds a column with each amount's currency (£ becomes GBP, $ USD, € EUR; ISO codes such as `USD 12` are read too). Amounts with no marker get `unmarked`; missing amounts stay missing. The amount itself is not changed, so its `convert_type` can then run without losing the currency. `$` is read as USD: pass `symbols: {"$": "CAD"}` when the source says so. Revert drops the column. |
| `convert_type` | `column`, `to` (`number`, `integer`, `date`, `datetime`, `boolean`, `string`), `decimal` (`.` or `,`), `date_order` (`dmy`, `mdy` or null), `allow_compact` (accept 20240131), `unit`, `on_error` (`set_missing` or `abort`) | Parses text. Values that fail become missing, are flagged as "conversion failure", and keep their original text in the change log, so a failure is never silent. Integer conversion falls back to number if decimals appear. Mixed time-zone awareness stops the conversion. Use `on_error: "abort"` when no failure is acceptable. |
| `standardize_categories` | `column`, `mapping` {variant: canonical} | Merges label variants. |
| `replace_values` | `column`, `mapping`, `reason` | Corrections backed by independent evidence (a lookup table, the user's confirmation). Never for guesses. |
| `set_missing_where` | `column`, `rule`, `reason`, `classification` | Sets values matching the rule to missing and flags the rows. For impossible values and missing-value codes. |
| `drop_duplicates` | `reason`, `subset` (null = all columns; a list = business key), `keep` (`first`, `last`, `most_complete`, `latest`, `earliest`), `order_by` (for `latest`/`earliest`), `ignore_case` | Removes duplicates. Each removed row records `retained_row_id` and `dm_differing_columns`, the columns where it differs from the kept row. Rows that share a business key but differ elsewhere are conflicting duplicates. Decide which to keep by completeness (`most_complete`), recency (`latest`) or a business rule, never by file order alone without saying so. |
| `drop_columns` | `columns`, `reason` | Moves columns to `audit/removed_columns.csv`. For empty or clearly irrelevant columns (and say why). |
| `apply_rule` | `rule_id`, `when` (conditions), `set` {column: value}, `source` (required), `only_if_missing` (default true), `description` | Applies a documented business rule. Two rules that would set the same cell to different values fail the second step and change nothing. See `data-contracts.md`. |
| `quarantine_rows` | `rule`, `reason`, `column` | Moves rows out of the cleaned data into `audit/quarantined_records.csv` (and `removed_records.csv`) until an issue is resolved. Their row status is "quarantined". |
| `impute` | `column`, `method`, plus method parameters (below), `add_indicator` (default true), `fit_on`, `source` | Fills missing values, adds `<column>_was_missing`, and writes one evidence record per filled cell to `audit/imputation_evidence.csv`. |
| `convert_currency` | `column` (numeric amount), `currency_column`, `rates_file`, `target`, `date_column` (needed for dated rates), `output_column` (default `<column>_<target>`), `decimals` (default 2) | Writes `<output_column>`, `<column>_fx_rate` and, for dated rates, `<column>_fx_rate_date`. The amount and currency columns are kept. `rates_file` is a CSV you supply (relative paths resolve against the folder holding `plan.json`) with `currency` and `rate` (units of `target` per one unit), and optionally `date`: each row then uses the latest rate on or before its date. Rows in the target currency get rate 1. Rows whose currency has no rate (such as `Unmarked`), or with no date or a date before the first rate, stay unconverted and are flagged "not converted". The file's sha256 is logged, and validation recomputes every converted value. Rates are never fetched from the internet. |
| `winsorize` | `column`, `lower_q`, `upper_q`, `fit_on` | Clips to quantiles. Never automatic; needs a documented analytical reason. |
| `flag_rows` | `rule`, `reason`, `column`, `classification`, `exclude_from` | Lists rows in `audit/flagged_records.csv`. Changes nothing. `exclude_from: ["income"]` (or `["all"]`) also leaves those rows out of the named columns' analysis in `eda`. The rows stay in the cleaned data. |

### Imputation methods

| `method` | Extra parameters | Use when |
|---|---|---|
| `mean` | none | numeric, roughly symmetric, low missing share, no pattern in the missingness |
| `median` | none | numeric and skewed or with outliers |
| `mode` | none | categorical, very low missing share |
| `constant` | `value`, `source` | the value is known from the domain (a missing discount that means 0; give the `source`) or for an explicit "Missing" category |
| `group_median`, `group_mean`, `group_mode` | `group_by` (column or list) | missingness or values depend on a group (MAR). Groups without data fall back to the overall value; the fallback is logged and graded down in the evidence. |
| `interpolate` | `order_by` (date or sequence column), `group_by` (one series per group), `max_gap` (steps), `max_gap_duration` (e.g. `"3D"`), `limit` | evenly spaced time series. Time-weighted, and never extrapolates past the first or last observed value. Runs longer than `max_gap` are left missing whole (never partly filled). Uses later values, so it is for describing the past, not for forecasting. |
| `ffill`, `bfill` | `order_by`, `group_by`, `max_gap`, `max_gap_duration`, `limit` | carrying a status or slowly changing value forward (uses only earlier values) or back (uses later values) |
| `knn` | `features` (numeric columns), `n_neighbors` | several correlated numeric columns; needs scikit-learn; features are standardised |
| `iterative` | `features` | multivariate model-based imputation; needs scikit-learn; seeded |
| `lookup` | `source` (column), `mapping` {source value: fill}, `match` (`exact` or `first_word`) | the value follows from another column by a rule the user supplies or confirms (title to gender, city to country). Unmatched rows stay missing, so follow with a `constant` fill if needed. Test the rule on rows where both columns are recorded first and report the agreement rate. |

The planner does not pick a method by a rule of thumb alone. For each column it proposes a method, then compares the plausible alternatives on hidden known values (see `imputation.md`). It switches to another method only when that method is clearly better (by default 10% lower error with no larger bias), and never to a continuous estimate on a discrete scale. The comparison and the rejected methods are in the step's `evidence.strategy_comparison`, and the reason is in its rationale. `dm compare OUT COLUMN` reruns the comparison after you change the plan.

`fit_on: {"column": "split", "values": ["train"]}` computes the statistics on those rows only and applies them everywhere. It is set automatically when a train/test split column is detected, to prevent leakage.

### Rules (for `set_missing_where`, `flag_rows`, `drop_rows`, `quarantine_rows`)

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

### Conditions (for `apply_rule` and contracts)

`when` is a list of conditions that must all hold: `{"column": "category", "op": "eq", "value": "Electronics"}`.

- Operators: `eq`, `ne`, `in`, `not_in`, `lt`, `le`, `gt`, `ge`, `missing`, `not_missing`.
- A missing value matches only `missing`.

## Constraints

Constraints are assertions that `validate` enforces on the cleaned data:

```json
{"column": "order_id", "rule": "unique"}
{"column": "age", "rule": "range", "min": 0, "max": 120}
{"column": "status", "rule": "allowed", "values": ["open", "paid", "void"]}
{"column": "email", "rule": "not_null"}
{"columns": ["start_date", "end_date"], "rule": "date_order", "severity": "warning"}
```

The planner adds the constraints the data supports: unique identifiers and the impossible ranges it enforced. These are *inferred*. Add the constraints the domain implies.

A failed constraint is a critical failure unless you give it `"severity": "warning"`. Do that only when violations are known and flagged rather than fixed, for example dates out of order that were flagged for review.

For rules the user states or confirms, with provenance, before/after counts and conflict detection, use a data contract (`data-contracts.md`).
