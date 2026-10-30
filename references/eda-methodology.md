# Exploratory data analysis

EDA is an investigation, not a gallery. Start from questions the data can answer, measure, and follow what is surprising. `dm eda` computes a broad, honest base layer; your job is to look into the strongest leads with the domain in mind.

## Questions to ask, in order

1. **What is a row?** An order, an order line, a customer-day, a survey response? Counts and sums mean nothing until this is clear. If one customer has many rows, customer-level statements need customer-level aggregation first.
2. **What is the outcome of interest?** Revenue, conversion, readmission, churn? If there is a target, most analysis should relate to it.
3. **Over what period and population?** The date range, the coverage, and what is missing from it (a region, a month, a product line).
4. **What varies, and with what?** Which numeric measures and grouping columns explain differences in the outcome?
5. **Has anything changed over time?** Trends, seasonality, shifts in definitions or collection.

## Univariate

| Numeric | Categorical |
|---|---|
| n, missing, mean, median, mode, min, max, range, variance, sd, quartiles, IQR, skewness, excess kurtosis, a shape label, 95% CI of the mean | n, missing, levels, frequency and %, most and least frequent, rare levels (< 1%), imbalance ratio, normalised entropy |
| histogram + box plot | bar chart of the top levels |

Read shape before choosing a summary. Skewed data is summarised with the median and IQR, not the mean and sd. Heavy tails mean a few rows dominate totals; say how much.

## Bivariate

| Pair | Measures | Chart |
|---|---|---|
| numeric–numeric | Spearman rho (monotonic) and Pearson r (linear), with p and q | scatter with a fitted line (sampled above 5,000 points) |
| numeric–categorical | group medians and means, Welch / Mann-Whitney / ANOVA / Kruskal-Wallis, effect size | box plot by group, n per group in the labels |
| categorical–categorical | crosstab, chi-square or Fisher, Cramér's V | grouped or stacked bars of row percentages |

A large Pearson r with a small Spearman rho suggests one or two extreme points drive the line; look at the scatter.

## Multivariate

Use these only when they answer a question:

- **Correlation matrix and heatmap:** an overview of numeric relationships.
- **Collinearity:** pairs with abs(rho) ≥ 0.85, and VIF above 5 (concerning) or 10 (serious). This matters for regression and for any claim that one variable "explains" the outcome.
- **PCA:** how many independent dimensions the numeric data really has. It is descriptive only, and the components need interpreting from their loadings.
- **Grouped summaries:** the outcome by two grouping variables together. This is where confounding shows up.
- **Simpson's paradox:** a relationship that reverses inside groups. dataMedic checks the strongest correlations within each grouping column; when it fires, the overall relationship must not be reported alone.
- **Clustering:** only when segments are the goal and the variables are meaningful together. Standardise, try several k, report a stability or silhouette check, and describe the clusters by their original variables.

## Time series

- **Sampling:** irregular timestamps (event data) are aggregated into calendar periods. Missing timestamps at the native step are counted, and their location is reported.
- **Period:** day when the data spans 90 days or less, week up to two years, month beyond that. Partial first and last periods are left out of trend and change figures, because comparing a partial period with full ones produces false drops.
- **Trend:** Spearman rho between period index and value, plus a linear slope per period.
- **Seasonality:** autocorrelation at the season lag (7 for daily, 52 weekly, 12 monthly), only with at least three full cycles; a day-of-week comparison for daily data.
- **Anomalies:** periods far from a rolling median (modified z above 3.5).
- **Distribution shift:** the first half against the second half (Kolmogorov-Smirnov). A shift can be a real change or a change in measurement; check definitions before comparing periods.
- **Growth:** period over period, and the last full cycle against the previous one. Additive measures (revenue, counts) are summed per period; non-additive ones (prices, rates) are averaged.

Decomposition (statsmodels STL) or forecasting needs at least two to three full cycles. Forecasts are predictive statements: label them, give intervals, and never present them as findings.

## Segmentation and concentration

- the share of a measure held by the top level, the top 3, and the top 20% of levels, against an even split;
- the share held by the top 10% of entities (customers, accounts) when an entity identifier exists;
- comparisons between segments only when each has enough rows (as a rule of thumb, at least 30 for means and at least 5 expected per cell for proportions).

## Charts

Every chart must have:

- a title that says what is shown, and the n;
- labelled axes with units;
- a zero baseline for bars;
- readable category labels (top 15, the rest grouped);
- no 3D, no dual axes unless unavoidable, and no rainbow palettes;
- at least 150 dpi.

Make the charts that support findings, not one of everything. dataMedic caps automatic charts at 24.

## Domain extras

See `domain-adaptation.md`. Only report what the available columns support. A sales file without cost data has no margin analysis; say so, rather than estimating one.
