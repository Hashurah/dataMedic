# Row decisions: retain, correct, impute, flag, quarantine, exclude, remove

A problem in one cell is rarely a reason to lose the whole row. A customer with a missing income still counts as a customer. Pick the lightest action that solves the problem.

| Decision | How | The row afterwards |
|---|---|---|
| **retain** | do nothing | in the cleaned data, unchanged |
| **correct** | `replace_values`, `set_missing_where`, `winsorize` | in the cleaned data; old values in `cell_changes.csv` |
| **impute** | `impute`, `apply_rule` | in the cleaned data; evidence in `imputation_evidence.csv`, indicator column added |
| **flag** | `flag_rows` | in the cleaned data, values unchanged, listed in `flagged_records.csv` |
| **exclude from an analysis** | `flag_rows` with `exclude_from: [columns]` or `["all"]` | in the cleaned data; `eda` leaves the rows out of those columns' statistics and reports how many and why |
| **quarantine** | `quarantine_rows` | outside the trusted data, whole and unchanged, in `quarantined_records.csv`, until someone resolves the issue |
| **remove** | `drop_rows`, `drop_duplicates` | outside the data for good reason (an empty row, an exact copy); original in `removed_records.csv` |

**Physical deletion** is for rows with no information or exact copies. **Quarantine** is for rows that may be valid but cannot be trusted yet, such as a negative amount that may be a refund or an error. **Analytical exclusion** is for rows that are fine to keep and count, but would distort one particular statistic.

Every one of these is reversible: `dm revert` rebuilds the original exactly, and `dm restore --op` undoes a single step.

## Before removing or quarantining a row

Answer these in the step's rationale:

1. Why: the rule and the evidence that the problem is real.
2. Could it be corrected or imputed instead? Could it stay with a flag?
3. How many rows, and what share of each group? Removing 2% overall can be 40% of one region.
4. What does it do to the analyses that follow? A removed row also disappears from counts.

The planner never proposes quarantine or removal of rows with content. Those are your decisions, made with a reason. Their risk is `high`, so `--safe-only` and `--max-risk moderate` runs hold them back.

## Row status file

`audit/row_status.csv` has one line per original row:

| Field | Values |
|---|---|
| `record_status` | `retained`, `retained, flagged`, `standardised` (formatting, types or labels only), `corrected`, `imputed`, `quarantined`, `removed` (the most consequential that applies) |
| `quality_flags` | the flag classifications on the row |
| `imputed_columns`, `corrected_columns` | which cells were filled or corrected |
| `exclusion_reason`, `quarantine_reason` | why the row left the cleaned data |
| `analytical_exclusions` | the columns the row is excluded from, and why |
| `analytical_eligibility` | `eligible`, `eligible except the listed exclusions`, or why it is not in the cleaned data |

The HTML report's "Inspect a record" box reads the same information.

## Duplicates

| Kind | How to detect | Default action |
|---|---|---|
| exact | identical in every column after formatting fixes | removed, first occurrence kept (`drop_duplicates`) |
| case-only | identical except letter case | reported; enable `ignore_case` if case carries no meaning |
| identifier | the same ID on several rows | flagged. It may be an update, an entry error or a one-to-many relationship. |
| content | identical except the ID | flagged as a possible double entry |
| business key | the same key columns (customer + date + amount) | your decision: `drop_duplicates` with `subset` |
| near or fuzzy | similar text (`Jon Smith` / `John Smith`) | label variants are found per column (`standardize_categories`); fuzzy matching of whole records is not automated, so check candidates with your own pandas step and record the decision |

Repeated observations are not duplicates. Two identical sales on the same day can be two customers. Judge that from the domain and an identifier, not from equality alone.

Rows that share a business key but differ in other columns are **conflicting duplicates**. Do not keep the first one by habit. Choose by:

- completeness (`keep: "most_complete"`);
- recency (`keep: "latest"`, `order_by: "updated_at"`);
- or a business rule (which source system is authoritative).

Each removed row records the row kept (`retained_row_id`) and the columns where it differed (`dm_differing_columns`), so the choice can be checked and reversed.

Duplicates are always judged on every recorded column, because deduplication runs before any column is dropped. Validation checks against the same columns.
