<!--
Template for reports/insights_and_recommendations.md. Replace every {{...}} and delete these comments.
Start from the finding_candidates in analysis/eda_results.json, check each against the data and the
domain, and keep only the well-supported ones. Five strong insights beat twenty weak ones.
See references/reporting-standards.md for the parts of an insight and references/writing-style.md for tone.
Every number must be traceable (`dm calc` for derived figures; save extra analyses as JSON in analysis/).
-->
# Insights and recommendations: {{dataset name}}

{{One paragraph: the analytical question, the data used (rows after cleaning, period), and any limit that applies to everything below (a sample, a short period, imputed values in a key column).}}

## Insights

### 1. {{The finding as a full sentence, for example "Electronics produced four fifths of revenue from two fifths of orders."}}

{{What was observed, with the numbers and n. The comparison: an even split, an earlier period, another group.}}

{{Evidence from tests where relevant: test, q-value, effect size and its magnitude.}}

{{Why it matters for the user's goal.}}

{{A possible explanation, labelled as one ("One explanation is ...; the order-level price data would confirm it"). Leave this out if there is no reasonable explanation.}}

Confidence: {{high, medium or low, and why: sample size, data quality issues that touch this finding, confounding, period covered.}}

### 2. {{...}}

## What the data does not show

{{Analyses the user might expect that the data cannot support, in one line each (for example "No cost column, so margins cannot be measured"), and relationships that were tested and not found, where that is useful.}}

## Recommendations

### {{Action as an imperative, for example "Fix the currency field at the source"}}

- Issue: {{the problem or opportunity}}
- Evidence: {{insight number and the key figure}}
- Action: {{what to do, specifically}}
- Expected benefit: {{in words; a number only if it was calculated}}
- Limits: {{what could make this wrong}}

## Method notes

{{Short: cleaning decisions that affect these insights (imputation in a column used here, flagged outliers kept), tests used, and multiple-testing correction. Point to data_quality_report.md and the audit report for the rest.}}
