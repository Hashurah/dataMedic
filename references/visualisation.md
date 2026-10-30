# Visualisation and the HTML report

## Libraries and their jobs

| Library | Required? | Used for | Without it |
|---|---|---|---|
| matplotlib | yes, for charts | every chart's figure, axes, layout and export; the Agg backend renders without a screen | no charts; analysis and reports still run |
| seaborn | optional | the statistical charts: histogram with density curve, violin plot (groups of 30 or more) or box plot (smaller groups), lower-triangle correlation heatmap | the same charts are drawn with matplotlib alone |
| plotly | optional, off by default | interactive time-series and scatter charts in the HTML report (`dm report OUT --interactive`, or `report.interactive: true`) | the report says so and shows the static charts |

`dm check` shows which are installed. `visualisations/chart_index.json` lists each chart with its kind, file and the library that drew it. Each chart's numbers are in `analysis/eda_results.json` under the matching section.

## One visual identity

`charts.configure()` sets one theme for every chart:

- deep teal for the data, navy and amber as second and third colours, slate for secondary text;
- light grid, no top or right spines;
- titles on the left, with the sample size in every title.

The HTML report uses the same palette, so charts and page match.

Settings are in config `charts`:

- `formats`: `["png"]` by default; add `"svg"` and/or `"pdf"` for vector copies next to each PNG;
- `dpi`: 150;
- `use_seaborn`: `"auto"` uses it when installed; `false` forces matplotlib.

## Which charts, and why

Charts follow the data, not a fixed list:

- **Distributions**: one per measure for the most informative measures, ranked by completeness, spread, link to the outcome and domain priority. Up to `eda.max_distribution_charts` (6). A log scale is used when the largest value is more than 20 times the 75th percentile.
- **Category counts**: the most balanced and complete grouping columns, up to `eda.max_bar_charts` (4); the top 15 levels.
- **Correlation heatmap**: all analysed measures, with the upper triangle hidden when seaborn draws it.
- **Relationships**: scatter (at most 5,000 points, sampled and labelled as such) and violin or box charts, only for finding candidates that survived multiple-testing correction.
- **Time series**: per period, with a moving average; partial first and last periods shown but labelled.
- **Missingness**: missing share per column before and after cleaning, and the row-by-column pattern of missing values (sampled to 400 rows when larger).
- **Multivariate anomalies**: the two main components of the scaled measures, with flagged rows highlighted.
- **Before and after**: the distribution of each numeric column that imputation, clipping or invalidation changed, in the report.

Pair plots are not drawn. With more than four measures they become unreadable, and the heatmap plus the targeted scatters carry the same information.

Every chart is capped by `eda.max_charts` (24).

## The HTML audit report

`reports/data_audit_report.html` is one self-contained file. Styles, scripts, charts (base64 PNG) and data are inline, so it opens offline and can be emailed. With `--interactive`, the plotly library is embedded inline (about 4 MB); nothing is fetched from the network.

Layout:

- side navigation (it collapses on narrow screens);
- light and dark themes that follow the system setting, with a toggle;
- print styles that hide navigation and open all collapsible parts.

| Section | Contents |
|---|---|
| Overview | before and after counts; values imputed; duplicates; rows removed and quarantined; steps applied; checks passed; outstanding items; quality by dimension (no single score) |
| Dataset profile | file properties and SHA-256; per-column type, roles and statistics (filterable) |
| Data quality | every issue, filterable by severity |
| Cleaning operations | every step with its status and risk; expand for parameters, statistical basis and planning evidence |
| Missing values | before/after and pattern charts; Little's MCAR test; columns missing together; structural candidates; missingness over time; the masked-value comparison; evidence levels |
| Duplicate records | removed duplicates, conflicting duplicates, flagged identifier duplicates |
| Outlier analysis | flagged outliers; multivariate anomalies with their main drivers |
| Type corrections | conversions and every value that could not be converted |
| Removed and quarantined records | both lists, sensitive values masked |
| Cleaned data preview | search, sort, page size, column chooser; imputed cells (amber) and changed cells (blue) with the original value on hover; a record inspector by row id |
| Exploratory analysis | variables chosen and why, analytical exclusions, charts, optional interactive charts |
| Statistical insights | finding candidates and the significant tests with effect sizes |
| Data lineage | a flow diagram from the original to the final dataset with counts per stage; a column lineage table (original name and type, every step, final name and type, missing before and after, validation checks) |
| Validation | every check; contract rules before and after; unresolved conflicts; outstanding items |
| Audit and recovery | for each step, where the originals are, what needs care when undoing it, and the `dm restore` command; reproducibility details |

Preview limits (`report.preview_rows`, default 500) keep the file small. The page says when the preview is a sample. The record inspector embeds up to 20,000 rows' status and 50,000 changes; beyond that it points to `audit/row_status.csv`.

**Integrity**: every figure comes from the run's JSON and CSV files. **Safety**:

- values from the data are escaped when the page is built;
- the scripts insert data with `textContent`, so a value like `<script>` is shown as text and never run;
- sensitive columns are masked unless `report.redact_sensitive` is false.
