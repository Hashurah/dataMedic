# Audit trail, revert and replay

The audit trail must let someone who never saw the original understand every change, and rebuild the original from the outputs.

## Row identity

Every row gets `dm_row_id`: its position in the loaded original table (1 = first data row, after any preamble and header). It stays in the cleaned data so any cleaned row can be traced back. Drop it only in a copy you hand to other tools.

## Files

| File | One row per | Key fields |
|---|---|---|
| `audit/transformation_log.json` / `.csv` | operation (including disabled, held-back and failed ones) | `op_id`, `timestamp`, `stage`, `execution_position`, `op`, `column`, `issue`, `method`, `params`, `risk`, `depends_on`, `affected` (values changed), `flagged`, `statistical_basis` (fill values, bounds, distributions before and after), `evidence` (including `strategy_comparison` for imputation), `rationale`, `decided_by`, `needs_review`, `status`, `reversible`, `reversal` |
| `audit/cell_changes.csv` | changed cell | `op_id`, `dm_row_id`, `column`, `old_value`, `new_value` (canonical text; empty = missing) |
| `audit/removed_records.csv` | removed or quarantined row | `op_id`, `dm_row_id`, `reason`, `retained_row_id` (for duplicates: the copy that was kept), `dm_disposition` (`removed` or `quarantined`), `dm_differing_columns` (for duplicates: columns where it differs from the kept row), then every original column with its original value |
| `audit/quarantined_records.csv` | quarantined row | the quarantined subset of `removed_records.csv` (written only when rows were quarantined) |
| `audit/imputation_evidence.csv` | imputed or rule-filled cell | method, value, supporting observations, group, spread, gap length, use of later values, fallback, source, masked-value result, evidence level, suitability, limitations (see `imputation.md`) |
| `audit/row_status.csv` | original row | `record_status`, `quality_flags`, imputed and corrected columns, exclusion and quarantine reasons, analytical exclusions and eligibility (see `row-decisions.md`) |
| `audit/removed_columns.csv` | row | `dm_row_id` and the original values of each removed column |
| `audit/flagged_records.csv` | flag | `flag_id`, `op_id`, `dm_row_id`, `column`, `value` (masked if sensitive), `reason`, `classification` (confirmed invalid, missing-value code, conversion failure, unresolved outlier, potential anomaly, logical inconsistency, duplicate identifier, possible duplicate, unit inconsistency), `exclude_from` (columns this row is left out of in analysis) |
| `audit/malformed_rows.csv` | source line that could not be parsed (too many fields) | line number, raw text |
| `audit/validation_results.json` | check | `check`, `passed`, `severity`, `detail`; plus `quality_dimensions` and, with a contract, each rule's violations before and after |
| `audit/revert_info.json` | run | added columns, renames, removed columns, original column order, the run's risk limit |
| `audit/audit_workbook.xlsx` | | the same tables as sheets (sensitive values masked in `removed_records`) |

Issues are distinguished by where they end up:

- **detected:** `profiling/issues.json`;
- **corrected:** log entries with `affected` > 0;
- **intentionally retained:** flags, and disabled operations with their reason;
- **flagged for review:** `flagged_records.csv`;
- **unresolved:** disabled `needs_review` operations, failed operations and failed validation checks, listed under Validation, Outstanding in the HTML report.

## How changes are captured

After each operation, the engine compares every affected column's canonical text before and after, and logs every cell that differs. An operation therefore cannot change a value without the log seeing it. The validator then checks the result independently against the original file.

Canonical text means:

- missing is empty;
- whole-number floats are written without `.0`;
- dates are `YYYY-MM-DD`, and datetimes are ISO 8601 (with an offset when time-zone aware);
- booleans are `true` and `false`.

A conversion that only changes formatting (`52.0` to `52`) is logged like any other change, and counted separately as "format only" in the reports.

## Revert

```
dm revert OUT [--to restored.csv]
```

Rebuilds the loaded original table:

1. Start from `cleaned_dataset.csv`.
2. Drop added indicator columns.
3. Undo every logged cell change in reverse order.
4. Undo renames.
5. Restore removed columns and removed rows from their original values.
6. Restore the original row and column order.

It then compares the result with the original file, cell by cell, and says whether they match. `validate` runs the same check. Malformed source lines are not part of the loaded table; they are kept in `malformed_rows.csv`, and the reference copy keeps the exact original bytes.

## Undo one step

```
dm restore OUT --op OP-007 [--to file.csv]
```

Writes a copy of the cleaned data with one step undone, to `OUT/restored/cleaned_without_OP-007.csv`, with a `_report.json` next to it. The cleaned data is never overwritten.

- Changed cells go back to their value before the step.
- Rows the step removed or quarantined come back with their original values. No other step is applied to them, so they may still need cleaning.
- A dropped column comes back.
- An imputation's indicator column is removed.

The report counts cells that later steps changed again. Restoring the step alone gives back the earlier value and drops those later changes, so check them.

Every step is undoable this way. The HTML report's Audit and recovery section lists, per step, where its originals are and what needs care.

## Replay

```
dm clean OUT
```

`clean` always starts from the original file and `plan.json`, so re-running it reproduces the cleaned data. `validate` replays the plan in a temporary folder and compares the SHA-256 of the two cleaned files. Randomised methods (iterative imputation) use the seed in `plan.json` settings. The seed, the software versions, the input hash, the plan hash and the configuration are recorded in `metadata/analysis_manifest.json` and `metadata/reproducibility_config.json`.

## Privacy

`cell_changes.csv`, `removed_records.csv`, `removed_columns.csv` and `cleaned_data/` contain real values, including sensitive ones, because reversibility requires them. The HTML report, the EDA report, the profiles and the flag file mask sensitive columns. Tell the user which folders can be shared.
