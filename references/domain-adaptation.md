# Domain adaptation

`profile` guesses the domain from column names and types, never the file name, and reports its confidence. Treat the guess as a hypothesis: confirm it from the columns' content and from what the user said. Mixed datasets are common; apply the parts that fit.

For each domain: what to check during cleaning, what to analyse, and what not to claim. Only analyse what the columns support. When a standard analysis is impossible (no cost column, so no margin), say so in one line rather than approximating it.

## Sales and retail

- **Clean:**
  - quantity x unit price against line total (discounts, tax and shipping explain gaps; flag, do not fix);
  - negative quantities (returns);
  - one currency;
  - order-level against line-level rows;
  - test orders and internal accounts.
- **Analyse:**
  - revenue and units over time (calendar periods, partial periods excluded);
  - category and product concentration;
  - average order value;
  - customer concentration and repeat purchase rate (if customer IDs exist);
  - margin (only with cost data);
  - seasonality and promotions.
- **Avoid:** calling a top category "driving growth" without decomposing growth by category (contribution = category change / total change).

## Financial

- **Clean:**
  - sign conventions (debits and credits);
  - currencies and FX dates;
  - posting date against value date;
  - balance continuity (opening + movements = closing);
  - duplicate transactions (same amount, date and counterparty can be legitimate; compare references);
  - reversals.
- **Analyse:**
  - flows over time;
  - category breakdowns;
  - concentration by counterparty;
  - anomalies against rolling baselines;
  - aging.
- **Avoid:** imputing amounts. A missing transaction amount is an investigation, not a fill.

## Marketing

- **Clean:**
  - channel and campaign label variants (utm values);
  - clicks ≤ impressions and conversions ≤ clicks;
  - attribution window and currency for spend.
- **Analyse:**
  - CTR, conversion rate and CPA by channel and campaign, with denominators and confidence intervals for rates;
  - spend against outcome (correlation is not ROI);
  - time trends.
- **Avoid:**
  - comparing rates from tiny denominators;
  - claiming a channel "causes" conversions from observational data (attribution is a modelling choice; say which one the data reflects).

## Customer behaviour

- **Clean:**
  - one row per event against per user;
  - bots and test users;
  - time zones of event timestamps;
  - sessions spanning midnight.
- **Analyse:**
  - cohorts by signup period;
  - retention and churn curves;
  - engagement distributions (usually heavy-tailed: use medians);
  - funnels.
- **Avoid:** survivorship bias. Current-customer data excludes those who left.

## Healthcare

- **Clean:**
  - physiologically impossible values against reference ranges the user provides (do not invent clinical thresholds);
  - units (mg/dL against mmol/L);
  - repeated measures per patient;
  - admission and discharge order;
  - the date of birth against event dates.
- **Analyse:**
  - distributions;
  - missingness by group (often informative: tests not ordered);
  - outcomes by cohort, with patient-level aggregation.
- **Avoid:**
  - clinical interpretation or advice;
  - treating patient rows as independent when patients repeat;
  - exposing identifiers (mark them sensitive).

## Education

- **Clean:** grade scales (letters against points against percentages), term coding, duplicate enrolments, impossible scores.
- **Analyse:** score distributions by course and term, pass rates with denominators, attendance against outcomes (association only), and cohort progression.
- **Avoid:** ranking small groups; reporting on individuals.

## Human resources

- **Clean:** hire and termination order, active employees with termination dates, salary currency and period (annual against monthly), department label variants.
- **Analyse:**
  - headcount over time;
  - attrition rate with the right denominator (the average headcount);
  - tenure distributions;
  - pay by role and grade, controlling for level before comparing groups.
- **Avoid:**
  - raw pay-gap figures presented as discrimination findings without controls;
  - exposing salaries of identifiable people (small groups).

## Operational

- **Clean:** sensor ranges, stuck values (long runs of identical readings), shift and time zone, downtime records that overlap.
- **Analyse:** throughput, downtime and defects over time; by line, shift and machine; control-chart style anomaly detection; and duration distributions.
- **Avoid:** comparing machines with different exposure without normalising (per hour, per unit).

## Logistics

- **Clean:** ship before order, delivery before ship, weight and dimension units, carrier label variants, duplicate tracking numbers.
- **Analyse:** lead times (median and percentiles), on-time rate against a defined SLA, performance by carrier, route and warehouse, and seasonality.
- **Avoid:** on-time rates without stating the definition and the denominator.

## Geospatial

- **Clean:** latitude and longitude ranges, swapped coordinates (latitude out of range but valid as a longitude), (0,0) placeholders, country and region label variants, postcode formats (kept as text).
- **Analyse:** distributions by region, density, and distance-based measures if needed.
- **Avoid:** maps without normalising by population or exposure.

## Survey and questionnaire

- **Clean:**
  - Likert coding consistency (1–5 against text labels; reversed items);
  - "Don't know" and "Prefer not to say", which are answers, not missing values;
  - straight-lining;
  - speeders, if timing exists.
- **Analyse:**
  - response distributions (ordinal: medians and proportions, not means, unless justified);
  - cross-tabs with chi-square;
  - subgroup sizes;
  - weighting, if weights are provided.
- **Avoid:** generalising beyond the respondents (selection bias); this is the domain where it matters most.

## Time series (any domain)

- **Clean:** duplicate timestamps, missing periods, irregular sampling, time zones and daylight-saving jumps, mixed date and datetime values.
- **Analyse:** trend, seasonality, anomalies, structural breaks, and growth with partial periods excluded.
- **Avoid:**
  - interpolating across long gaps or irregular intervals without understanding them;
  - using future data in anything predictive.

## Scientific and experimental

- **Clean:** units, instrument limits (values at the detection limit are censored, not exact), replicate structure, batch effects.
- **Analyse:**
  - treatment against control with tests chosen for the design (paired, blocked, repeated measures);
  - effect sizes with intervals;
  - pre-registered comparisons first.
- **Avoid:** treating technical replicates as independent; post-hoc subgroup claims without correction.

## Adding domain rules

Add patterns to `config/domain_rules.json` (`impossible` for values that cannot exist, `expected` for usual ranges) when the user provides definitive ranges, and mention the source in the plan's rationale.
