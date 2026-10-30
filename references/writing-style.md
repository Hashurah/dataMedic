# Writing style for reports and messages

Every sentence dataMedic writes for a person (the executive summary, the insights, recommendations, the completion message) must read like a careful analyst wrote it, not a chatbot. These rules adapt the [Humanizer](https://github.com/blader/humanizer) skill by Siqi Chen (MIT licence; see `THIRD_PARTY_NOTICES.md`), which is based on Wikipedia's [Signs of AI writing](https://en.wikipedia.org/wiki/Wikipedia:Signs_of_AI_writing). They are narrowed here to analytical writing.

`dm.py review` checks the narrative reports for the most common tells listed below and for causal or certain wording. A warning is a prompt to reread the sentence, not an automatic rewrite.

## Why analysis text drifts into AI style

A model picks the wording that fits the most readers, so it states importance instead of facts, adds rhythm (triads, dashes) by rule, and inflates ordinary results. An analyst writes for one reader about one dataset, so the text is specific and uneven. The fix is always the same: say the specific thing, with the number.

## How to write

1. Lead with the finding, then the number, then the comparison. "Electronics produced 79.9% of revenue, against 25% for an even split across the four categories."
2. Every sentence must add something the reader did not already have. Cut sentences that restate a table, a chart caption or the previous sentence.
3. Use plain verbs: *is*, *has*, *rose*, *fell*, *differs*. Not *serves as*, *stands as*, *boasts*, *showcases*.
4. Vary sentence length. Short and long sentences alternate in real writing.
5. Keep the right uncertainty. One qualifier where the evidence calls for it ("may", "is consistent with"); never a stack ("could potentially possibly").
6. Write numbers exactly as computed, rounded consistently. Never write a number that is not in the results (see "Numbers" below).

## Patterns to remove

Strongest first. The first five justify a rewrite on one sighting.

| # | Pattern | Example of the tell | Write instead |
|---|---|---|---|
| 1 | Not X but Y | "This is not just a dip, it's a structural shift." | "Orders fell 12% in each of the last three months." |
| 2 | One-line closers | "That distinction matters." "The message is clear." | Cut it, or replace it with the consequence as a fact. |
| 3 | Sayings that sound deep | "At its core, the real question is retention." | "Retention explains most of the revenue gap: ..." |
| 4 | Staged run-ups | "Let's dive in." "Here's what you need to know." | Start with the finding. |
| 5 | Arguing with no one | "This isn't to say pricing doesn't matter." | State the claim you are making. |
| 6 | Forced triads | "faster, smarter and more efficient" | Name the one or two things the data shows. |
| 7 | Repeated openings | "Revenue rose. Revenue peaked. Revenue fell." | Merge, or lead with the action. |
| 8 | Dashes as connectors | "Sales rose — mainly in Q4 — after the launch." | Use commas, a colon, parentheses or two sentences. No em or en dashes. |
| 9 | Stacked qualifiers | "could potentially indicate" | "may indicate" |
| 12 | Stock AI words | delve, crucial, pivotal, testament, showcase, underscore, landscape, robust (figurative), valuable insights, key (as an adjective) | Plain words, or nothing. |
| 13 | Inflated significance | "marks a pivotal moment for the business" | State the change and its size. |
| 15 | Shallow -ing riders | "..., highlighting the importance of retention." | Cut the rider or make it a sentence with evidence. |
| 16 | Sales language | "a stunning 40% jump", "game-changer" | "a 40% increase" |
| 17 | Borrowed authority | "Experts agree that..." | Cite the actual source, or cut. |
| 19 | Bold as decoration | "**Revenue:** Revenue increased" | Bold only what a skimming reader must not miss. |
| 20 | Decorative headings | "## Key Insights And Strategic Takeaways 🚀" | "## Findings" (sentence case, no emoji). |
| 22 | Chat residue | "I hope this helps! Let me know if..." | Remove. |
| 23 | Knowledge-limit fillers | "While specific details are limited, it appears..." | Say what the data does not show. |
| 25 | Writing about the document | "The table below compares..." | Let the table speak; say what it shows. |
| 26 | Re-explaining what the reader knows | A completion message that restates the request before the result. | Lead with the result. |

(Numbers follow the Humanizer pattern list so the source can be checked.)

## Analysis-specific rules

These matter more in a data report than any word choice:

- **Association is not causation.** Observational data shows that things move together. Write "was associated with", "differed by", "is consistent with". Use "caused", "drives", "leads to", "because of" only when the design supports it (a controlled experiment, or a mechanism the data itself records, such as revenue = quantity x price). `review` warns on causal verbs.
- **Significant means statistically significant.** Never use "significant" for "large". Report effect sizes next to p-values; a tiny p with a negligible effect is not a finding.
- **Label the kind of statement.** Facts (computed), statistical observations (test results), assumptions (choices you made), interpretations (possible explanations). Interpretations get "may" or "one explanation is", and a way to check them.
- **No invented benefits.** Never estimate savings, revenue uplift or percentages that were not calculated. Say "this could reduce returns; measuring the return rate before and after would show by how much."
- **Name the denominator.** "18% of orders", not "18%". "In the 300 rows with a recorded date", not "in the data".
- **State the limits once, where they apply.** Not a stock "Limitations" paragraph of generic caveats.

## Numbers

Every number in `executive_summary.md` and `insights_and_recommendations.md` must come from the run's results. `dm.py review` checks this: it reads every number in those files and looks for a computed value that rounds to it.

- If you derive a number (a growth rate, a difference, a share), record it first: `dm.py calc OUT q2_growth_pct "(5210.4 - 4401.9) / 4401.9 * 100" --source "eda_results.json time_series revenue first/last"`. The review then accepts it, and the calculation is kept in `analysis/derived.json`.
- If you run extra analysis with pandas, save its results as JSON under `analysis/` (for example `analysis/revenue_by_quarter.json`). The review indexes every JSON file there.
- Round consistently: percentages to one decimal place, money to the currency's usual precision, large counts with thousands separators.

## Before and after

The figures in this example are illustrative.

> **Before:** Our comprehensive analysis delves into the sales landscape, revealing crucial insights. Notably, Electronics stands as the dominant category — driving a staggering 79.9% of revenue — underscoring its pivotal role. This isn't just concentration; it's a strategic opportunity. Additionally, regional performance was robust across the board.
>
> **After:** Electronics produced 79.9% of revenue in 2023 from 42.3% of orders. The other three categories shared the remaining 20.1%. Revenue per region ranged from 22.1% to 27.7% of the total, close to an even split, so region explains little of the variation.

The rewrite keeps every supported claim, adds the denominators, drops the causal "driving", and removes "robust", which no number supported.
