# Statistical testing

A test answers one narrow question: how surprising the observed pattern would be if there were no real effect. It does not say how large or important the effect is, or why it exists. Report all three things separately: the test result, the effect size, and your interpretation.

## Choosing a test

| Question | Variables | Assumptions hold | Otherwise |
|---|---|---|---|
| Do two groups differ? | numeric by 2 groups | Welch's t-test (normal-ish in each group, or n ≥ 30 with mild skew) | Mann-Whitney U |
| Do several groups differ? | numeric by k groups | one-way ANOVA (normal-ish, equal variances by Levene) | Kruskal-Wallis H; Welch's ANOVA when only the variances differ |
| Are two categorical variables related? | category by category | chi-square test of independence (expected counts ≥ 5 in at least 80% of cells) | Fisher's exact test (2x2); merge sparse levels for larger tables |
| Do two numeric variables move together? | numeric by numeric | Pearson r (linear, no extreme outliers) | Spearman rho (monotonic, rank-based); dataMedic reports both |
| Is there a trend over time? | numeric by time | Spearman rho between period index and value | Mann-Kendall (equivalent idea; adjust for autocorrelation) |
| Did a distribution change? | numeric, two periods | two-sample Kolmogorov-Smirnov | |
| Paired measurements (before/after on the same units)? | numeric, paired | paired t-test | Wilcoxon signed-rank |

dataMedic chooses the group tests automatically after checking assumptions:

- **Normality:** Shapiro-Wilk on up to 5,000 values per group. With n ≥ 30 and abs(skewness) < 1, the t-test is treated as robust (central limit theorem).
- **Equal variances:** Levene's test with the median center (Brown-Forsythe), which is robust to non-normality.

Groups with fewer than 5 values are left out of a comparison, and the group sizes are always reported.

## Effect sizes

| Measure | Used with | Negligible / small / medium / large |
|---|---|---|
| Cohen's d | t-test | < 0.2 / 0.2 / 0.5 / 0.8 |
| rank-biserial r | Mann-Whitney U | < 0.1 / 0.1 / 0.3 / 0.5 |
| eta squared | ANOVA | < 0.01 / 0.01 / 0.06 / 0.14 |
| epsilon squared | Kruskal-Wallis | as eta squared |
| Cramér's V | chi-square | < 0.1 / 0.1 / 0.3 / 0.5 |
| rho, r | correlation | < 0.1 / 0.1 / 0.3 / 0.5 (very strong above 0.8) |

These conventions (Cohen, 1988) are rough. Practical importance depends on the domain: a 0.5% conversion lift can be worth millions, and a "large" effect on a trivial metric is not.

## Multiple testing

Running many tests guarantees some false positives: at alpha 0.05, 1 in 20 null comparisons will look significant. dataMedic applies the Benjamini-Hochberg false-discovery-rate correction across every test in a run and reports a q-value next to each p-value. Use q, not p, to call a result significant. If you run additional tests yourself, correct them together with these, or at least within their own family, and say which correction you used. Use Bonferroni when any single false positive would be costly.

## What every reported test includes

- null and alternative hypotheses, in words;
- the test, and why it was chosen (the assumption checks);
- the test statistic, the p-value, and the q-value after correction;
- the effect size, with a confidence interval where available (mean difference for Welch, Fisher-z interval for Pearson r);
- group sizes or n;
- the interpretation and its limits.

`analysis/eda_results.json` stores this for every built-in test.

## Things a test never shows

- **Causation.** Groups can differ for reasons other than the grouping (confounding). Only randomised assignment supports a causal reading.
- **Importance.** With 100,000 rows, trivial differences have p < 0.001. Lead with the effect size.
- **No effect.** A non-significant result means the data could not detect an effect of the tested size. It does not show the effect is zero, especially in small samples.
- **Independence it assumed.** Repeated measures on the same customer, patient or day violate independence. Use group-level summaries or methods built for clustered data, and say so.

## Sources

- NIST/SEMATECH e-Handbook of Statistical Methods, chapters 1 and 7.
- SciPy documentation, `scipy.stats`.
- Benjamini, Y., and Hochberg, Y. (1995). Controlling the false discovery rate. *JRSS B*, 57(1), 289–300.
- Cohen, J. *Statistical Power Analysis for the Behavioral Sciences*, 2nd ed. 1988.
