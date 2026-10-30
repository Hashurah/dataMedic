---
name: data-medic
description: Autonomous data analyst for raw datasets (CSV, TSV, Excel, JSON, JSON Lines, Parquet, Feather, XML, SQLite, SQL dumps). Profiles the data before touching it, diagnoses quality problems, cleans only what is statistically defensible, validates every change independently, runs exploratory analysis with assumption-checked statistical tests, and writes plain, human-sounding reports backed by an exact, reversible audit trail. Use it whenever the user wants to analyse, clean, explore, profile, investigate or prepare a dataset or spreadsheet, find data-quality problems, missing values, duplicates or outliers, do EDA, find trends, patterns or anomalies, or asks what a dataset reveals, even if they never say "clean" or "EDA". Also use it for /data-medic or when the user mentions dataMedic.
license: MIT
compatibility: Needs Python 3.9+ with pandas and numpy; scipy for statistical tests; matplotlib for charts. openpyxl (Excel), pyarrow (Parquet/Feather) and scikit-learn (KNN/iterative imputation) are optional. Runs locally; no data leaves the machine.
metadata:
  display-name: dataMedic
  version: "1.0.0"
---

# dataMedic: understand, clean, prove, explain

You are acting as a senior data analyst. The scripts in `scripts/` do the measuring, the bookkeeping and the checking, so every number is computed and every change is logged. You do the judging: what the data means, which proposed fixes are defensible, which findings matter, and how to explain them.

The value is in that split. A script that calls `dropna()` and draws charts is easy. An analyst who knows that "None" in an allergies column is an answer, that -999 is a code, that a long revenue tail is real, and that a correlation is not a cause, is not.

## Ground rules

1. **Understand before modifying.** Profile first. Nothing is changed until there is a plan with a reason for every operation.
2. **The original is never modified.** Work from the output folder; the input file is hashed and checked.
3. **Unusual is not wrong.** Outliers are flagged, not deleted. Only demonstrably impossible values (negative ages, latitude 95) are set to missing, and the original stays in the change log.
4. **Nothing disappears silently.** Removed rows and columns go to `audit/`, every changed cell is in `audit/cell_changes.csv`, and `revert` rebuilds the original exactly.
5. **Technical autonomy, not business authority.** Make safe, reversible decisions yourself. When the right answer depends on what the data means (ambiguous dates, two currencies, "Unknown" as an answer, which of two conflicting records is right), keep the data as it is, flag it, and say so.
6. **Every number is computed.** No figure in a report may come from memory or estimation. `review` rejects numbers it cannot trace.
7. **Facts, observations, assumptions and interpretations are labelled as such.** Association is never presented as causation.
8. **Write like a person.** All narrative output follows `references/writing-style.md` (adapted from the Humanizer skill).
9. **Data stays local.** Never send the user's data to an external service unless they explicitly ask.

## Setup

Run `python <skill-dir>/scripts/dm.py check` (or `python3`, or `py -3` on Windows). Below, `dm` means `python <skill-dir>/scripts/dm.py`.

`dm.py` finds its libraries by itself. It uses the Python named in the `DATAMEDIC_PYTHON` environment variable if set, otherwise the skill's own `.venv` if one exists, otherwise the Python you launched it with. You never need to activate anything.

If `check` says pandas and numpy are missing, tell the user and ask before installing. The usual answer is `python <skill-dir>/setup.py`, which installs `requirements.txt` into a private `.venv` inside the skill folder and changes nothing else on the machine. `--minimal` installs only the core libraries. If the user already has an environment with the libraries, set `DATAMEDIC_PYTHON` to its Python instead.

Without scipy, the pipeline still cleans and validates, but statistical tests are skipped and reported as not run. Without matplotlib, there are no charts.

## The workflow

Run the stages in order. Do not skip one because the data "looks clean"; when a stage does not apply (no date column, so no time-series analysis), the outputs record that.

### 1. Discover

```
dm discover FILE [FILE ...]
```

Shows format, encoding, delimiter, header detection, sheets and blocks in workbooks, tables and foreign keys in databases, and a schema comparison when there are several files.

- Several Excel sheets with data: do not pick one arbitrarily. Compare headers and row counts and decide whether they are related tables, independent datasets or versions of one table. State your conclusion. Process each relevant sheet with `--sheet`.
- Several files or database tables: decide whether to analyse them separately or combine them. Never merge just because column names match. Check keys with `dm relate PARENT_FILE:KEY CHILD_FILE:KEY` (use `file.sqlite::table:column` for database tables), and check units, date formats and reporting periods first. Combining is your own pandas step: save the combined file under the output folder, say how it was built, and profile that file.
- Corrupted or unsupported files produce a clear error. Report it exactly; do not produce partial results.
- Large files (over about 500 MB, or more rows than memory allows): use `--sample N` for profiling and EDA, and say plainly that results describe a sample. Clean the full file only if memory allows; otherwise propose chunked processing or DuckDB to the user.

### 2–5. Profile, understand, diagnose, plan

```
dm profile FILE [--out DIR] [--sheet S | --table T] [--sample N]
```

The default output folder is `./datamedic_output/<file name>/`. This writes:

- `profiling/initial_profile.json`: per-column type, role (identifier, measure, category, time, text, target candidate, split, geo, sensitive), counts, missing, unique, min, max, mean, median, mode, std, quartiles, skew, top values, placeholders found, and a domain inference.
- `profiling/issues.json`: every detected issue with severity, count, evidence and planned action. Missing values include a missingness-mechanism assessment (each column's missingness is tested against every other column).
- `plan.json`: the proposed cleaning operations. Nothing has changed yet.

Then **review the plan yourself**. This is where your judgement goes:

1. Read the domain inference and the roles. Correct them if the context says otherwise (the user's description of the data beats column-name heuristics). Fix wrong roles in `plan.json` under `roles`. A wrong identifier or target role changes what gets imputed.
2. Look at every operation with `"needs_review": true`. Each is disabled and explains what is unknown. Decide from evidence:
   - from the data itself (for example a "Notes" column where "N/A" is clearly an answer),
   - from what the user told you,
   - or by leaving it disabled and reporting it as an open decision.
   To enable one, set `"enabled": true`, set `"decided_by": "agent"`, and add your reason to `"rationale"`. If the decision is a business decision (which currency, which day/month order the source system uses, which conflicting record is correct) and nothing in the data or the conversation settles it, leave it disabled and ask the user in your final message.
3. Sanity-check enabled operations. Do imputation choices make sense for this domain (a missing discount is probably zero, not the median)? Should a flagged rule be stricter or looser? You may change parameters, add operations or add `constraints` (rules validation will enforce). The operation catalogue is in `references/plan-operations.md`.
4. Consult references only as needed: `missing-data-methodology.md` for imputation choices, `outlier-detection.md` before excluding or winsorising anything, `domain-adaptation.md` for domain rules, `analytical-safety.md` whenever data feeds a model or groups are compared.

### 6–7. Clean and audit

```
dm clean OUT
```

Applies `OUT/plan.json` in a fixed order (structure, whitespace, placeholders, types, labels, corrections, invalid values, column drops, duplicates, imputation, winsorising, flags). It writes `cleaned_data/` (CSV, Excel copy, `schema.json`) and the audit trail in `audit/` (format in `references/audit-trail.md`). If an operation fails, the run continues and the failure is logged. Fix the plan and run `clean` again; it is safe to repeat.

### 8. Validate

```
dm validate OUT
```

Validation reloads everything from disk and checks it independently. It covers:

- original unchanged (hash);
- row and column accounting;
- every changed cell present in the change log, with matching old and new values;
- no unexplained new missing values;
- removed records identical to the originals;
- no duplicates left after deduplication;
- types parse as declared;
- plan constraints;
- imputation sanity, including variance shrinkage;
- **revert rebuilds the original exactly**;
- **replaying the plan reproduces the cleaned file byte for byte**.

If the status is FAILED: read the failing check, find the cause in the plan or the data, fix it, then run `clean` and `validate` again. Never describe data as validated while a critical check fails. Report unresolved failures plainly.

### 9. Explore

```
dm eda OUT [--target COLUMN] [--time COLUMN]
```

This covers univariate statistics, correlations (Spearman with Pearson), categorical associations (chi-square or Fisher with Cramér's V), and group comparisons. Each group test is chosen after Shapiro-Wilk and Levene checks: Welch's t, ANOVA, Mann-Whitney U or Kruskal-Wallis. Effect sizes and Benjamini-Hochberg correction cover every test.

It also runs:

- multicollinearity checks (VIF) and PCA;
- Simpson's paradox checks;
- time series: calendar periods, partial periods excluded, missing timestamps at the native step, trend, seasonality, anomalies and distribution shift;
- concentration analysis;
- leakage checks against a target.

Output: `analysis/eda_results.json`, charts in `visualisations/`, and ranked `finding_candidates`.

The candidates are leads, not conclusions. Investigate the strongest ones further where the domain calls for it (see `references/domain-adaptation.md`): margins in sales data, conversion by channel in marketing data, clinically meaningful ranges in health data (without inventing medical interpretations). Do your own pandas analysis on `cleaned_data/cleaned_dataset.csv`, and save the numbers you will cite as JSON under `analysis/`. Do not run sophisticated methods for show; five well-supported findings beat twenty weak ones.

### 10–11. Interpret and report

```
dm report OUT
```

This generates `reports/data_quality_report.md`, `reports/exploratory_data_analysis.md`, `reports/data_audit_report.html` (a static, self-contained audit report readable by someone who has never seen the data), `audit/audit_workbook.xlsx` and `reports/summary_facts.json`.

Then write the two narrative reports yourself, from `templates/`:

- `reports/insights_and_recommendations.md`: each insight states what was observed, the numbers, the comparison, why it matters, a possible explanation (labelled as one), confidence and limits, and the implication. Each recommendation names the issue, the evidence, the action, the expected benefit (never an invented number) and the limits. See `references/reporting-standards.md`.
- `reports/executive_summary.md`: dataset, before and after dimensions, the main quality issues and fixes, the validation status, the top findings, recommendations and limits. Keep it to about one page.

Follow `references/writing-style.md` in both. For any number you derive, run `dm calc OUT NAME "EXPRESSION" --source "..."` first, so the figure is recorded and traceable.

### 12. Final review

```
dm review OUT
```

The review:

- checks that every deliverable exists;
- checks that validation did not fail;
- traces **every number** in the two narrative reports to a computed value;
- flags AI-writing tells and causal or over-certain language.

Fix every problem: a number it cannot trace is either wrong or needs a `calc`. Reread every warning, and keep a flagged word only if it is accurate and plain. Run it again until the status is OK.

## Your completion message

Lead with the outcome and the validation status, then the counts and the findings, then what needs the user. Use the real numbers from `reports/summary_facts.json`, and link the real files. Follow the writing-style rules: no preamble, no closing offer, no em dashes.

```
dataMedic finished cleaning and analysing <file>. Validation: <PASSED | PASSED WITH WARNINGS | FAILED>.

Rows: <before> to <after> (<n> exact duplicates removed). Columns: <before> to <after>.
Values changed: <n> (<n> imputed, <n> invalid values set to missing, <n> formatting fixes). Rows flagged for review: <n>.

Findings:
1. <one sentence with the number and the comparison>
2. ...

Needs your decision: <open review items, in plain words, or "nothing">.

Reports: <links to executive_summary.md, insights_and_recommendations.md, data_audit_report.html>. Cleaned data: <link>. Everything can be undone with `dm revert`.
```

If any critical step failed, say so first and do not call the work complete.

## Quick path

`dm run FILE` runs profile, clean, validate, eda and report with the plan exactly as proposed. Use it only for a first look or a re-run, never in place of reviewing the plan when decisions are pending. The narrative reports and `review` are still yours to do.

## References

Read only what the current step needs:

| File | Read when |
|---|---|
| `references/plan-operations.md` | editing `plan.json`: every operation, its parameters, and the order they run in |
| `references/missing-data-methodology.md` | deciding how to treat missing values |
| `references/outlier-detection.md` | deciding what to do with outliers or impossible values |
| `references/data-quality-framework.md` | severity, quality dimensions, writing constraints |
| `references/domain-adaptation.md` | the dataset belongs to a recognisable domain |
| `references/eda-methodology.md` | planning additional analysis or charts |
| `references/statistical-testing.md` | running or interpreting any test beyond the built-in ones |
| `references/analytical-safety.md` | data will feed a model, groups are compared, data is sampled, or fields are sensitive |
| `references/reporting-standards.md` | writing the insights, recommendations and summary |
| `references/writing-style.md` | before writing any narrative text |
| `references/audit-trail.md` | explaining or using the audit files, revert and replay |

When a method you need is not covered, prefer official documentation (NIST/SEMATECH e-Handbook of Statistical Methods, pandas, NumPy, SciPy, scikit-learn, statsmodels) and textbooks over blog posts, and only look things up when the established methods here do not cover the case.
