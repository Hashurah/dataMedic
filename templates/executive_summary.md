<!--
Template for reports/executive_summary.md. Replace every {{...}} and delete these comments.
Rules: about one page; every number from reports/summary_facts.json, analysis/*.json or `dm calc`;
follow references/writing-style.md (no em dashes, no inflated words, association is not causation).
`dm review` rejects untraceable numbers and leftover {{placeholders}}.
-->
# Executive summary: {{dataset name}}

{{One or two sentences: what the data is (rows, columns, period, what a row represents), its likely domain, and what was asked.}}

## Data quality

{{Two to four sentences. The main problems found, with counts. What was fixed (duplicates removed, values imputed, invalid values set to missing), what was deliberately kept (outliers flagged, not deleted), and what is still open. The validation status, exactly as reported, for example "Validation passed with warnings: imputation narrowed the spread of income by 23%."}}

Rows: {{before}} to {{after}}. Columns: {{before}} to {{after}}. Completeness: {{before}}% to {{after}}%.

## Findings

{{Three to five findings, most important first. One or two sentences each: the observation, the number, the comparison. Mark interpretations as such.}}

1. {{finding}}
2. {{finding}}
3. {{finding}}

## Recommendations

{{Two to four, each tied to a finding above. No invented benefits.}}

- {{recommendation}}

## Open decisions and limits

{{Decisions only the user can make (from the plan's disabled review items), and the limits that most affect the findings: sample size, period, missing data, observational design.}}

Full details: `data_quality_report.md`, `exploratory_data_analysis.md`, `insights_and_recommendations.md` and `data_audit_report.html`.
