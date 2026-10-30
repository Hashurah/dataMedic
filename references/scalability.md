# Scalability

Read this when a file is large (tens of thousands of rows or more), when a run is slow, or before promising a user how long something will take.

## Measured results

`tests/benchmark.py` builds a synthetic messy sales table (9 columns: text dates, mixed-case regions, prices with `$` and thousands separators, `N/A`/`NULL`/blank placeholders, `-999` codes, 1% exact duplicates) and runs the full pipeline in a fresh process per size. Results are written to `tests/benchmark_results.json`.

Measured on Windows 11, Python 3.14.6, all optional libraries installed:

| Rows | Profile + plan | Clean | Validate (incl. revert + replay) | EDA + charts | Reports | Total | Peak memory |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 10,100 | 12.7 s | 8.3 s | 9.0 s | 4.7 s | 5.0 s | 39.6 s | 294 MB |
| 101,000 | 119.6 s | 79.7 s | 126.4 s | 9.2 s | 24.2 s | 359.1 s | 868 MB |

Output sizes at 101,000 rows: input 6.5 MB, audit trail 8.6 MB, HTML report 2.5 MB. Validation PASSED at both sizes.

What this shows:

- Profiling, cleaning and validation grow roughly linearly with rows. Validation is the most expensive stage at scale because it reverts the whole file and replays the plan from scratch to prove both are exact.
- EDA and report time grow slowly, because charts and the HTML preview use capped samples (`report.preview_rows`, chart limits in `config/default.json`).
- Peak memory was about 130 times the CSV size at 101,000 rows. Everything is held as text first (so nothing is silently coerced), which costs memory.
- These are one machine and one synthetic shape. Wide tables, long free-text columns and many categorical columns cost more. Do not quote these numbers to a user as a promise for their data; measure with `--sample` first if timing matters.

## What to do with larger data

1. **Profile a sample first.** `dm profile FILE --sample N` (for example 50,000). Every result that comes from a sample is labelled as such. Review the plan on the sample.
2. **Clean the full file when memory allows.** A rough rule from the measurements: plan for about 1 GB of RAM per 100,000 rows of a table this shape, more for wide or text-heavy tables.
3. **Turn off what you don't need on very large runs:**
   - `imputation_comparison.enabled: false` skips the masked-value comparison, which reruns the imputation several times per column. Imputation then uses the rule-based method, and the evidence file says no comparison was run.
   - `imputation_comparison.max_masked` caps the masked values per repeat (default 2,000), so the comparison cost stops growing with the file.
4. **Beyond memory:** pre-aggregate or filter with DuckDB or the source database, save the result under the output folder, say how it was built, and run dataMedic on that file. Chunked cleaning is not built in.

## Deferred

These were considered and left out of this version:

- a DuckDB backend for profiling and cleaning out of core;
- a JSONL or SQLite audit index (the cell change log is a CSV, which is fine at the measured sizes but slow to search at tens of millions of changes);
- chunked processing of the cleaning engine;
- progress bars for long stages.
