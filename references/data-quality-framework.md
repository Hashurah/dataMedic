# Data quality framework

## Dimensions

| Dimension | Question | How dataMedic measures it |
|---|---|---|
| Completeness | Are values present? | missing cells, per column and overall (completeness %) |
| Uniqueness | Is each real-world record present once? | exact duplicates, duplicates after trimming and case-folding, duplicate identifiers, same content under different IDs |
| Validity | Do values fit their type and allowed range? | parse failures, impossible values (domain rules), constraint checks |
| Consistency | Do the same things look the same, and do related fields agree? | label variants, mixed units and currencies, end before start, quantity x price against total, mixed date formats |
| Accuracy | Do values match reality? | not measurable from the data alone; flag anomalies and ask for a reference source |
| Timeliness | Is the data current and complete for its period? | date range, future dates, partial and empty periods |
| Integrity | Do related tables agree? | key uniqueness, orphans, cardinality (`dm relate`) |

## Severity

| Severity | Meaning | Examples |
|---|---|---|
| critical | the analysis would be wrong or impossible | the table cannot be parsed; a key relationship is broken in most rows |
| high | a material share of rows or a key column is affected | conflicting duplicate IDs; impossible values; mixed currencies; more than 30% missing; dates in conflicting formats |
| medium | correctable or limited, but worth reporting | label variants; placeholders; outliers in over 5% of rows; unusual values |
| low | cosmetic or small | whitespace; empty rows; a few outliers |
| info | context, not a problem | constant columns, high cardinality, rare categories, numeric-looking codes kept as text |

Severity is about impact on the analysis, not the number of rows. One wrong currency in a revenue column outranks a thousand trailing spaces.

## Issue kinds detected

- **Structure:** header problems (blank or duplicated names), preamble lines, ragged rows (padded or quarantined), blank lines, empty rows, empty columns, constant and near-constant columns, high cardinality, encoding damage (mojibake).
- **Missing values:** system placeholders, word placeholders, numeric codes, with a mechanism assessment.
- **Types:** numbers as text (currency, percent, thousands separators, decimal comma, parentheses for negatives), dates as text (ISO, day-first, month-first, named months, time zones), booleans, mixed types, numeric-looking identifiers kept as text.
- **Text:** surrounding and repeated spaces, case and unicode variants, possible misspellings (similar labels; never merged automatically).
- **Duplicates:** exact, formatting-only, conflicting identifiers, identical content under different IDs.
- **Values:** impossible (domain rules), unusual (expected ranges), statistical outliers (IQR, log-IQR, MAD, z), missing-value codes.
- **Logic:** date order, quantity x price against total, future and pre-1900 dates, birth dates after events.
- **Units:** mixed currencies, mixed percent and plain numbers.
- **Relations:** non-unique parent keys, orphaned child rows, formatting-only key mismatches.

## Writing constraints

Constraints in `plan.json` turn knowledge into checks that validation enforces. Add them when the domain or the user implies them:

- identifiers that must be unique (`unique`);
- ranges (`range`), such as `0 ≤ discount_pct ≤ 100`;
- allowed sets (`allowed`) for status codes;
- required fields (`not_null`);
- event order (`date_order`).

A constraint that the current data violates, and that you chose to flag rather than fix, should carry `"severity": "warning"`, so validation reports it without failing.

## Validation checks

`dm validate` runs these from disk, independently of the cleaning run:

| Check | Fails when |
|---|---|
| original file unchanged; reference copy intact | the hash differs |
| row ids unique; no row both kept and removed | bookkeeping is broken |
| row accounting | original ≠ cleaned + removed |
| column accounting | original ≠ cleaned + removed − added indicators |
| every changed value is in the change log | the cleaned value differs from the original with no log entry |
| change log old/new values match | the logged chain does not end at the cleaned value or start at the original |
| no unexplained new missing values | more values became missing than the log explains |
| removed records equal the original rows | the quarantine is incomplete or altered |
| no exact duplicates remain (if deduplication ran) | duplicates remain |
| every value matches its declared type | a value does not parse as the schema says |
| plan constraints | any constraint is violated |
| imputation filled the column; spread kept | values are still missing, or sd shrank > 20% (warnings) |
| operations ran | an operation failed |
| revert rebuilds the original exactly | the audit trail cannot reproduce the input |
| replaying the plan reproduces the cleaned data | the cleaning is not deterministic |

The status is `PASSED`, `PASSED WITH WARNINGS` or `FAILED`. Open decisions (disabled operations needing review) are listed separately; they are not failures, but they belong in the final message.
