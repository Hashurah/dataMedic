#!/usr/bin/env python3
"""dataMedic command line.

  discover FILE [FILE ...]          what is in the file(s): format, encoding, sheets, tables, shape
  profile  FILE [--out DIR]         baseline profile, issues and a proposed plan.json (changes nothing)
  clean    OUT [--safe-only | --max-risk LEVEL]  apply OUT/plan.json; writes cleaned data and the audit trail
  validate OUT [--contract FILE]    independent checks, including revert, replay and a data contract
  eda      OUT [--target C --time C]  exploratory analysis and tests on the cleaned data
  report   OUT [--interactive]      generated reports (quality, EDA, HTML audit, Excel audit)
  compare  OUT COLUMN               masked-value comparison of imputation methods for one column
  restore  OUT --op OP-ID           copy of the cleaned data with one step undone (cleaned data untouched)
  calc     OUT NAME EXPR            record a derived number (for example a growth rate) for review
  review   OUT                      final review: deliverables, numbers traceable, writing style
  revert   OUT [--to FILE]          rebuild the original table from cleaned data + audit files
  relate   PARENT:KEY CHILD:KEY     key uniqueness and referential integrity between two tables
  audit    OUT                      print the transformation log
  run      FILE [--out DIR]         profile + clean + validate + eda + report with the proposed plan
  check                             which Python and libraries dataMedic is using

Exit codes: 0 ok, 1 error, 2 validation failed or review found problems.

The libraries come from the first of: the Python in $DATAMEDIC_PYTHON, the skill's own .venv
(created by `python setup.py`), or the Python running this script.
"""

import argparse
import json
import os
import subprocess
import sys

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _has_libraries():
    from importlib.util import find_spec
    return find_spec("numpy") is not None and find_spec("pandas") is not None


def _environment_python():
    """The Python that has dataMedic's libraries, if it is not the current one."""
    explicit = os.environ.get("DATAMEDIC_PYTHON")
    if explicit:
        return explicit
    venv = os.path.join(SKILL_DIR, ".venv", "Scripts" if os.name == "nt" else "bin",
                        "python.exe" if os.name == "nt" else "python")
    return venv if os.path.exists(venv) else None


def _reexec_if_needed():
    """Run under the skill's environment when it exists, so nothing has to be activated."""
    if os.environ.get("DATAMEDIC_REEXEC"):
        return
    target = _environment_python()
    if target and os.path.normcase(os.path.abspath(target)) != os.path.normcase(os.path.abspath(sys.executable)):
        env = dict(os.environ, DATAMEDIC_REEXEC="1")
        try:
            sys.exit(subprocess.run([target, os.path.abspath(__file__), *sys.argv[1:]], env=env).returncode)
        except OSError as exc:
            print(f"dataMedic error: cannot run the Python at {target} ({exc}). Fix DATAMEDIC_PYTHON, or "
                  f"re-create the environment with: python {os.path.join(SKILL_DIR, 'setup.py')}", file=sys.stderr)
            sys.exit(1)
    if not _has_libraries():
        print("dataMedic error: pandas and numpy are not installed for " + sys.executable + ".\n"
              "Install them once with:  python " + os.path.join(SKILL_DIR, "setup.py") + "\n"
              "(this creates a private environment in the skill folder that dm.py then uses automatically),\n"
              "or set DATAMEDIC_PYTHON to a Python that already has them.", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    _reexec_if_needed()

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datamedic import term  # noqa: E402
from datamedic.common import ROW_ID, DataMedicError, fmt_num, read_json, write_json  # noqa: E402


def _print_json(obj):
    print(json.dumps(obj, indent=2, ensure_ascii=False, default=str))


def cmd_discover(a):
    from datamedic.loader import compare_schemas, discover
    infos = [discover(p) for p in a.files]
    if a.json:
        out = {"files": infos}
        if len(infos) > 1:
            out["comparison"] = compare_schemas(infos)
        _print_json(out)
        return 0
    for i in infos:
        print(f"== {i['file']}  ({i['format']}, {fmt_num(i['size_bytes'])} bytes)")
        if i.get("error"):
            print(f"   ERROR: {i['error']}")
        for k in ("encoding", "delimiter", "header_detected", "preamble_rows_skipped", "rows", "columns",
                  "empty_rows", "malformed_rows", "short_rows_padded", "json_path"):
            if k in i:
                print(f"   {k}: {i[k]!r}" if k == "delimiter" else f"   {k}: {i[k]}")
        if i.get("column_names"):
            print("   columns: " + ", ".join(i["column_names"][:40]) + (" ..." if len(i["column_names"]) > 40 else ""))
        for s in i.get("sheets", []):
            print(f"   sheet '{s['sheet']}': {s['rows']} rows x {s['columns']} cols, {len(s['tables'])} block(s); "
                  f"header: {', '.join(s.get('first_header', [])[:12])}")
        for t in i.get("tables", []):
            print(f"   table '{t['table']}': {t['rows']} rows; pk {t['primary_key']}; fks {t['foreign_keys']}; "
                  f"columns {', '.join(t['columns'][:15])}")
        for w in i.get("warnings", []):
            print(f"   warning: {w}")
    if len(infos) > 1:
        cmp = compare_schemas(infos)
        print("== comparison")
        for p in cmp["pairs"]:
            print(f"   {p['a']} vs {p['b']}: {p['assessment']}; shared: {', '.join(p['shared_columns'][:12]) or 'none'}"
                  + (f"; name variants: {p['name_variants'][:5]}" if p["name_variants"] else ""))
    return 0


def cmd_profile(a):
    from datamedic.pipeline import default_out, run_profile
    out = a.out or default_out(a.file)
    r = run_profile(a.file, out, sheet=a.sheet, table=a.table, block=a.block, sample=a.sample, cfg_path=a.config,
                    force_plan=a.force_plan)
    p, plan = r["profile"], r["plan"]
    print(f"Output folder: {out}")
    print(f"Rows {fmt_num(p['rows'])}, columns {p['columns']}, exact duplicate rows {p['exact_duplicate_rows']}, "
          f"empty rows {p['empty_rows']}" + (" (SAMPLED)" if r["meta"].get("sampled") else ""))
    dom = p["domain"]["candidates"]
    if dom:
        print(f"Likely domain: {dom[0]['domain']} ({p['domain']['confidence']} confidence; {', '.join(dom[0]['matched_terms'])})")
    print("Columns:")
    for c in p["columns_profile"]:
        print(f"  - {c['column']}: {c['inferred_type']} [{', '.join(c['roles'])}] missing {c['missing_count']} "
              f"({c['missing_pct']}%), unique {c['unique_count']}")
    print(f"Issues: {len(r['issues'])}")
    for i in r["issues"]:
        if i["severity"] in ("critical", "high", "medium"):
            print(f"  [{i['severity']}] {i['issue_id']} {i['kind']} {i['column'] or ''}: {i['detail']}")
    ops = plan["operations"]
    print(f"Plan: {len(ops)} operations, {sum(o['enabled'] for o in ops)} enabled, "
          f"{len(plan['review_required'])} need a decision ({', '.join(plan['review_required']) or 'none'})")
    print(f"Written: {r['plan_file']}, profiling/initial_profile.json, profiling/issues.json")
    return 0


def cmd_clean(a):
    from datamedic.pipeline import load_manifest, run_clean
    man = load_manifest(a.out)
    plan = a.plan or os.path.join(a.out, "plan.json")
    src = man["input"]["path"]
    if not os.path.exists(src):
        src = os.path.join(a.out, man["input"]["reference_copy"])
    max_risk = "safe" if a.safe_only else a.max_risk
    eng = run_clean(src, plan, a.out, max_risk=max_risk)
    r = read_json(os.path.join(a.out, "metadata", "analysis_manifest.json"))["result"]
    print(f"Rows {fmt_num(r['rows_before'])} -> {fmt_num(r['rows_after'])}; columns {r['columns_before']} -> "
          f"{r['columns_after']}; values changed {fmt_num(r['cells_changed'])}; rows removed {r['rows_removed']}"
          f" (of which quarantined {r.get('rows_quarantined', 0)}); rows flagged {r['rows_flagged']}")
    print(f"Operations: {r['operations_applied']} applied, {r['operations_disabled']} disabled, "
          f"{r['operations_failed']} failed" + (f", {r['operations_held_by_risk_limit']} held back by the "
                                               f"'{max_risk}' risk limit" if max_risk else ""))
    for e in eng.log:
        if e.get("status") == "failed":
            term.line(f"  FAILED {e['op_id']} {e['op']} {e.get('column', '')}: {e.get('error')}", "fail")
        elif "above this run's limit" in str(e.get("status", "")):
            term.line(f"  held   {e['op_id']} {e['op']} {e.get('column', '')} (risk {e.get('risk')})", "warn")
    return 1 if r["operations_failed"] else 0


def cmd_validate(a):
    from datamedic.pipeline import mark
    from datamedic.validate import validate
    res = validate(a.out, replay=not a.no_replay, contract_path=a.contract)
    mark(a.out, "validate", "done" if res["status"] != "FAILED" else "failed")
    kind = "ok" if res["status"] == "PASSED" else "fail" if res["status"] == "FAILED" else "warn"
    term.line(f"Validation: {res['status']} ({res['critical_failures']} critical, {res['warnings']} warnings, "
              f"{sum(c['passed'] for c in res['checks'])}/{len(res['checks'])} checks passed)", kind)
    for c in res["checks"]:
        k = "ok" if c["passed"] else "fail" if c["severity"] == "critical" else "warn"
        term.line(f"  {'ok  ' if c['passed'] else 'FAIL' if c['severity'] == 'critical' else 'warn'} {c['check']}"
                  + (f": {c['detail']}" if c["detail"] else ""), None if c["passed"] else k)
    if res["open_review_items"]:
        print("Open decisions (disabled, need review): " + ", ".join(res["open_review_items"]))
    return 2 if res["status"] == "FAILED" else 0


def cmd_eda(a):
    from datamedic.eda import EDA
    from datamedic.pipeline import mark
    res = EDA(a.out, target=a.target, time_col=a.time, charts_on=not a.no_charts).run()
    mark(a.out, "explore")
    print(f"EDA: {len(res['measures'])} measures, {len(res['categories'])} grouping columns, time column "
          f"{res['time_column'] or 'none'}, {res['tests']['count']} tests, {len(res['charts'])} charts")
    for c in res["finding_candidates"][:10]:
        print(f"  {c['candidate_id']} [{c['type']}] {c['title']}")
    print("Written: analysis/eda_results.json, visualisations/")
    return 0


def cmd_report(a):
    from datamedic.pipeline import mark
    from datamedic.report import build_reports
    for f in build_reports(a.out, interactive=True if a.interactive else None):
        print("Written: " + f)
    mark(a.out, "report")
    return 0


def cmd_calc(a):
    from datamedic.review import record_calc
    v = record_calc(a.out, a.name, a.expression, a.source or "")
    print(f"{a.name} = {v!r}  (recorded in analysis/derived.json)")
    return 0


def cmd_review(a):
    from datamedic.review import review
    res = review(a.out)
    print(f"Final review: {res['status']} ({len(res['problems'])} problems, {len(res['warnings'])} warnings)")
    for p in res["problems"]:
        print("  PROBLEM " + "; ".join(f"{k}: {v}" for k, v in p.items()))
    for w in res["warnings"][:40]:
        print("  warning " + "; ".join(f"{k}: {v}" for k, v in w.items() if k != "text"))
    return 2 if res["problems"] else 0


def cmd_revert(a):
    from datamedic.clean import frames_equal, revert
    from datamedic.loader import load_table
    from datamedic.pipeline import load_manifest
    df = revert(a.out)
    man = load_manifest(a.out)
    src = man["input"]["path"] if os.path.exists(man["input"]["path"]) else os.path.join(a.out, man["input"]["reference_copy"])
    lo = man.get("load_options", {})
    raw, _ = load_table(src, sheet=lo.get("sheet"), table=lo.get("table"), block=lo.get("block"),
                        sample=lo.get("sample"), seed=lo.get("seed", 42))
    eq, det = frames_equal(df, raw)
    if a.to:
        out = df.drop(columns=[ROW_ID]) if not a.keep_row_id else df
        out.to_csv(a.to, index=False, encoding="utf-8", lineterminator="\n")
        print(f"Written: {a.to}")
    print("Rebuilt table matches the original exactly." if eq else f"MISMATCH: {det}")
    return 0 if eq else 2


def cmd_restore(a):
    from datamedic.clean import restore_step
    df, rep_ = restore_step(a.out, a.op)
    dest = a.to or os.path.join(a.out, "restored", f"cleaned_without_{a.op}.csv")
    os.makedirs(os.path.dirname(os.path.abspath(dest)), exist_ok=True)
    df.to_csv(dest, index=False, encoding="utf-8", lineterminator="\n")
    write_json(os.path.splitext(dest)[0] + "_report.json", rep_)
    print(f"{a.op} ({rep_['op']}) undone in a copy: {rep_['cells_restored']} cells and {rep_['rows_restored']} rows "
          f"restored" + (f", columns back: {', '.join(rep_['columns_restored'])}" if rep_["columns_restored"] else "")
          + (f", indicator dropped: {', '.join(rep_['columns_dropped'])}" if rep_["columns_dropped"] else ""))
    for n in rep_["notes"]:
        term.line("  note: " + n, "warn")
    print(f"Written: {dest} (the cleaned data in cleaned_data/ is unchanged)")
    return 0


def cmd_compare(a):
    from datamedic.clean import Engine
    from datamedic.common import merged_config, optional
    from datamedic.impute_eval import candidates_for, compare_methods
    from datamedic.loader import load_table
    from datamedic.pipeline import load_manifest
    man = load_manifest(a.out)
    plan = read_json(os.path.join(a.out, "plan.json"))
    src = man["input"]["path"] if os.path.exists(man["input"]["path"]) else os.path.join(a.out, man["input"]["reference_copy"])
    lo = man.get("load_options", {})
    raw, _ = load_table(src, sheet=lo.get("sheet"), table=lo.get("table"), block=lo.get("block"),
                        sample=lo.get("sample"), seed=lo.get("seed", 42))
    pre = [o for o in plan["operations"] if o.get("enabled", True) and o["op"] not in ("impute", "winsorize")]
    df = Engine(raw, {"operations": pre, "settings": plan.get("settings", {})}).run().df
    if a.column not in df.columns:
        raise DataMedicError(f"column '{a.column}' is not in the data after the plan's other steps")
    cfg = merged_config(plan.get("settings", {}).get("config"))
    s = df[a.column]
    numeric = s.dtype.kind in "fi" or str(s.dtype) == "Int64"
    cats = [c for c in (a.group_by or plan.get("roles", {}).get("category", [])) if c in df.columns and c != a.column]
    nums = [c for c in df.columns if c not in (a.column, ROW_ID) and (df[c].dtype.kind in "fi" or str(df[c].dtype) == "Int64")]
    specs = candidates_for(df, a.column, numeric, cats, nums, a.time, sklearn=optional("sklearn") is not None)
    res = compare_methods(df, a.column, specs, cfg, cfg.get("seed", 42), group_for_subgroups=cats[0] if cats else None)
    path = os.path.join(a.out, "analysis", f"imputation_comparison_{''.join(ch if ch.isalnum() else '_' for ch in a.column)}.json")
    write_json(path, res)
    if res.get("status") != "ok":
        print(f"{a.column}: {res.get('status')}")
        return 0
    key = "nmae" if res["kind"] == "numeric" else "accuracy"
    rows = [[n, f"{m[key]:.3f}" if m.get(key) is not None else "", f"{m['coverage'] * 100:.0f}%",
             fmt_num(m.get("bias")) if res["kind"] == "numeric" else fmt_num(m.get("macro_f1"))]
            if m.get("status") == "ok" else [n, "could not run", "", m.get("error", "")[:50]]
            for n, m in sorted(res["methods"].items(), key=lambda t: t[0] != res["best"])]
    term.table(["Method", "normalised MAE" if key == "nmae" else "accuracy", "filled",
                "bias" if key == "nmae" else "macro-F1"], rows,
               title=f"{a.column}: {res['masked_per_repeat']} known values hidden x {res['repeats']} (best first)")
    for lim in res["limitations"]:
        print("  limitation: " + lim)
    print(f"Written: {path}")
    return 0


def cmd_relate(a):
    from datamedic.relations import relate
    res = relate(a.parent, a.child, sheet_p=a.parent_sheet, sheet_c=a.child_sheet)
    _print_json(res)
    return 0


def cmd_audit(a):
    log = read_json(os.path.join(a.out, "audit", "transformation_log.json"))["operations"]
    for e in log:
        if not e.get("op"):
            continue
        print(f"{e['op_id']:8} {e['op']:22} {str(e.get('column', ''))[:24]:24} {str(e.get('status', ''))[:34]:34} "
              f"changed {e.get('affected', 0)}" + (f", flagged {e['flagged']}" if e.get("flagged") else ""))
    return 0


def cmd_check(a):
    from datamedic.pipeline import versions
    v = versions()
    print(f"Python: {sys.executable} ({v['python']})")
    print(f"Environment: {'skill .venv' if os.path.join(SKILL_DIR, '.venv') in sys.executable else 'DATAMEDIC_PYTHON' if os.environ.get('DATAMEDIC_PYTHON') else 'current Python'}")
    for k in ("pandas", "numpy", "scipy", "matplotlib", "seaborn", "openpyxl", "pyarrow", "sklearn", "plotly", "rich"):
        print(f"  {k:11} {v[k]}")
    notes = {"scipy": "statistical tests are skipped", "matplotlib": "no charts", "openpyxl": "no Excel input or copies",
             "pyarrow": "no Parquet or Feather input",
             "sklearn": "no KNN or iterative imputation, no Isolation Forest (a simpler robust distance is used)",
             "seaborn": "charts are drawn with matplotlib alone", "plotly": "no interactive charts in the HTML report",
             "rich": "plain terminal output"}
    for k, why in notes.items():
        if v[k] == "not installed":
            print(f"Without {k}: {why}.")
    return 0


def cmd_run(a):
    from datamedic.pipeline import default_out
    out = a.out or default_out(a.file)
    a.out = out
    a.force_plan = True
    steps = [cmd_profile, cmd_clean, cmd_validate, cmd_eda, cmd_report]
    a.plan, a.no_replay, a.target, a.time, a.no_charts = None, False, a.target, a.time, a.no_charts
    a.contract, a.interactive = getattr(a, "contract", None), getattr(a, "interactive", False)
    a.safe_only, a.max_risk = getattr(a, "safe_only", False), getattr(a, "max_risk", None)
    term.heading("dataMedic: profile, clean, validate, explore, report")
    worst = 0
    for s in steps:
        term.heading(s.__name__[4:])
        rc = s(a)
        worst = max(worst, rc)
        if rc == 1:
            return 1
    f = os.path.join(out, "reports", "summary_facts.json")
    if os.path.exists(f):
        sf = read_json(f)
        val = read_json(os.path.join(out, "audit", "validation_results.json"))
        rows = [["Original records", fmt_num(sf["rows_before"])], ["Final records", fmt_num(sf["rows_after"])],
                ["Values imputed", fmt_num(sf["missing_values_imputed"])],
                ["Duplicates removed", fmt_num(sf["duplicate_rows_removed"])],
                ["Rows quarantined", fmt_num(sf.get("rows_quarantined", 0))], ["Rows flagged", fmt_num(sf["rows_flagged"])],
                ["Validation checks passed", f"{sum(c['passed'] for c in val['checks'])}/{len(val['checks'])}"],
                ["Open decisions", ", ".join(sf["open_decisions"]) or "none"]]
        term.table(["", ""], rows, title=f"Final summary: validation {sf['validation_status']}")
    print("\nNext: write reports/executive_summary.md and reports/insights_and_recommendations.md from "
          "templates/, then run 'review'.")
    return worst


def main(argv=None):
    try:
        sys.stdout.reconfigure(errors="replace")
    except (AttributeError, ValueError):
        pass
    ap = argparse.ArgumentParser(prog="dm.py", description="dataMedic: clean, validate and explore datasets with an audit trail.")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("discover")
    s.add_argument("files", nargs="+")
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_discover)

    def load_args(s):
        s.add_argument("--sheet")
        s.add_argument("--table")
        s.add_argument("--block", type=int)
        s.add_argument("--sample", type=int, help="analyse a random sample of N rows (recorded as sample-based)")
        s.add_argument("--config", help="JSON config overriding config/default.json")

    s = sub.add_parser("profile")
    s.add_argument("file")
    s.add_argument("--out")
    s.add_argument("--force-plan", action="store_true", help="overwrite an existing plan.json")
    load_args(s)
    s.set_defaults(fn=cmd_profile)
    s = sub.add_parser("clean")
    s.add_argument("out")
    s.add_argument("--plan")
    g = s.add_mutually_exclusive_group()
    g.add_argument("--safe-only", action="store_true", help="apply only steps that cannot lose information")
    g.add_argument("--max-risk", choices=["safe", "moderate", "high"],
                   help="apply enabled steps up to this risk level; higher ones are recorded as held back")
    s.set_defaults(fn=cmd_clean)
    s = sub.add_parser("validate")
    s.add_argument("out")
    s.add_argument("--no-replay", action="store_true")
    s.add_argument("--contract", help="data contract JSON (default: OUT/contract.json if present)")
    s.set_defaults(fn=cmd_validate)
    s = sub.add_parser("eda")
    s.add_argument("out")
    s.add_argument("--target")
    s.add_argument("--time")
    s.add_argument("--no-charts", action="store_true")
    s.set_defaults(fn=cmd_eda)
    s = sub.add_parser("report")
    s.add_argument("out")
    s.add_argument("--interactive", action="store_true", help="add plotly charts (needs plotly; adds about 4 MB)")
    s.set_defaults(fn=cmd_report)
    s = sub.add_parser("compare")
    s.add_argument("out")
    s.add_argument("column")
    s.add_argument("--group-by", nargs="*", help="grouping columns to try for group-based methods")
    s.add_argument("--time", help="date column for interpolation")
    s.set_defaults(fn=cmd_compare)
    s = sub.add_parser("restore")
    s.add_argument("out")
    s.add_argument("--op", required=True, help="the step to undo, e.g. OP-007")
    s.add_argument("--to", help="output CSV (default OUT/restored/cleaned_without_<op>.csv)")
    s.set_defaults(fn=cmd_restore)
    s = sub.add_parser("calc")
    s.add_argument("out")
    s.add_argument("name")
    s.add_argument("expression")
    s.add_argument("--source", help="where the inputs came from, e.g. 'eda_results.json time_series rows first/last'")
    s.set_defaults(fn=cmd_calc)
    s = sub.add_parser("review")
    s.add_argument("out")
    s.set_defaults(fn=cmd_review)
    s = sub.add_parser("revert")
    s.add_argument("out")
    s.add_argument("--to")
    s.add_argument("--keep-row-id", action="store_true")
    s.set_defaults(fn=cmd_revert)
    s = sub.add_parser("relate")
    s.add_argument("parent", help="FILE:KEY_COLUMN (the table whose key should be unique)")
    s.add_argument("child", help="FILE:FOREIGN_KEY_COLUMN")
    s.add_argument("--parent-sheet")
    s.add_argument("--child-sheet")
    s.set_defaults(fn=cmd_relate)
    s = sub.add_parser("audit")
    s.add_argument("out")
    s.set_defaults(fn=cmd_audit)
    s = sub.add_parser("check")
    s.set_defaults(fn=cmd_check)
    s = sub.add_parser("run")
    s.add_argument("file")
    s.add_argument("--out")
    s.add_argument("--target")
    s.add_argument("--time")
    s.add_argument("--no-charts", action="store_true")
    load_args(s)
    s.set_defaults(fn=cmd_run)
    a = ap.parse_args(argv)
    try:
        return a.fn(a)
    except DataMedicError as exc:
        print(f"dataMedic error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
