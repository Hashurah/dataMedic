# dataMedic

An [Agent Skill](https://agentskills.io) that turns a coding agent (Claude Code, OpenAI Codex and others) into a careful data analyst. Give it a raw dataset and you get back the following:

- a cleaned file;
- a record of every change, which can rebuild your original exactly;
- independent validation;
- exploratory analysis with proper statistical tests;
- reports written in plain language, where every number traces back to a calculation.

It is a skill, not an app: a `SKILL.md` that tells the agent how to work, plus Python scripts that do the measuring and bookkeeping. Your data never leaves your machine.

## What it does differently

Most "clean my data" scripts call `dropna()`, fill gaps with the mean and delete outliers. dataMedic doesn't:

- **It profiles before it touches anything,** and writes a cleaning plan in which every operation has a reason. You (or the agent) can read and edit the plan before anything changes.
- **Unusual is not wrong.**
  - Outliers are flagged, not deleted.
  - Only demonstrably impossible values, such as a negative age, are set to missing.
  - A long revenue tail is recognised as a tail.
- **It knows when not to decide.**
  - "None" in an allergies column is an answer.
  - 03/04/2024 is ambiguous.
  - A column mixing $ and € can't be converted.
  - A missing star rating shouldn't be invented.

  These are proposed but held for a decision, and the reasons are written down.
- **Imputation is tested, not assumed.**
  - Before a method is chosen, known values are hidden and every plausible method (mean, median, group statistics, time interpolation, KNN, iterative) tries to fill them.
  - It switches only to a clearly better method, and records the methods it rejected.
  - Every filled cell gets an evidence record (supporting observations, spread, gap length, test result, limitations) and a plain evidence level instead of an invented confidence number.
- **Rows are not thrown away lightly.** A row can be:
  - kept, corrected or imputed;
  - flagged;
  - excluded from one analysis only;
  - quarantined until someone resolves it;
  - or removed.

  Conflicting duplicates are resolved by completeness or recency, and the differences are recorded. Long gaps in a time series are left open rather than drawn over.
- **Business rules and contracts have provenance.** A rule needs a source. Two rules that disagree stop the run rather than one silently winning. A data contract is checked before and after cleaning. Quality is reported per dimension, with accuracy marked "unverified" unless there is a reference to check against.
- **Every change is reversible.** Every changed cell is logged with its old and new value, and removed and quarantined rows and removed columns are kept. `dm revert` rebuilds the original and checks it cell by cell; `dm restore --op` undoes a single step in a copy.
- **It checks its own work.** Validation re-reads everything from disk. It confirms that the original file is untouched, that the rows and columns add up, that every change is logged, that revert is exact, and that replaying the plan reproduces the output byte for byte.
- **Statistics are chosen, not assumed.**
  - Tests are picked after normality and variance checks.
  - Effect sizes are reported alongside p-values.
  - All tests are corrected for multiple comparisons.
  - It checks for Simpson's paradox, collinearity, leakage and partial time periods.
- **The writing sounds like a person.** The reports follow writing rules adapted from the [Humanizer](https://github.com/blader/humanizer) skill. A final review rejects any number in the narrative that does not match a computed result, and flags AI-style phrasing and causal claims the data can't support.

## Install

Two commands: get the skill, then install its Python libraries.

```sh
git clone https://github.com/Hashurah/dataMedic.git ~/.claude/skills/data-medic
python ~/.claude/skills/data-medic/setup.py
```

On Windows, use `py -3` instead of `python` if `python` isn't found. On macOS and Linux, `python3` may be the name.

`setup.py` creates a private environment in the skill's `.venv` folder and installs everything in `requirements.txt` there. It doesn't touch your system Python or other projects. `scripts/dm.py` then uses that environment automatically, so there is nothing to activate. Other options:

- `python setup.py --minimal` installs only the core libraries (pandas, numpy, scipy, matplotlib).
- `python setup.py --extra` also installs plotly (interactive report charts) and rich (terminal tables).
- `python setup.py --check` shows what is installed.
- To uninstall the libraries, delete the `.venv` folder.

**Already have an environment with pandas?** You can skip `setup.py` and either install into it yourself with `pip install -r requirements.txt`, or point dataMedic at it by setting `DATAMEDIC_PYTHON` to that environment's Python executable.

`python scripts/dm.py check` shows which Python and libraries dataMedic is using. If you skip installation, the agent will notice when it first runs and ask before installing anything.

Where to clone:

- The folder must be called `data-medic` (skill names are lowercase with hyphens).
- For a single project, clone into `.claude/skills/data-medic` instead.
- For OpenAI Codex, clone into Codex's skills folder (for example `~/.codex/skills/data-medic`). Codex has not been tested yet; the skill uses only instructions and Python scripts, so it should work the same way.

Requirements:

- Python 3.9 or later (tested on 3.12 and 3.14), with pandas 2 or 3, numpy and scipy;
- matplotlib for charts;
- optional, installed by default: openpyxl (Excel), pyarrow (Parquet and Feather), scikit-learn (KNN and iterative imputation, Isolation Forest), seaborn (statistical charts);
- optional, with `--extra`: plotly (interactive charts in the HTML report), rich (terminal tables).

Each optional feature degrades gracefully without its library, and `dm check` says what changes.

## Use

Ask in plain words: "clean and analyse sales.xlsx", "what does this CSV tell us?", "find the data quality problems in customers.parquet". You can also invoke it with `/data-medic`. The agent then runs the stages below:

| Stage | Command | Output |
|---|---|---|
| Discover | `dm discover FILE` | format, encoding, sheets and tables, keys, schema comparison |
| Profile, understand, diagnose, plan | `dm profile FILE` | `profiling/`, `plan.json` (nothing changed yet) |
| (agent reviews the plan) | edit `plan.json` | decisions recorded with reasons |
| Clean and audit | `dm clean OUT` (`--safe-only` or `--max-risk moderate` for a conservative pass) | `cleaned_data/`, `audit/` |
| Validate | `dm validate OUT` (`--contract FILE`) | `audit/validation_results.json` |
| Explore | `dm eda OUT` | `analysis/eda_results.json`, `visualisations/` |
| Report | `dm report OUT` (`--interactive`) | quality and EDA reports, interactive HTML audit report, Excel audit workbook |
| Interpret | agent writes from `templates/` | `executive_summary.md`, `insights_and_recommendations.md` |
| Final review | `dm review OUT` | every number traced; style and claims checked |

Here `dm` is `python ~/.claude/skills/data-medic/scripts/dm.py`, and `OUT` defaults to `./datamedic_output/<file name>/`. `dm run FILE` runs the scripted stages with the proposed plan for a quick first look.

Other commands:

- `dm revert OUT --to original.csv` rebuilds the original;
- `dm restore OUT --op OP-007` writes a copy of the cleaned data with one step undone;
- `dm compare OUT column` scores imputation methods for one column on hidden known values;
- `dm relate a.csv:id b.csv:a_id` checks keys and orphans between tables;
- `dm baseline OUT --halt` saves a run's category shares, means and missing rates as a contract, so `dm run next_batch.csv --contract OUT/baseline_contract.json` stops before analysis if the new batch drifts;
- `dm calc OUT name "expression"` records a derived figure so the review can trace it;
- `dm audit OUT` prints the operation log;
- `dm check` shows which Python and libraries are in use.

## Supported formats

- CSV, TSV and other delimited text: delimiter and encoding are detected, including decimal commas, cp1252 files, preamble lines and ragged rows;
- Excel `.xlsx` (and `.xls` with xlrd): sheets and blank-row-separated blocks;
- JSON (records, or records nested under a key) and JSON Lines;
- Parquet and Feather;
- flat XML;
- SQLite databases and SQLite-compatible SQL dumps: tables, primary keys and foreign keys.

Corrupted and unsupported files produce a clear error, not partial results.

## Outputs

```text
datamedic_output/<dataset>/
├── plan.json               every cleaning decision and its reason
├── original_reference/     read-only copy of the input
├── cleaned_data/           cleaned_dataset.csv, .xlsx, schema.json
├── profiling/              initial and final profiles, issues, missingness diagnostics, imputation comparison
├── audit/                  operation log, cell changes, removed and quarantined rows, removed columns, flags,
│                           imputation evidence, row status, validation, Excel workbook
├── analysis/               EDA results, multivariate anomalies, derived calculations
├── visualisations/         distributions, relationships, correlations, time series, missingness, chart index
├── restored/               copies with one step undone (only if dm restore was used)
├── reports/                executive summary, insights, data quality, EDA, HTML audit report
└── metadata/               manifest (hashes, versions, seed, stages), reproducibility config, final review
```

## Tests

```sh
python -m pytest tests -q
```

141 tests run against synthetic datasets with planted problems. The datasets cover:

- sales;
- heavy and patterned missingness;
- exact, formatting-only and conflicting duplicates;
- outliers;
- mixed and ambiguous date formats;
- inconsistent labels;
- mixed types and currencies;
- skewed data;
- time series with gaps;
- related tables with orphans;
- legitimate extreme values;
- empty columns;
- misleading placeholders;
- correlated variables;
- sensitive fields;
- impossible values and broken logic;
- ragged, corrupted and unsupported files;
- eight file formats.

The tests check that:

- the original never changes;
- every decision is logged;
- every removed record is recoverable and revert is exact;
- imputed values are marked;
- duplicate counts are right;
- statistics match independent calculations;
- outliers are never deleted;
- reports agree with the data;
- validation catches tampering;
- runs are reproducible;
- train/test leakage is prevented;
- the review rejects invented numbers;
- installation helpers pick the right Python and pass exit codes through;
- duplicates are judged before columns are dropped, and dependencies and risk limits hold steps back;
- quarantine, analytical exclusion and conflicting-duplicate choices are recorded and reversible;
- business rules need a source and conflicting rules change nothing;
- long time-series gaps stay open, groups are filled separately, and forward fill never uses later values;
- the masked-value comparison cannot see the hidden values and picks the clearly better method;
- every imputed cell has an evidence record and no numeric confidence;
- Little's test separates random from value-related missingness;
- encoding repair is exact, and formula-like text stays text in Excel;
- contracts are checked before and after, and conflicting rules are reported;
- multivariate anomalies are flagged and discrete scales are declined;
- the HTML report is offline, escapes data, masks sensitive columns, and embeds plotly inline;
- terminal output is plain when captured.

They pass on Windows 11 with Python 3.14 and pandas 3.1, and with Python 3.12 and pandas 2.3.

## Limits

- Data is loaded into memory as text. The full pipeline has been measured up to 101,000 rows (`references/scalability.md`); beyond what fits in memory, use `--sample` (results are then labelled as sample-based) or pre-aggregate with DuckDB.
- Domain rules (impossible ranges) cover common fields. Add your own in `config/domain_rules.json`.
- The scripts measure; the agent judges. Decisions that depend on business meaning (which currency, which of two conflicting records is right) are left open and reported, never guessed.
- `audit/` and `cleaned_data/` contain real values, including sensitive ones, because reversibility needs them. The reports mask sensitive columns.

## Licence

MIT. `references/writing-style.md` adapts the MIT-licensed [Humanizer](https://github.com/blader/humanizer) by Siqi Chen; see `THIRD_PARTY_NOTICES.md`.
