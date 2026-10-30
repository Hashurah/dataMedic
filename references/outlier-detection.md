# Outliers and invalid values

The first question about an unusual value is never "how do I remove it" but "is it possible". dataMedic separates three classes:

| Class | Example | Treatment |
|---|---|---|
| **Confirmed invalid** (impossible) | age -4, latitude 95, a percentage of 150 on a 0–100 scale, a birth date in the future | set to missing, flag the row, keep the original in the change log |
| **Potential anomaly** (possible but unusual for the field) | negative quantity (a return?), a date before 1900, a future order date | flag, keep |
| **Unresolved statistical outlier** | a revenue far above the rest | flag, keep |

Only the first class changes data, and only on the basis of a rule that is true by definition. The rules are in `config/domain_rules.json`: name patterns with `impossible` and `expected` ranges. Add rules for the domain at hand when you know them, for example a valid range for a lab measurement, from a reference the user trusts.

## Detection methods

dataMedic computes several methods for each numeric column and reports all of them, because they disagree in informative ways:

| Method | Rule | Good for | Weakness |
|---|---|---|---|
| IQR fences (Tukey) | below Q1 − 1.5·IQR or above Q3 + 1.5·IQR (3·IQR = "extreme") | any roughly symmetric distribution; robust to the outliers themselves | flags much of the tail of skewed data |
| IQR on the log scale | the same fences on log(x), for positive data | right-skewed data (income, revenue, durations) | positive values only |
| z-score | abs(x − mean) / sd above 3 | near-normal data | the mean and sd are inflated by the very outliers being sought (masking) |
| Modified z-score (MAD) | 0.6745·abs(x − median) / MAD above 3.5 (Iglewicz and Hoaglin) | robust general screen | MAD = 0 when most values are equal |
| Percentiles | below p0.5 or above p99.5, say | describing tails | always flags a fixed share, outliers or not |
| Domain thresholds | from `domain_rules.json` | impossible and unusual values | needs the rule to exist |
| Multivariate (Mahalanobis distance, Isolation Forest) | unusual combinations, for example a tall child with a low weight | when single-variable checks miss combination errors | needs enough data and a reason; not run by default |

The flag rule chosen automatically is IQR, or IQR on the log scale when skewness is above 1 and all values are positive. When IQR is zero (most values identical), no statistical outlier flag is created.

## Investigating a flagged value

For the flags that matter to the analysis:

1. Look at the whole row. Is anything else odd, such as other extreme values, a test account or a default date?
2. Check related columns. Does `quantity × unit_price` match `total`? Is a large order from a customer type that places large orders?
3. Check whether it repeats. An exact value repeated many times (9999, 1900-01-01) suggests a code or a default.
4. Check whether it is the tail of the distribution. If it disappears on a log scale, it is probably real.
5. Decide, and record the decision in the plan:
   - keep (the default);
   - correct with independent evidence (`replace_values`, citing the source);
   - exclude from a specific analysis (say so in that analysis; do not delete the row);
   - winsorise for a stated analytical reason (for example, to keep one extreme value from dominating a mean). Never winsorise to make data look tidy.

## Reporting

Give counts per method and the bounds used, state which values were kept and why, and run sensitive analyses (means, correlations) with and without the extremes when they change the conclusion.

## Sources

- NIST/SEMATECH e-Handbook of Statistical Methods, section 1.3.5.17, "Detection of Outliers".
- Iglewicz, B., and Hoaglin, D. *How to Detect and Handle Outliers*. ASQC Quality Press, 1993.
- Tukey, J. W. *Exploratory Data Analysis*. Addison-Wesley, 1977.
