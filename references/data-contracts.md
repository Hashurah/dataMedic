# Data contracts and business rules

## Where a rule comes from

Use this hierarchy. A higher source overrides a lower one, and a conflict between two sources at the same level is reported, never resolved silently.

1. **Explicit user rules**: what the user states ("a missing discount means no discount").
2. **Verified domain rules with provenance**: a policy, a data dictionary, a regulation, with a reference.
3. **Constraints the data supports**: an ID that is unique in the source, a range the data never leaves.
4. **Statistical inference**: imputation, outlier flags.
5. **Human review**: anything else stays open and is reported.

A missing warranty field is not "no warranty" because the product is electronics. It is 24 months only if the warranty policy says so, and then the policy is the `source`.

## Business rules in the plan: `apply_rule`

```json
{"op_id": "OP-030", "op": "apply_rule", "risk": "moderate", "enabled": true, "decided_by": "user",
 "issue": "warranty not recorded", "rationale": "Policy fixes the warranty for phones.",
 "params": {"rule_id": "BR-01",
            "description": "Phones sold in 2024 carry a 24-month warranty",
            "when": [{"column": "category", "op": "eq", "value": "phone"},
                     {"column": "order_date", "op": "ge", "value": 20240101}],
            "set": {"warranty_months": 24},
            "source": "Warranty policy v3, section 2 (supplied by the user 2026-10-30)",
             "only_if_missing": true}}
```

Each rule records:

- **ID and description**;
- **condition** (`when`), using the operators in `plan-operations.md`;
- **action** (`set`);
- **source**: required, and a rule without one is refused;
- **priority**: its position in the plan; rules run in plan order;
- **scope**: the rows matched by `when`; `only_if_missing` limits it to blanks;
- **validation**: add a contract rule that checks the outcome.

Filled cells go into `imputation_evidence.csv` with method "domain rule" and the source, and count as strong evidence. They are only as good as the rule.

**Conflicts.** Suppose a later rule would set a cell to a different value than an earlier rule set. The later step fails with the first conflicting cell named, and changes nothing. Fix the rules (narrow a condition, or decide which source wins) and run `clean` again.

## Contracts: what the cleaned data must satisfy

Put a contract in `OUT/contract.json`, or pass `dm validate OUT --contract FILE`. Validation checks every rule on the original data (with the cleaned schema's types) and on the cleaned data, and reports both counts.

```json
{"rules": [
  {"id": "C1", "rule": "required_columns", "columns": ["order_id", "order_date", "amount"], "provenance": "confirmed"},
  {"id": "C2", "rule": "type", "column": "amount", "type": "number", "provenance": "user"},
  {"id": "C3", "rule": "range", "column": "age", "min": 0, "max": 120, "provenance": "confirmed",
   "source": "data dictionary v2"},
  {"id": "C4", "rule": "unique", "columns": ["order_id"], "provenance": "confirmed"},
  {"id": "C5", "rule": "allowed", "column": "status", "values": ["open", "paid", "void"], "provenance": "user"},
  {"id": "C6", "rule": "not_null", "column": "customer_id", "max_missing_share": 0.0, "provenance": "user"},
  {"id": "C7", "rule": "date_range", "column": "order_date", "min": "2020-01-01", "max": "today", "provenance": "inferred"},
  {"id": "C8", "rule": "pattern", "column": "postcode", "regex": "[A-Z]{1,2}\\d[A-Z\\d]? ?\\d[A-Z]{2}", "provenance": "user"},
  {"id": "C9", "rule": "compare", "left": "ship_date", "op": ">=", "right": "order_date", "provenance": "confirmed"},
  {"id": "C10", "rule": "conditional", "when": [{"column": "status", "op": "eq", "value": "paid"}],
   "then": [{"column": "payment_date", "op": "not_missing"}], "provenance": "user"},
  {"id": "C11", "rule": "reference", "column": "customer_id", "file": "customers.csv", "key": "id", "provenance": "confirmed"}
]}
```

| Provenance | Meaning | Default severity |
|---|---|---|
| `confirmed` | a verified business rule | critical |
| `user` | the user's stated expectation | critical |
| `inferred` | suggested by the data (for example the plan's unique ID) | warning |
| `provisional` | an assumption to be checked | warning |

Set `severity` explicitly to override the default.

**Conflicting rules** are reported as warnings and not resolved. Examples: ranges that do not overlap; allowed lists that share nothing; one column declared with two types; a range that contains none of the allowed values. Decide which rule holds, fix the contract, then validate again.

## What passing proves

Passing a contract shows the data is **consistent** with the rules. It does not show the values are **accurate**. A plausible but wrong age passes a 0 to 120 range.

Accuracy needs an external reference: a trusted source, a sample checked by hand, or ground truth. Without one, report accuracy as unverified. `validation_results.json` does this under `quality_dimensions.accuracy`.

## Quality dimensions

Validation reports each dimension separately, before and after cleaning, and never as one combined score:

| Dimension | Measured as |
|---|---|
| completeness | missing cells (including placeholders) of all cells |
| uniqueness | exact duplicate rows |
| validity | type, constraint and contract checks passed; contract violations before and after |
| consistency | rows flagged as logically inconsistent (dates out of order, totals that do not add up) |
| integrity | original unchanged, row and column accounting, change log complete, revert and replay |
| accuracy | `unverified` unless an external reference is supplied |
