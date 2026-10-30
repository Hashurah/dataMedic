# Reporting standards

## Deliverables

```text
datamedic_output/<dataset>/
├── plan.json                         the cleaning plan, with every decision and its reason
├── original_reference/               read-only copy of the input (not made for files over 500 MB)
├── cleaned_data/                     cleaned_dataset.csv, cleaned_dataset.xlsx, schema.json
├── profiling/                        initial_profile.json, final_profile.json, issues.json
├── audit/                            transformation_log.json/.csv, cell_changes.csv, removed_records.csv,
│                                     removed_columns.csv, flagged_records.csv, malformed_rows.csv,
│                                     validation_results.json, revert_info.json, audit_workbook.xlsx
├── analysis/                         eda_results.json, derived.json, and any extra analysis you saved
├── visualisations/                   distributions/, relationships/, correlations/, time_series/, cleaning/
├── reports/                          executive_summary.md*, insights_and_recommendations.md*,
│                                     data_quality_report.md, exploratory_data_analysis.md,
│                                     data_audit_report.html, summary_facts.json
└── metadata/                         analysis_manifest.json, reproducibility_config.json, final_review.json
```

Files marked * are written by you from `templates/`. The rest are generated. Files only exist for stages that ran. The HTML report prints cleanly to PDF from a browser if the user needs a PDF.

## Kinds of statement

Keep these apart, in the text and in your head:

| Kind | Example | Wording |
|---|---|---|
| Fact (computed) | "Revenue in Q2 was 18.4% higher than in Q1." | plain past tense |
| Statistical observation | "Spend differs by channel (Kruskal-Wallis, q < 0.001, epsilon² 0.21, a large effect)." | test, q, effect size |
| Assumption | "Dates were read day-first because 41 values can only be read that way." | "we assumed", "were read as" |
| Interpretation | "This may reflect the March price change." | "may", "one explanation is", plus how to check it |
| Prediction | "If the trend continues, ..." | only with a stated model and an interval |
| Recommendation | "Consider ..." | linked to a finding |

The four analytic levels:

- **descriptive** (what happened);
- **diagnostic** (what might explain it; never presented as proven);
- **predictive** (only with a defensible model);
- **prescriptive** (what to consider doing).

## An insight

Every important insight has these parts. Prose is fine; the labels are for you.

1. **Title.** The finding as a sentence: "Electronics produced four fifths of revenue."
2. **Observation.** What was seen, specifically.
3. **Evidence.** The numbers, from the results, with n.
4. **Comparison.** Against what: an even split, the previous period, another group, a benchmark the user gave.
5. **Why it matters.** The consequence for the user's goal.
6. **Possible explanation.** Labelled as a possibility, with what would confirm it.
7. **Confidence and limits.** Sample size, data quality issues that touch this finding, confounding, period covered.
8. **Implication.** What it means for a decision.

Rank insights by statistical strength, practical size, relevance, and the reliability of the underlying data. Do not force a number of insights. If only two things are well supported, report two. "No meaningful difference between regions" is a finding when the user cares about regions.

## A recommendation

- **Issue:** the problem or opportunity, from a finding.
- **Evidence:** the insight it rests on.
- **Action:** specific and feasible.
- **Expected benefit:** in words, or a number only if it was calculated. Never invent savings, uplift or ROI. "Measuring X before and after would show the effect" is better than a made-up percentage.
- **Limits:** what could make it wrong.

Recommendations about the data itself (fix the currency field at the source, add a validation rule to the form) are often the most valuable ones.

## The executive summary

About one page, for someone who reads nothing else:

1. What the data is (rows, columns, period, domain) and what was asked.
2. Data quality in two or three sentences: the main problems, what was fixed, and what was left (with counts). The validation status, stated exactly.
3. The three to five most important findings, one sentence each with a number.
4. The top recommendations.
5. Open decisions and the most important limits.

## Numbers and traceability

Every number in the narrative reports must be in the results. `dm review` enforces this. Derived figures go through `dm calc`; extra analyses are saved as JSON under `analysis/`. Round consistently, and give denominators. A report that disagrees with the cleaned data is a failed report.

## Sensitive data

Columns marked sensitive (names, emails, phones, national IDs, dates of birth, card numbers, salaries, diagnoses) are masked in the HTML report, the EDA report and the profiles. The audit CSVs keep real values, because reversibility needs them; tell the user that the `audit/` folder contains raw data and should be shared with care. Never quote an individual's record in a narrative report. Aggregate, and avoid groups so small that individuals can be identified (fewer than 5 or 10).
