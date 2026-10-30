#!/usr/bin/env python3
"""dataMedic command line.

  discover FILE [FILE ...]          what is in the file(s): format, encoding, sheets, tables, shape
  profile  FILE [--out DIR]         baseline profile, issues and a proposed plan.json (changes nothing)
  clean    OUT                      apply OUT/plan.json; writes cleaned data and the audit trail
  validate OUT                      independent checks, including revert and replay
  eda      OUT [--target C --time C]  exploratory analysis and tests on the cleaned data
  report   OUT                      generated reports (quality, EDA, HTML audit, Excel audit)
  calc     OUT NAME EXPR            record a derived number (for example a growth rate) for review
  review   OUT                      final review: deliverables, numbers traceable, writing style
  revert   OUT [--to FILE]          rebuild the original table from cleaned data + audit files
  relate   PARENT:KEY CHILD:KEY     key uniqueness and referential integrity between two tables
  audit    OUT                      print the transformation log
  run      FILE [--out DIR]         profile + clean + validate + eda + report with the proposed plan

Exit codes: 0 ok, 1 error, 2 validation failed or review found problems.
"""

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from datamedic.common import ROW_ID, DataMedicError, fmt_num, read_json  # noqa: E402


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
    eng = run_clean(src, plan, a.out)
    r = read_json(os.path.join(a.out, "metadata", "analysis_manifest.json"))["result"]
    print(f"Rows {fmt_num(r['rows_before'])} -> {fmt_num(r['rows_after'])}; columns {r['columns_before']} -> "
          f"{r['columns_after']}; values changed {fmt_num(r['cells_changed'])}; rows removed {r['rows_removed']}; "
          f"rows flagged {r['rows_flagged']}")
    print(f"Operations: {r['operations_applied']} applied, {r['operations_disabled']} disabled, "
          f"{r['operations_failed']} failed")
    for e in eng.log:
        if e.get("status") == "failed":
            print(f"  FAILED {e['op_id']} {e['op']} {e.get('column', '')}: {e.get('error')}")
    return 1 if r["operations_failed"] else 0


def cmd_validate(a):
    from datamedic.pipeline import mark
    from datamedic.validate import validate
    res = validate(a.out, replay=not a.no_replay)
    mark(a.out, "validate", "done" if res["status"] != "FAILED" else "failed")
    print(f"Validation: {res['status']} ({res['critical_failures']} critical, {res['warnings']} warnings)")
    for c in res["checks"]:
        print(f"  {'ok  ' if c['passed'] else 'FAIL' if c['severity'] == 'critical' else 'warn'} {c['check']}"
              + (f": {c['detail']}" if c["detail"] else ""))
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
    for f in build_reports(a.out):
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


def cmd_run(a):
    from datamedic.pipeline import default_out
    out = a.out or default_out(a.file)
    a.out = out
    a.force_plan = True
    steps = [cmd_profile, cmd_clean, cmd_validate, cmd_eda, cmd_report]
    a.plan, a.no_replay, a.target, a.time, a.no_charts = None, False, a.target, a.time, a.no_charts
    worst = 0
    for s in steps:
        print(f"\n--- {s.__name__[4:]}")
        rc = s(a)
        worst = max(worst, rc)
        if rc == 1:
            return 1
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
    s.set_defaults(fn=cmd_clean)
    s = sub.add_parser("validate")
    s.add_argument("out")
    s.add_argument("--no-replay", action="store_true")
    s.set_defaults(fn=cmd_validate)
    s = sub.add_parser("eda")
    s.add_argument("out")
    s.add_argument("--target")
    s.add_argument("--time")
    s.add_argument("--no-charts", action="store_true")
    s.set_defaults(fn=cmd_eda)
    s = sub.add_parser("report")
    s.add_argument("out")
    s.set_defaults(fn=cmd_report)
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
