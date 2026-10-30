"""Generated reports: data quality (Markdown), EDA (Markdown), audit (static HTML) and an Excel audit workbook.

These reports state measured facts. The executive summary and the insights are written by
the agent from templates/, using the numbers here, and checked by `review`.
"""

import base64
import html
import os

import pandas as pd

from . import charts, html_report
from .clean import read_str_csv
from .common import ROW_ID, fmt_num, mask_value, merged_config, optional, read_json, write_excel_safe, write_json


def _load(out):
    j = lambda *p: os.path.join(out, *p)  # noqa: E731
    d = {"man": read_json(j("metadata", "analysis_manifest.json")), "plan": read_json(j("plan.json")),
         "p0": read_json(j("profiling", "initial_profile.json")), "issues": read_json(j("profiling", "issues.json"))["issues"],
         "log": read_json(j("audit", "transformation_log.json"))["operations"],
         "schema": read_json(j("cleaned_data", "schema.json"))}
    d["p1"] = read_json(j("profiling", "final_profile.json")) if os.path.exists(j("profiling", "final_profile.json")) else None
    d["val"] = read_json(j("audit", "validation_results.json")) if os.path.exists(j("audit", "validation_results.json")) else None
    d["eda"] = read_json(j("analysis", "eda_results.json")) if os.path.exists(j("analysis", "eda_results.json")) else None
    d["changes"] = read_str_csv(j("audit", "cell_changes.csv"))
    d["removed"] = read_str_csv(j("audit", "removed_records.csv"))
    d["flags"] = read_str_csv(j("audit", "flagged_records.csv"))
    d["cleaned"] = read_str_csv(j("cleaned_data", "cleaned_dataset.csv"))
    d["sensitive"] = set(d["plan"].get("roles", {}).get("sensitive", [])) | \
        {c for c, i in d["schema"]["columns"].items() if i.get("sensitive")}
    opt = {"missingness": j("profiling", "missingness.json"), "comparison": j("profiling", "imputation_comparison.json")}
    for k, path in opt.items():
        d[k] = read_json(path) if os.path.exists(path) else None
    d["evidence"] = read_str_csv(j("audit", "imputation_evidence.csv")) if os.path.exists(j("audit", "imputation_evidence.csv")) else None
    d["row_status"] = read_str_csv(j("audit", "row_status.csv")) if os.path.exists(j("audit", "row_status.csv")) else None
    d["cfg"] = merged_config(d["plan"].get("settings", {}).get("config"))
    return d


def quality_scores(prof, rows_flagged_invalid=0):
    cells = prof["rows"] * max(1, prof["columns"])
    missing = sum(c["missing_count"] for c in prof["columns_profile"])
    return {"completeness_pct": round(100 * (1 - missing / cells), 2) if cells else None,
            "uniqueness_pct": round(100 * (1 - prof["exact_duplicate_rows"] / max(1, prof["rows"])), 2),
            "missing_cells": missing, "cells": cells}


def _md_table(headers, rows):
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join("---" for _ in headers) + "|"]
    for r in rows:
        out.append("| " + " | ".join(str(x).replace("|", "\\|").replace("\n", " ") for x in r) + " |")
    return "\n".join(out)


def _status_counts(log):
    applied = [e for e in log if str(e.get("status", "")).startswith("applied") and e.get("op") != "flag_rows"]
    flagged = [e for e in log if e.get("op") == "flag_rows" and e.get("flagged")]
    disabled = [e for e in log if "disabled" in str(e.get("status", "")) and e.get("needs_review")]
    failed = [e for e in log if e.get("status") == "failed"]
    return applied, flagged, disabled, failed


def _declined(log):
    """Proposed operations that were decided against (disabled, no longer awaiting review)."""
    return [e for e in log if "disabled" in str(e.get("status", "")) and not e.get("needs_review")]


def quality_md(d):
    man, p0, p1, issues, log, val = d["man"], d["p0"], d["p1"], d["issues"], d["log"], d["val"]
    r = man.get("result", {})
    q0 = quality_scores(p0)
    q1 = quality_scores(p1) if p1 else None
    inp = man["input"]
    L = [f"# Data quality report: {inp['file']}", ""]
    scope = man.get("analysis_scope", {})
    L.append(f"Source: `{inp['file']}` ({inp['format']}, {fmt_num(inp['size_bytes'])} bytes, sha256 `{inp['sha256'][:16]}...`)."
             + (f" Sheet `{inp['sheet']}`." if inp.get("sheet") else "") + (f" Table `{inp['table']}`." if inp.get("table") else ""))
    if scope.get("sampled"):
        L.append(f"\nThis run analysed a random sample of {fmt_num(scope['rows_analysed'])} of "
                 f"{fmt_num(scope['rows_in_file'])} rows. Counts below describe the sample.")
    L += ["", "## Initial assessment", "",
          _md_table(["Measure", "Before cleaning", "After cleaning"], [
              ["Rows", fmt_num(p0["rows"]), fmt_num(r.get("rows_after", "n/a"))],
              ["Columns", fmt_num(p0["columns"]), fmt_num(r.get("columns_after", "n/a"))],
              ["Missing cells", f"{fmt_num(q0['missing_cells'])} of {fmt_num(q0['cells'])}",
               f"{fmt_num(q1['missing_cells'])} of {fmt_num(q1['cells'])}" if q1 else "n/a"],
              ["Completeness", f"{q0['completeness_pct']}%", f"{q1['completeness_pct']}%" if q1 else "n/a"],
              ["Exact duplicate rows", fmt_num(p0["exact_duplicate_rows"]), fmt_num(p1["exact_duplicate_rows"]) if p1 else "n/a"],
              ["Empty rows", fmt_num(p0["empty_rows"]), fmt_num(p1["empty_rows"]) if p1 else "n/a"],
              ["Malformed lines (kept out of the table)", fmt_num(p0.get("malformed_rows", 0)), "n/a"],
          ]), ""]
    if p1:
        L.append("After-cleaning completeness counts the cleaned columns, including any `_was_missing` indicator "
                 "columns added by imputation.\n")
    sev = {}
    for i in issues:
        sev[i["severity"]] = sev.get(i["severity"], 0) + 1
    L.append("Issues found by severity: " + ", ".join(f"{k} {v}" for k, v in sev.items()) + "." if sev else "No issues found.")
    L += ["", "## Issues", "", _md_table(["ID", "Severity", "Kind", "Column", "Count", "%", "Detail", "Action"], [
        [i["issue_id"], i["severity"], i["kind"], i["column"] or "(table)", fmt_num(i["count"]) if i["count"] is not None else "",
         i["pct"] if i["pct"] is not None else "", i["detail"], i["planned_action"]] for i in issues])]
    miss = [c for c in p0["columns_profile"] if c["missing_count"]]
    L += ["", "## Missing values", ""]
    if miss:
        mech = {i["column"]: i for i in issues if i["kind"] == "missing values"}
        L.append(_md_table(["Column", "Missing", "%", "Placeholders found", "Assessment", "Action"], [
            [c["column"], fmt_num(c["missing_count"]), c["missing_pct"],
             ", ".join(f"{k} ({v})" for k, v in c["missing_tokens_found"].items()) or "",
             mech.get(c["column"], {}).get("evidence", {}).get("mechanism_assessment", "after placeholder fixes only"),
             mech.get(c["column"], {}).get("planned_action", "")] for c in miss]))
        L.append("\nMissingness tests compare the rows with and without a value on every other column "
                 "(chi-square for categories, Mann-Whitney U for numbers, Benjamini-Hochberg corrected). "
                 "A relationship makes MAR plausible. No relationship is consistent with MCAR, but MNAR "
                 "(missing because of the value itself) cannot be tested from the observed data.")
    else:
        L.append("No missing values.")
    applied, flagged, disabled, failed = _status_counts(log)
    conv = [e for e in applied if e["op"] == "convert_type"]
    L += ["", "## Type corrections", ""]
    L.append(_md_table(["Op", "Column", "Converted to", "Values rewritten", "Of which format only (52.0 to 52)",
                        "Failed conversions (set missing, flagged)"], [
        [e["op_id"], e["column"], e["params"]["to"], fmt_num(e.get("affected", 0)),
         fmt_num(e.get("details", {}).get("format_only_changes", 0)),
         fmt_num(e.get("details", {}).get("failed_conversions", 0))] for e in conv]) if conv else "No type conversions.")
    L += ["", "## Duplicates", ""]
    dd = [e for e in applied if e["op"] == "drop_duplicates"]
    if dd:
        for e in dd:
            L.append(f"{e['op_id']}: removed {fmt_num(e['affected'])} rows ({e['method']}). Compared columns: "
                     f"{e['details']['columns_compared']}. Each removed row lists the row that was kept "
                     "(`retained_row_id` in `audit/removed_records.csv`).")
    else:
        L.append("No duplicate rows were removed.")
    dup_flags = [e for e in flagged if "duplicate" in e.get("issue", "")]
    for e in dup_flags:
        L.append(f"{e['op_id']}: flagged {fmt_num(e['flagged'])} rows: {e['issue']}.")
    rem = d["removed"]
    if len(rem) and "dm_disposition" in rem.columns and (rem["dm_disposition"] == "quarantined").any():
        q = rem[rem["dm_disposition"] == "quarantined"]
        L += ["", "## Quarantined rows", "", f"{fmt_num(len(q))} rows were set aside: they are not in the cleaned data and "
              "are kept with their original values in `audit/quarantined_records.csv` until the issue is resolved."]
        L += [f"- {k}: {fmt_num(v)} rows" for k, v in q.groupby("reason").size().items()]
    ev = d.get("evidence")
    if ev is not None and len(ev):
        L += ["", "## Imputation evidence", "", "Each imputed value has a record in `audit/imputation_evidence.csv` "
              "(method, supporting observations, spread, masked-value check, limitations). Evidence levels:", ""]
        L.append(_md_table(["Column", "Method", "Evidence level", "Cells"],
                           ev.groupby(["column", "method", "evidence_level"]).size().reset_index().values.tolist()))
    L += ["", "## Invalid values, outliers and logical checks", ""]
    inv = [e for e in applied if e["op"] == "set_missing_where"]
    for e in inv:
        L.append(f"- {e['op_id']} `{e['column']}`: {fmt_num(e['affected'])} values set to missing ({e['issue']}).")
    for e in flagged:
        L.append(f"- {e['op_id']} `{e['column'] or '(row)'}`: {fmt_num(e['flagged'])} rows flagged, values kept ({e['issue']}).")
    if not inv and not flagged:
        L.append("Nothing was flagged.")
    L += ["", "## Every cleaning decision", "", _md_table(["Op", "Operation", "Column", "Status", "Changed", "Why"], [
        [e["op_id"], e["op"], e["column"], e.get("status", ""), fmt_num(e.get("affected", 0)), e.get("rationale", "")]
        for e in log if e.get("op")])]
    if disabled:
        L += ["", "## Decisions left open", "", "These operations were proposed but not applied because they depend "
              "on what the data means. Enable them in `plan.json` and re-run `clean` once decided.", ""]
        L += [f"- {e['op_id']} `{e['column']}`: {e['issue']}. {e['rationale']}" for e in disabled]
    declined = _declined(log)
    if declined:
        L += ["", "## Proposed and declined", "", "These were considered and deliberately not applied.", ""]
        L += [f"- {e['op_id']} `{e['column']}` ({e['issue']}), decided by {e.get('decided_by', 'auto')}: "
              f"{e['rationale']}" for e in declined]
    if failed:
        L += ["", "## Failed operations", ""] + [f"- {e['op_id']}: {e.get('error')}" for e in failed]
    L += ["", "## Final assessment", ""]
    if val:
        L.append(f"Validation status: **{val['status']}** ({val['critical_failures']} critical failures, "
                 f"{val['warnings']} warnings, {len(val['checks'])} checks). Details in `audit/validation_results.json`.")
    else:
        L.append("Validation has not been run yet.")
    return "\n".join(L) + "\n"


def eda_md(d):
    e, man = d["eda"], d["man"]
    L = [f"# Exploratory data analysis: {man['input']['file']}", ""]
    dom = e.get("domain") or {}
    cands = dom.get("candidates", [])
    L += ["## Scope", ""]
    L.append(f"{fmt_num(e['rows'])} rows and {e['columns']} columns after cleaning. Numeric measures analysed: "
             f"{', '.join(e['measures']) or 'none'}. Grouping columns: {', '.join(e['categories']) or 'none'}. "
             f"Time column: {e['time_column'] or 'none'}. Outcome column: {e['target'] or 'none identified'}.")
    if cands:
        L.append(f"\nLikely domain: {cands[0]['domain']} (confidence {dom.get('confidence')}; matched "
                 f"{', '.join(cands[0]['matched_terms'])}). This is inferred from column names and types only.")
    if e.get("sensitive_columns_excluded_from_value_reports"):
        L.append("\nColumns treated as sensitive (values not shown): " + ", ".join(e["sensitive_columns_excluded_from_value_reports"]) + ".")
    uni = e.get("univariate", {})
    if uni.get("numeric"):
        L += ["", "## Numeric variables", "", _md_table(
            ["Column", "n", "Missing", "Mean", "Median", "Std", "Min", "Q1", "Q3", "Max", "Skew", "Shape"],
            [[c, fmt_num(s["n"]), fmt_num(s.get("missing", 0)), fmt_num(s.get("mean")), fmt_num(s.get("median")),
              fmt_num(s.get("std")), fmt_num(s.get("min")), fmt_num(s.get("q1")), fmt_num(s.get("q3")),
              fmt_num(s.get("max")), fmt_num(s.get("skewness")), s.get("shape", "")] for c, s in uni["numeric"].items()])]
        for c, s in uni["numeric"].items():
            if s.get("chart"):
                L.append(f"\n![Distribution of {c}](../{s['chart']})")
    if uni.get("categorical"):
        L += ["", "## Categorical variables", "", _md_table(
            ["Column", "n", "Missing", "Levels", "Most frequent", "Least frequent", "Rare levels", "Imbalance ratio"],
            [[c, fmt_num(s["n"]), fmt_num(s["missing"]), s["levels"],
              f"{s['top'][0]['value']} ({s['top'][0]['pct']}%)" if s["top"] else "",
              ", ".join(x["value"] for x in s["least_frequent"]), s["rare_levels"], fmt_num(s["imbalance_ratio"])]
             for c, s in uni["categorical"].items()])]
        for c, s in uni["categorical"].items():
            if s.get("chart"):
                L.append(f"\n![{c}](../{s['chart']})")
    tests = {tuple(t.get("variables", [])): t for t in e.get("tests", {}).get("results", [])}
    cor = sorted(e.get("correlations", []), key=lambda r: -abs(r["spearman_rho"]))
    if cor:
        L += ["", "## Relationships between numeric variables", "", _md_table(
            ["Pair", "n", "Spearman rho", "Pearson r", "q (BH)", "Strength"],
            [[f"{r['a']} / {r['b']}", fmt_num(r["n"]), f"{r['spearman_rho']:.3f}", f"{r['pearson_r']:.3f}",
              f"{tests.get((r['a'], r['b']), {}).get('q_value_bh', float('nan')):.3g}", r["strength"]] for r in cor[:15]])]
        L.append("\nCorrelation describes association only. It does not show that one variable causes the other.")
    grp = [g for g in e.get("group_comparisons", []) if tests.get((g["measure"], g["group"]), {}).get("significant_after_correction")]
    if e.get("group_comparisons"):
        L += ["", "## Differences between groups", ""]
        if grp:
            L.append(_md_table(["Measure", "Group", "Test", "Statistic", "p", "q (BH)", "Effect size", "Groups (n)"], [
                [g["measure"], g["group"], g["test"], f"{g['statistic']:.4g}", f"{g['p_value']:.3g}",
                 f"{tests[(g['measure'], g['group'])]['q_value_bh']:.3g}",
                 ", ".join(f"{k} {v:.3g}" if isinstance(v, float) else f"{k} {v}" for k, v in g["effect_size"].items()),
                 ", ".join(f"{k} ({v})" for k, v in list(g["group_sizes"].items())[:6])] for g in grp]))
        else:
            L.append("No group difference was significant after correction for multiple testing.")
        L.append(f"\n{len(e['group_comparisons'])} comparisons ran. Each test was chosen after checking normality "
                 "(Shapiro-Wilk) and equal variances (Levene): Welch's t-test or ANOVA when they held, Mann-Whitney U "
                 "or Kruskal-Wallis when they did not.")
    ca = [a for a in e.get("categorical_associations", []) if a["p_value"] < 0.05]
    if ca:
        L += ["", "## Associations between categorical variables", "", _md_table(
            ["Pair", "Test", "p", "Cramer's V", "Warning"],
            [[f"{a['a']} / {a['b']}", a["test"], f"{a['p_value']:.3g}", f"{a['effect_size']['cramers_v']:.3f}",
              a.get("warning", "")] for a in ca])]
    mv = e.get("multivariate", {})
    if mv:
        L += ["", "## Multivariate view", ""]
        if mv.get("heatmap"):
            L.append(f"![Correlation matrix](../{mv['heatmap']})\n")
        if mv.get("highly_correlated_pairs"):
            L.append("Highly correlated pairs (|rho| >= 0.85), which matter for regression models: " +
                     "; ".join(f"{p['a']} / {p['b']} ({p['spearman_rho']:.2f})" for p in mv["highly_correlated_pairs"]) + ".")
        if mv.get("vif"):
            hv = {k: v for k, v in mv["vif"].items() if v and v > 5}
            if hv:
                L.append("Variance inflation factors above 5: " + ", ".join(f"{k} {v:.1f}" for k, v in hv.items()) + ".")
        if mv.get("pca"):
            ev = mv["pca"]["explained_variance_ratio"]
            L.append(f"Principal components (standardised, {fmt_num(mv['pca']['rows_used'])} complete rows): the first "
                     f"explains {100 * ev[0]:.1f}% of the variance, the first two {100 * sum(ev[:2]):.1f}%.")
        for sp in mv.get("simpsons_paradox_checks", []):
            L.append(f"Possible Simpson's paradox: {sp['a']} and {sp['b']} have rho {sp['overall_rho']:.2f} overall, but "
                     f"the sign reverses within most {sp['group']} groups.")
    ts = e.get("time_series", {})
    L += ["", "## Over time", ""]
    if ts.get("applicable"):
        L.append(f"`{ts['column']}` runs from {ts['start'][:10]} to {ts['end'][:10]} ({fmt_num(ts['span_days'])} days), "
                 f"summarised per {ts['frequency']} ({ts['periods']} periods). Sampling is {ts['sampling']}. "
                 f"Periods with no rows: {ts['empty_period_count']}.")
        if ts.get("missing_steps"):
            L.append(f"\nAt the data's own step ({ts['native_step']}), {ts['missing_steps']} expected timestamps are "
                     "missing: " + "; ".join(f"{g['missing_steps']} between {g['after'][:10]} and {g['before'][:10]}"
                                            for g in ts.get("gaps", [])[:5]) + ".")
        pp = ts.get("partial_periods", {})
        if pp.get("first") or pp.get("last"):
            which = " and ".join(w for w, f in (("first", pp.get("first")), ("last", pp.get("last"))) if f)
            L.append(f"\nThe {which} {ts['frequency']} is only partly covered by the data, so it is left out of the "
                     "first/last values, trends and changes below (the charts still show it).")
        rows = []
        for name, s in ts["series"].items():
            tr = s.get("trend", {})
            rows.append([name, s["aggregation"], fmt_num(s["first"]), fmt_num(s["last"]),
                         f"{s['max']['period']} ({fmt_num(s['max']['value'])})",
                         f"{tr.get('spearman_rho', float('nan')):.2f} (p {tr.get('p_value', float('nan')):.3g})" if tr else "",
                         fmt_num(s.get("last_period_change_pct")),
                         len(s.get("anomalous_periods", []))])
        L.append("\n" + _md_table(["Series", "Per period", "First", "Last", "Peak", "Trend rho", "Last change %",
                                   "Anomalous periods"], rows))
        for name, s in ts["series"].items():
            if s.get("chart"):
                L.append(f"\n![{name} over time](../{s['chart']})")
        sh = [s for s in ts.get("distribution_shift", []) if s["p_value"] < 0.05 and s["statistic"] >= 0.1]
        if sh:
            L.append("\nDistribution changes between the first and second half of the period: " +
                     "; ".join(f"{s['column']} (KS D {s['statistic']:.2f}, median {fmt_num(s['median_first_half'])} "
                               f"then {fmt_num(s['median_second_half'])})" for s in sh) + ".")
    else:
        L.append(f"Not applicable: {ts.get('reason', 'no time column')}.")
    seg = e.get("segments", [])
    if seg:
        L += ["", "## Concentration", ""]
        for s in seg:
            if "top_level" in s:
                parts = [f"'{s['top_level']}' holds {s['top_share_pct']:.1f}% (an even split would give each of the "
                         f"{s['levels']} levels {100 / s['levels']:.1f}%)"]
                if s.get("top3_share_pct") is not None:
                    parts.append(f"the top 3 hold {s['top3_share_pct']:.1f}%")
                if s.get("top20pct_levels_share_pct") is not None:
                    parts.append(f"the top 20% of levels hold {s['top20pct_levels_share_pct']:.1f}%")
                L.append(f"- {s['measure']} by {s['group']}: " + "; ".join(parts) + ".")
            else:
                L.append(f"- {s['measure']} by {s['group']}: the top 10% of {s['levels']} hold "
                         f"{s['top10pct_entities_share_pct']:.1f}%.")
    ta = e.get("target_analysis")
    if ta:
        L += ["", "## Outcome variable", "", f"Outcome: `{ta['target']}`."]
        if ta.get("class_balance"):
            L.append("Class balance: " + ", ".join(f"{k} {v}" for k, v in ta["class_balance"].items()) + ".")
        for lk in ta.get("possible_leakage", []):
            L.append(f"Possible leakage: `{lk['column']}` ({lk.get('reason') or 'rho %.2f' % lk.get('spearman_rho', 0)}). "
                     "Check whether it is only known after the outcome.")
    t = e.get("tests", {})
    sig = [x for x in t.get("results", []) if x.get("significant_after_correction")]
    L += ["", "## Statistical tests", "", f"{t.get('count', 0)} tests ran; {len(sig)} are significant at "
          f"{t.get('alpha', 0.05)} after {t.get('correction', 'correction')}. Full records (hypotheses, test choice, "
          "assumption checks, statistic, p, q, effect size) are in `analysis/eda_results.json`."]
    fc = e.get("finding_candidates", [])
    L += ["", "## Finding candidates", "", "Ranked by effect size. These are leads for interpretation, not conclusions; "
          "the written findings are in `insights_and_recommendations.md`.", ""]
    L += [f"{i}. **{c['candidate_id']}** {c['title']} ({c['type']}). Caveat: {c['caveat']}." for i, c in enumerate(fc[:15], 1)] \
        or ["No candidate met the thresholds for a notable finding."]
    if e.get("limitations"):
        L += ["", "## Limitations", ""] + [f"- {x}" for x in e["limitations"]]
    return "\n".join(L) + "\n"


# ---------------------------------------------------------------- HTML audit report

CSS = """
:root{--bg:#fbfbfa;--fg:#1f2933;--muted:#616e7c;--line:#e4e7eb;--card:#ffffff;--accent:#2563eb;--ok:#15803d;--warn:#b45309;--bad:#b91c1c;--chip:#eef2f7}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#111418;--fg:#e6e8eb;--muted:#9aa5b1;--line:#2b3138;--card:#181c21;--accent:#7aa2ff;--ok:#4ade80;--warn:#fbbf24;--bad:#f87171;--chip:#22282f}}
:root[data-theme="dark"]{--bg:#111418;--fg:#e6e8eb;--muted:#9aa5b1;--line:#2b3138;--card:#181c21;--accent:#7aa2ff;--ok:#4ade80;--warn:#fbbf24;--bad:#f87171;--chip:#22282f}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:15px/1.55 system-ui,-apple-system,"Segoe UI",sans-serif}
main{max-width:1080px;margin:0 auto;padding:32px 16px 64px}h1{font-size:26px;margin:0 0 4px}h2{font-size:19px;margin:40px 0 10px;padding-top:8px;border-top:1px solid var(--line)}
h3{font-size:15px;margin:20px 0 6px}.muted{color:var(--muted)}.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:14px 0}
.stat{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:10px 12px}.stat b{display:block;font-size:20px}.stat span{color:var(--muted);font-size:13px}
.table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:8px;background:var(--card);margin:8px 0 14px}
table{border-collapse:collapse;width:100%;font-size:13px}th,td{text-align:left;padding:6px 9px;border-bottom:1px solid var(--line);vertical-align:top}
th{background:var(--chip);font-weight:600;white-space:nowrap}tr:last-child td{border-bottom:none}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:12px;font-weight:600;background:var(--chip)}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}img{max-width:100%;border:1px solid var(--line);border-radius:8px;background:#fff}
nav ol{columns:2;padding-left:20px;margin:8px 0}code{background:var(--chip);padding:1px 5px;border-radius:4px;font-size:12.5px}
@media print{body{background:#fff;color:#000}h2{break-before:auto}}
"""


def _h(x):
    return html.escape("" if x is None else str(x))


def _table(headers, rows, limit=None):
    rows = list(rows)
    more = ""
    if limit and len(rows) > limit:
        more = f"<p class='muted'>Showing {limit} of {len(rows):,}. The full list is in the CSV named above.</p>"
        rows = rows[:limit]
    if not rows:
        return "<p class='muted'>None.</p>"
    head = "".join(f"<th>{_h(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(f"<td>{c if isinstance(c, Raw) else _h(c)}</td>" for c in r) + "</tr>" for r in rows)
    return f"<div class='table-wrap'><table><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>{more}"


class Raw(str):
    pass


def _img(path):
    if not path or not os.path.exists(path):
        return ""
    with open(path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode()
    return f"<img alt='' src='data:image/png;base64,{b64}'>"


def _mask_df(df, sensitive):
    df = df.copy()
    for c in df.columns:
        if c in sensitive:
            df[c] = df[c].map(lambda v: mask_value(v) if v else v)
    return df


def audit_html(d, out, interactive=False):
    """The interactive, self-contained audit report (see html_report.py)."""
    return html_report.build(d, out, interactive=interactive)


def _legacy_audit_html(d, out):
    man, p0, p1, issues, log, val = d["man"], d["p0"], d["p1"], d["issues"], d["log"], d["val"]
    sens = d["sensitive"]
    r = man.get("result", {})
    applied, flagged, disabled, failed = _status_counts(log)
    status = val["status"] if val else "NOT VALIDATED"
    cls = "ok" if status == "PASSED" else "warn" if "WARN" in status else "bad"
    inp = man["input"]
    S = []
    S.append(f"<h1>Data audit report</h1><p class='muted'>{_h(inp['file'])} · generated by dataMedic "
             f"{_h(man.get('version'))} · validation <span class='pill {cls}'>{_h(status)}</span></p>")
    S.append("<nav><ol>" + "".join(f"<li><a href='#s{i}'>{t}</a></li>" for i, t in enumerate(
        ["Analysis overview", "Original dataset", "Issues detected", "Cleaning operations", "Before and after",
         "Removed records", "Modified values", "Retained anomalies", "Validation results", "Unresolved issues",
         "Recovery and reproducibility"], 1)) + "</ol></nav>")
    q0 = quality_scores(p0)
    q1 = quality_scores(p1) if p1 else {"completeness_pct": "n/a"}
    S.append("<h2 id='s1'>1. Analysis overview</h2>")
    S.append("<div class='grid'>" + "".join(f"<div class='stat'><b>{_h(v)}</b><span>{_h(k)}</span></div>" for k, v in [
        ("rows before", fmt_num(p0["rows"])), ("rows after", fmt_num(r.get("rows_after", "n/a"))),
        ("columns before / after", f"{p0['columns']} / {r.get('columns_after', 'n/a')}"),
        ("values changed", fmt_num(r.get("cells_changed", 0))), ("rows removed", fmt_num(r.get("rows_removed", 0))),
        ("rows flagged for review", fmt_num(r.get("rows_flagged", 0))),
        ("completeness before / after", f"{q0['completeness_pct']}% / {q1['completeness_pct']}%"),
        ("issues found", len(issues))]) + "</div>")
    scope = man.get("analysis_scope", {})
    S.append(f"<p>Scope: {_h(scope.get('note', 'full dataset'))}. Operations applied: {len(applied)}; flag rules: "
             f"{len(flagged)}; proposed but left for a decision: {len(disabled)}; proposed and declined: "
             f"{len(_declined(log))}; failed: {len(failed)}.</p>")
    S.append("<h2 id='s2'>2. Original dataset</h2>")
    S.append(_table(["Property", "Value"], [["File", inp["file"]], ["Format", inp["format"]],
                                            ["Encoding", inp.get("encoding") or "n/a"], ["Delimiter", repr(inp.get("delimiter")) if inp.get("delimiter") else "n/a"],
                                            ["Size (bytes)", fmt_num(inp["size_bytes"])], ["SHA-256", Raw(f"<code>{inp['sha256']}</code>")],
                                            ["Rows", fmt_num(p0["rows"])], ["Columns", p0["columns"]],
                                            ["Exact duplicate rows", fmt_num(p0["exact_duplicate_rows"])], ["Empty rows", fmt_num(p0["empty_rows"])]]))
    S.append(_table(["Column", "Inferred type", "Roles", "Non-null", "Missing", "Missing %", "Unique", "Min", "Max", "Mean", "Median"],
                    [[c["column"], c["inferred_type"], ", ".join(c["roles"]), fmt_num(c["non_null_count"]),
                      fmt_num(c["missing_count"]), c["missing_pct"], fmt_num(c["unique_count"]),
                      "" if "sensitive" in c["roles"] else fmt_num(c.get("min")) if isinstance(c.get("min"), (int, float)) else _h(c.get("min", "")),
                      "" if "sensitive" in c["roles"] else fmt_num(c.get("max")) if isinstance(c.get("max"), (int, float)) else _h(c.get("max", "")),
                      fmt_num(c.get("mean")) if c.get("mean") is not None else "", fmt_num(c.get("median")) if c.get("median") is not None else ""]
                     for c in p0["columns_profile"]]))
    S.append("<h2 id='s3'>3. Issues detected</h2>")
    S.append(_table(["ID", "Severity", "Kind", "Column", "Count", "Detail", "Action"],
                    [[i["issue_id"], i["severity"], i["kind"], i["column"] or "(table)",
                      fmt_num(i["count"]) if i["count"] is not None else "", i["detail"], i["planned_action"]] for i in issues]))
    S.append("<h2 id='s4'>4. Cleaning operations</h2><p>In the order they ran. Every value change is in "
             "<code>audit/cell_changes.csv</code>.</p>")
    S.append(_table(["Op", "Operation", "Column", "Issue", "Method", "Changed", "Status", "Why", "Basis", "Undo"],
                    [[e["op_id"], e["op"], e["column"], e.get("issue", ""), e.get("method", ""),
                      fmt_num(e.get("affected", 0)) + (f" (+{e['flagged']} flagged)" if e.get("flagged") else ""),
                      e.get("status", ""), e.get("rationale", ""),
                      _basis(e), e.get("reversal", "")] for e in log if e.get("op")]))
    S.append("<h2 id='s5'>5. Before and after</h2>")
    imgs = []
    ch = d["changes"]
    cleaned = d["cleaned"]
    for e in applied:
        if e["op"] in ("impute", "winsorize", "set_missing_where") and e.get("affected") and charts.available():
            c = e["params"]["column"]
            if c in sens or c not in cleaned.columns:
                continue
            sub = ch[(ch["op_id"] == e["op_id"]) & (ch["column"] == c)]
            after = pd.to_numeric(cleaned[c].mask(cleaned[c] == ""), errors="coerce")
            if after.notna().sum() < 5:
                continue
            before = after.copy()
            idx = cleaned[ROW_ID].astype(int)
            m = idx.isin(sub[ROW_ID].astype(int))
            old = sub.set_index(sub[ROW_ID].astype(int))["old_value"]
            olds = idx[m].map(old)
            before[m] = pd.to_numeric(olds.mask(olds == ""), errors="coerce").values
            p = os.path.join(out, "visualisations", "cleaning", f"{e['op_id']}_{c}.png"[:80])
            try:
                charts.before_after(before, after, f"{c} ({e['op_id']} {e['op']})", p)
                imgs.append(f"<h3>{_h(c)}: {_h(e['op_id'])} {_h(e.get('method', ''))}</h3>" + _img(p))
            except Exception:
                pass
            if len(imgs) >= 8:
                break
    if p1:
        f1 = {c["column"]: c for c in p1["columns_profile"]}
        rows = []
        for c in p0["columns_profile"]:
            name = d["schema"].get("renamed_columns", {}).get(c["column"], c["column"])
            a = f1.get(name)
            if not a:
                rows.append([c["column"], c["inferred_type"], "(removed)", fmt_num(c["missing_count"]), "", "", ""])
                continue
            rows.append([name, c["inferred_type"], d["schema"]["columns"].get(name, {}).get("type", a["inferred_type"]),
                         fmt_num(c["missing_count"]), fmt_num(a["missing_count"]),
                         fmt_num(c["unique_count"]), fmt_num(a["unique_count"])])
        S.append(_table(["Column", "Type before", "Type after", "Missing before", "Missing after", "Unique before", "Unique after"], rows))
    S.append("".join(imgs) or "<p class='muted'>No numeric values were imputed, clipped or invalidated, so there is no distribution to compare.</p>")
    S.append("<h2 id='s6'>6. Removed records</h2><p>Full original rows are in <code>audit/removed_records.csv</code>.</p>")
    rem = _mask_df(d["removed"], sens)
    cols = [c for c in rem.columns][:12]
    S.append(_table(cols, rem[cols].values.tolist(), limit=50))
    S.append("<h2 id='s7'>7. Modified values</h2><p>Sensitive columns are masked here; the CSV keeps the real "
             "values so the changes can be reversed.</p>")
    chm = ch.copy()
    if len(chm):
        sm = chm["column"].isin(sens)
        chm.loc[sm, "old_value"] = chm.loc[sm, "old_value"].map(lambda v: mask_value(v) if v else v)
        chm.loc[sm, "new_value"] = chm.loc[sm, "new_value"].map(lambda v: mask_value(v) if v else v)
        summ = chm.groupby(["op_id", "column"]).size().reset_index(name="values changed")
        S.append(_table(["Op", "Column", "Values changed"], summ.values.tolist()))
    S.append(_table(["Op", "Row", "Column", "Original", "New"], chm[["op_id", ROW_ID, "column", "old_value", "new_value"]].values.tolist(), limit=200))
    S.append("<h2 id='s8'>8. Retained anomalies</h2><p>Flagged rows keep their values. They need a person's judgement. "
             "Full list: <code>audit/flagged_records.csv</code>.</p>")
    fl = d["flags"]
    if len(fl):
        summ = fl.groupby(["classification", "column", "reason"]).size().reset_index(name="rows")
        S.append(_table(["Classification", "Column", "Reason", "Rows"], summ.values.tolist()))
        S.append(_table(["Flag", "Row", "Column", "Value", "Reason"], fl[["flag_id", ROW_ID, "column", "value", "reason"]].values.tolist(), limit=100))
    else:
        S.append("<p class='muted'>Nothing was flagged.</p>")
    S.append("<h2 id='s9'>9. Validation results</h2>")
    if val:
        S.append(_table(["Check", "Result", "Severity", "Detail"], [[c["check"], Raw(
            f"<span class='{'ok' if c['passed'] else 'bad' if c['severity'] == 'critical' else 'warn'}'>"
            f"{'passed' if c['passed'] else 'FAILED'}</span>"), c["severity"], c["detail"]] for c in val["checks"]]))
    else:
        S.append("<p class='bad'>Validation has not been run. Run <code>dm.py validate</code>.</p>")
    S.append("<h2 id='s10'>10. Unresolved issues</h2>")
    unres = [[e["op_id"], e["column"], e["issue"], e["rationale"]] for e in disabled]
    if val:
        unres += [["validation", "", c["check"], c["detail"]] for c in val["checks"] if not c["passed"]]
    unres += [[e["op_id"], e["column"], "operation failed", e.get("error", "")] for e in failed]
    S.append(_table(["Item", "Column", "Issue", "Detail"], unres) if unres else "<p>None.</p>")
    S.append("<h2 id='s11'>11. Recovery and reproducibility</h2>")
    sw = man.get("software", {})
    S.append(_table(["Item", "Value"], [
        ["Original file", Raw(f"<code>{_h(inp['path'])}</code> (never modified; sha256 checked during validation)")],
        ["Reference copy", Raw(f"<code>{_h(inp.get('reference_copy') or 'none (large file)')}</code>")],
        ["Rebuild the original", Raw("<code>python scripts/dm.py revert &lt;output folder&gt;</code> rebuilds it from the cleaned data and audit files and checks it cell by cell")],
        ["Re-run the cleaning", Raw("<code>python scripts/dm.py clean &lt;output folder&gt;</code> replays <code>plan.json</code>; validation checks the result is identical")],
        ["Row identity", Raw(f"<code>{ROW_ID}</code> is the row's position in the original table (1 = first data row)")],
        ["Plan sha256", Raw(f"<code>{_h(man.get('plan_sha256', ''))}</code>")], ["Random seed", man.get("random_seed")],
        ["Software", ", ".join(f"{k} {v}" for k, v in sw.items() if k != "platform")]]))
    body = "\n".join(S)
    doc = (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>Data audit report</title><style>{CSS}</style></head><body><main>{body}</main></body></html>")
    p = os.path.join(out, "reports", "data_audit_report.html")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return p


def _basis(e):
    b = e.get("statistical_basis") or {}
    if not b:
        ev = e.get("evidence") or {}
        return ", ".join(f"{k}: {v}" for k, v in list(ev.items())[:3] if not isinstance(v, (dict, list)))[:200]
    parts = []
    for k in ("value", "fallback_value", "lower_bound", "upper_bound", "fitted_on_rows", "groups", "order_by", "features"):
        if k in b and b[k] is not None:
            v = b[k]
            parts.append(f"{k.replace('_', ' ')} {fmt_num(v) if isinstance(v, (int, float)) else v}")
    db, da = b.get("distribution_before", {}), b.get("distribution_after", {})
    if db and da:
        parts.append(f"mean {fmt_num(db.get('mean'))} to {fmt_num(da.get('mean'))}, sd {fmt_num(db.get('std'))} to {fmt_num(da.get('std'))}")
    return "; ".join(parts)


def audit_workbook(d, out):
    if optional("openpyxl") is None:
        return None
    p = os.path.join(out, "audit", "audit_workbook.xlsx")
    sens = d["sensitive"]
    ops = pd.DataFrame([{k: (v if not isinstance(v, (dict, list)) else str(v)) for k, v in e.items()} for e in d["log"]])
    sheets = {"operations": ops, "cell_changes": d["changes"].head(1_000_000),
              "removed_records": _mask_df(d["removed"], sens).head(1_000_000),
              "flagged_records": d["flags"].head(1_000_000)}
    if d.get("evidence") is not None:
        sheets["imputation_evidence"] = d["evidence"].head(1_000_000)
    if d["val"]:
        sheets["validation"] = pd.DataFrame(d["val"]["checks"]).astype(str)
    sheets["issues"] = pd.DataFrame(d["issues"]).astype(str)
    write_excel_safe(sheets, p)
    return p


def summary_facts(d):
    man, p0, p1, val, e = d["man"], d["p0"], d["p1"], d["val"], d["eda"]
    applied, flagged, disabled, failed = _status_counts(d["log"])
    imputed = sum(x.get("affected", 0) for x in applied if x["op"] in ("impute", "apply_rule"))
    dup = sum(x.get("affected", 0) for x in applied if x["op"] == "drop_duplicates")
    invalid = sum(x.get("affected", 0) for x in applied if x["op"] == "set_missing_where")
    q0 = quality_scores(p0)
    return {"file": man["input"]["file"], "rows_before": p0["rows"], "rows_after": man.get("result", {}).get("rows_after"),
            "columns_before": p0["columns"], "columns_after": man.get("result", {}).get("columns_after"),
            "missing_cells_before": q0["missing_cells"], "missing_values_imputed": imputed,
            "duplicate_rows_removed": dup, "invalid_values_set_missing": invalid,
            "values_changed": man.get("result", {}).get("cells_changed"),
            "rows_flagged": man.get("result", {}).get("rows_flagged"),
            "open_decisions": [x["op_id"] for x in disabled], "failed_operations": [x["op_id"] for x in failed],
            "validation_status": val["status"] if val else "NOT VALIDATED",
            "completeness_before_pct": q0["completeness_pct"],
            "completeness_after_pct": quality_scores(p1)["completeness_pct"] if p1 else None,
            "sampled": man.get("analysis_scope", {}).get("sampled"),
            "rows_quarantined": int((d["removed"].get("dm_disposition", pd.Series(dtype=str)) == "quarantined").sum()),
            "imputation_evidence_levels": (d["evidence"].groupby("evidence_level").size().to_dict()
                                           if d.get("evidence") is not None and len(d["evidence"]) else {}),
            "quality_dimensions": (val or {}).get("quality_dimensions"),
            "contract_rules_failed": [r["id"] for r in ((val or {}).get("contract") or {}).get("rules", []) if not r["passed"]],
            "top_finding_candidates": [{"id": c["candidate_id"], "title": c["title"]} for c in (e or {}).get("finding_candidates", [])[:8]]}


def build_reports(out, interactive=None):
    d = _load(out)
    charts.configure(d["cfg"])
    if interactive is None:
        interactive = bool(d["cfg"].get("report", {}).get("interactive"))
    rd = os.path.join(out, "reports")
    os.makedirs(rd, exist_ok=True)
    written = []
    with open(os.path.join(rd, "data_quality_report.md"), "w", encoding="utf-8", newline="\n") as fh:
        fh.write(quality_md(d))
    written.append("reports/data_quality_report.md")
    if d["eda"]:
        with open(os.path.join(rd, "exploratory_data_analysis.md"), "w", encoding="utf-8", newline="\n") as fh:
            fh.write(eda_md(d))
        written.append("reports/exploratory_data_analysis.md")
    audit_html(d, out, interactive=interactive)
    written.append("reports/data_audit_report.html")
    wb = audit_workbook(d, out)
    if wb:
        written.append("audit/audit_workbook.xlsx")
    write_json(os.path.join(rd, "summary_facts.json"), summary_facts(d))
    written.append("reports/summary_facts.json")
    return written
