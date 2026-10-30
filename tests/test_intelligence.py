"""Tests for the row decisions, rules, risk limits, imputation evidence, contracts, diagnostics and reporting."""

import json
import os
import re
import subprocess
import sys

import numpy as np
import pandas as pd
import pytest

from datamedic.clean import read_str_csv
from datamedic.common import ROW_ID, read_json, sha256_file, write_json


def _pipeline(tmp_path, df, edit=None, name="d", max_risk=None, contract=None, eda=False, report=False):
    from datamedic.pipeline import run_clean, run_profile
    from datamedic.validate import validate
    p = tmp_path / f"{name}.csv"
    df.to_csv(p, index=False)
    out = str(tmp_path / f"out_{name}")
    run_profile(str(p), out)
    plan_path = os.path.join(out, "plan.json")
    if edit:
        plan = read_json(plan_path)
        edit(plan)
        write_json(plan_path, plan)
    if contract:
        write_json(os.path.join(out, "contract.json"), contract)
    run_clean(str(p), plan_path, out, max_risk=max_risk)
    val = validate(out)
    res = {"out": out, "val": val, "plan": read_json(plan_path)}
    if eda:
        from datamedic.eda import EDA
        res["eda"] = EDA(out, charts_on=False).run()
    if report:
        from datamedic.report import build_reports
        build_reports(out)
    return res


def _add(plan, op, params, **extra):
    n = len(plan["operations"]) + 1
    plan["operations"].append({"op_id": f"OP-{n:03d}", "op": op, "params": params, "issue": "test", "rationale": "test",
                               "enabled": True, "needs_review": False, "evidence": {}, "decided_by": "agent", **extra})
    return f"OP-{n:03d}"


def _log(out):
    return {e["op_id"]: e for e in read_json(os.path.join(out, "audit", "transformation_log.json"))["operations"]}


def _cleaned(out):
    return read_str_csv(os.path.join(out, "cleaned_data", "cleaned_dataset.csv"))


def _passed(val, prefix):
    return all(c["passed"] for c in val["checks"] if c["check"].startswith(prefix))


# ---------------------------------------------------------------- order and dependencies

def test_duplicates_are_judged_before_columns_are_dropped(tmp_path):
    df = pd.DataFrame({"a": [1, 1, 2], "b": ["x", "x", "y"], "note": ["first", "second", "z"]})

    def edit(plan):
        plan["operations"] = [o for o in plan["operations"] if o["op"] != "drop_duplicates"]
        _add(plan, "drop_columns", {"columns": ["note"], "reason": "not needed"})
        _add(plan, "drop_duplicates", {"reason": "exact duplicate"})

    r = _pipeline(tmp_path, df, edit)
    # rows 1 and 2 differ in 'note', so they are not duplicates even though 'note' is dropped later
    assert len(_cleaned(r["out"])) == 3
    assert r["val"]["status"] != "FAILED"


def test_dependencies_hold_back_a_step_whose_prerequisite_did_not_run(tmp_path):
    df = pd.DataFrame({"a": ["1", "2", "x", "4"] * 10, "b": range(40)})

    def edit(plan):
        dep = _add(plan, "replace_values", {"column": "b", "mapping": {"1": "100"}})
        plan["operations"][-1]["enabled"] = False
        _add(plan, "flag_rows", {"column": "b", "rule": {"gt": 50}, "reason": "big"}, depends_on=[dep])
        _add(plan, "trim_whitespace", {"columns": ["a"]}, depends_on=["OP-999"])

    r = _pipeline(tmp_path, df, edit)
    log = _log(r["out"])
    flag = next(e for e in log.values() if e.get("op") == "flag_rows" and e.get("depends_on"))
    assert flag["status"].startswith("not applied (depends on")
    bad = next(e for e in log.values() if e.get("depends_on") == ["OP-999"])
    assert bad["status"] == "failed" and "not in the plan" in bad["error"]


def test_a_dependency_that_runs_later_is_rejected(tmp_path):
    df = pd.DataFrame({"a": [1.0, None, 3.0] * 10, "b": range(30)})

    def edit(plan):
        later = _add(plan, "flag_rows", {"column": "b", "rule": {"gt": 5}, "reason": "big"})
        _add(plan, "trim_whitespace", {"columns": ["a"]}, depends_on=[later])

    r = _pipeline(tmp_path, df, edit)
    e = next(e for e in _log(r["out"]).values() if e.get("depends_on"))
    assert e["status"] == "failed" and "runs later" in e["error"]


def test_risk_limit_holds_back_higher_risk_steps_and_replay_respects_it(tmp_path):
    df = pd.DataFrame({"id": range(60), "v": [" a "] * 30 + ["b"] * 30, "x": list(range(59)) + [10_000]})

    def edit(plan):
        _add(plan, "winsorize", {"column": "x", "lower_q": 0.0, "upper_q": 0.95})

    r = _pipeline(tmp_path, df, edit, max_risk="safe")
    log = _log(r["out"])
    held = [e for e in log.values() if "above this run's limit" in str(e.get("status"))]
    assert any(e["op"] == "winsorize" for e in held)
    assert all(e.get("risk") != "safe" for e in held)
    assert _passed(r["val"], "replaying") and _passed(r["val"], "revert")


# ---------------------------------------------------------------- row decisions

def test_quarantine_keeps_rows_out_of_cleaned_data_and_reversible(tmp_path):
    df = pd.DataFrame({"id": range(1, 31), "amount": [10.0] * 28 + [-5.0, -7.0], "grp": ["a", "b"] * 15})

    def edit(plan):
        _add(plan, "quarantine_rows", {"rule": {"column": "amount", "lt": 0}, "column": "amount",
                                       "reason": "negative amount; source system to confirm"})

    r = _pipeline(tmp_path, df, edit)
    out = r["out"]
    assert len(_cleaned(out)) == 28
    q = read_str_csv(os.path.join(out, "audit", "quarantined_records.csv"))
    assert len(q) == 2 and set(q["amount"]) == {"-5", "-7"} or set(q["amount"]) == {"-5.0", "-7.0"}
    rs = read_str_csv(os.path.join(out, "audit", "row_status.csv"))
    assert (rs["record_status"] == "quarantined").sum() == 2
    assert rs.loc[rs["record_status"] == "quarantined", "quarantine_reason"].str.contains("negative").all()
    assert r["val"]["status"] != "FAILED" and _passed(r["val"], "revert") and _passed(r["val"], "quarantined")


def test_analytical_exclusion_keeps_the_row_but_leaves_it_out_of_eda(tmp_path):
    rng = np.random.default_rng(0)
    df = pd.DataFrame({"id": range(1, 101), "income": rng.normal(50_000, 5_000, 100).round(0),
                       "score": rng.normal(10, 2, 100).round(2)})
    df.loc[[0, 1], "income"] = [9_000_000, 8_000_000]

    def edit(plan):
        _add(plan, "flag_rows", {"column": "income", "rule": {"gt": 1_000_000}, "reason": "implausible income",
                                 "classification": "potential anomaly", "exclude_from": ["income"]})

    r = _pipeline(tmp_path, df, edit, eda=True)
    assert len(_cleaned(r["out"])) == 100
    ex = r["eda"]["analytical_exclusions"]
    assert ex and ex[0]["rows"] == 2 and ex[0]["columns"] == ["income"]
    assert r["eda"]["univariate"]["numeric"]["income"]["max"] < 1_000_000
    assert r["eda"]["univariate"]["numeric"]["score"]["n"] == 100
    rs = read_str_csv(os.path.join(r["out"], "audit", "row_status.csv"))
    assert (rs["analytical_exclusions"] != "").sum() == 2


def test_duplicates_keep_the_latest_record_and_report_differences(tmp_path):
    df = pd.DataFrame({"customer": ["A", "A", "B", "C"], "updated": ["2024-01-01", "2024-03-01", "2024-02-01", "2024-02-02"],
                       "email": ["old@x.com", "new@x.com", "b@x.com", "c@x.com"]})

    def edit(plan):
        plan["operations"] = [o for o in plan["operations"] if o["op"] != "drop_duplicates"]
        _add(plan, "drop_duplicates", {"reason": "one row per customer", "subset": ["customer"], "keep": "latest",
                                       "order_by": "updated"})

    r = _pipeline(tmp_path, df, edit)
    c = _cleaned(r["out"])
    assert c.loc[c["customer"] == "A", "updated"].iloc[0].startswith("2024-03-01")
    rem = read_str_csv(os.path.join(r["out"], "audit", "removed_records.csv"))
    assert len(rem) == 1 and "email" in rem["dm_differing_columns"].iloc[0]
    assert rem["retained_row_id"].iloc[0] == "2"
    e = next(e for e in _log(r["out"]).values() if e.get("op") == "drop_duplicates")
    assert e["details"]["removed_rows_that_differ_from_the_kept_row"] == 1
    assert _passed(r["val"], "revert")


# ---------------------------------------------------------------- rules

def test_business_rule_fills_only_matching_missing_cells_with_provenance(tmp_path):
    df = pd.DataFrame({"order": range(30), "product": ["phone", "phone", "cable", "cable", "phone"] * 6,
                       "warranty_months": [24, None, None, 0, None] * 6})

    def edit(plan):
        plan["operations"] = [o for o in plan["operations"] if not (o["op"] == "impute")]
        _add(plan, "apply_rule", {"rule_id": "BR-1", "when": [{"column": "product", "op": "eq", "value": "phone"}],
                                  "set": {"warranty_months": 24}, "source": "2024 warranty policy, section 3"})

    r = _pipeline(tmp_path, df, edit)
    c = _cleaned(r["out"])
    phone = c[c["product"] == "phone"]["warranty_months"]
    cable = c[c["product"] == "cable"]["warranty_months"]
    assert (phone != "").all() and (cable == "").sum() == 6  # cables are not covered by the rule: left missing
    ev = read_str_csv(os.path.join(r["out"], "audit", "imputation_evidence.csv"))
    assert len(ev) == 12 and (ev["method"] == "domain rule").all()
    assert ev["source"].str.contains("warranty policy").all()
    assert r["val"]["status"] != "FAILED"


def test_conflicting_rules_fail_and_change_nothing(tmp_path):
    df = pd.DataFrame({"kind": ["a", "a", "b"] * 5, "v": [None, 1, None] * 5})

    def edit(plan):
        plan["operations"] = [o for o in plan["operations"] if o["op"] != "impute"]
        _add(plan, "apply_rule", {"rule_id": "R1", "when": [{"column": "kind", "op": "eq", "value": "a"}],
                                  "set": {"v": 5}, "source": "policy A", "only_if_missing": False})
        _add(plan, "apply_rule", {"rule_id": "R2", "when": [{"column": "v", "op": "ge", "value": 0}],
                                  "set": {"v": 7}, "source": "policy B", "only_if_missing": False})

    r = _pipeline(tmp_path, df, edit)
    log = _log(r["out"])
    r2 = next(e for e in log.values() if e.get("params", {}).get("rule_id") == "R2")
    assert r2["status"] == "failed" and "conflicts" in r2["error"]
    ch = read_str_csv(os.path.join(r["out"], "audit", "cell_changes.csv"))
    assert not (ch["op_id"] == r2["op_id"]).any()


def test_a_rule_without_a_source_is_refused(tmp_path):
    from datamedic.clean import Engine
    from datamedic.common import DataMedicError
    raw = pd.DataFrame({ROW_ID: [1, 2], "a": ["x", ""]}).astype({"a": object})
    op = {"op_id": "OP-001", "op": "apply_rule", "params": {"rule_id": "R", "when": [], "set": {"a": "y"}, "source": ""}}
    with pytest.raises(DataMedicError):
        Engine(raw, {"operations": [op]}).run()


# ---------------------------------------------------------------- time series gaps

def test_interpolation_leaves_long_gaps_missing_and_respects_groups(tmp_path):
    days = pd.date_range("2024-01-01", periods=30, freq="D").strftime("%Y-%m-%d").tolist()
    v = [float(i) for i in range(30)]
    for i in (5, 6):
        v[i] = None                       # a 2-step gap: filled
    for i in range(12, 17):
        v[i] = None                       # a 5-step gap: left missing
    df = pd.DataFrame({"date": days * 2, "store": ["A"] * 30 + ["B"] * 30, "sales": v + [x * 10 if x is not None else None for x in v]})

    def edit(plan):
        plan["operations"] = [o for o in plan["operations"] if o["op"] != "impute"]
        _add(plan, "impute", {"column": "sales", "method": "interpolate", "order_by": "date", "group_by": "store",
                              "max_gap": 3, "add_indicator": True})

    r = _pipeline(tmp_path, df, edit)
    c = _cleaned(r["out"])
    a = c[c["store"] == "A"]["sales"].tolist()
    b = c[c["store"] == "B"]["sales"].tolist()
    assert float(a[5]) == pytest.approx(5.0) and float(b[6]) == pytest.approx(60.0)  # interpolated within its own store
    assert all(x == "" for x in a[12:17]) and all(x == "" for x in b[12:17])
    e = next(e for e in _log(r["out"]).values() if e.get("op") == "impute")
    assert e["statistical_basis"]["left_missing_because_gap_too_long"] == 10
    assert e["statistical_basis"]["uses_future_values"] is True
    ev = read_str_csv(os.path.join(r["out"], "audit", "imputation_evidence.csv"))
    assert set(ev["gap_length"]) == {"2"} and (ev["uses_future_values"] == "True").all()
    assert r["val"]["status"] != "FAILED"


def test_forward_fill_never_uses_later_values(tmp_path):
    df = pd.DataFrame({"t": range(20), "v": [1.0, None, 3.0, None, None] * 4})

    def edit(plan):
        plan["operations"] = [o for o in plan["operations"] if o["op"] != "impute"]
        _add(plan, "impute", {"column": "v", "method": "ffill", "order_by": "t", "add_indicator": False})

    r = _pipeline(tmp_path, df, edit)
    c = _cleaned(r["out"])
    assert c["v"].tolist()[:5] == ["1", "1", "3", "3", "3"] or c["v"].tolist()[:5] == ["1.0", "1.0", "3.0", "3.0", "3.0"]
    ev = read_str_csv(os.path.join(r["out"], "audit", "imputation_evidence.csv"))
    assert (ev["uses_future_values"] == "False").all()


# ---------------------------------------------------------------- imputation intelligence

def _grouped(n=400, seed=3):
    rng = np.random.default_rng(seed)
    g = rng.choice(["north", "south", "east", "west"], n)
    base = {"north": 100, "south": 300, "east": 600, "west": 1000}
    v = np.array([base[x] for x in g]) + rng.normal(0, 20, n)
    df = pd.DataFrame({"region": g, "spend": v.round(2), "id": range(n)})
    df.loc[rng.choice(n, 30, replace=False), "spend"] = np.nan
    return df


def test_masked_comparison_picks_the_clearly_better_method_and_records_alternatives(tmp_path):
    r = _pipeline(tmp_path, _grouped())
    op = next(o for o in r["plan"]["operations"] if o["op"] == "impute" and o["params"]["column"] == "spend")
    sc = op["evidence"]["strategy_comparison"]
    assert op["params"]["method"].startswith("group_") and op["params"]["group_by"] == "region"
    assert sc["methods"][sc["chosen"]]["nmae"] < 0.5 * sc["methods"]["mean"]["nmae"]
    assert "mean" in sc["rejected"] and sc["limitations"]
    comp = read_json(os.path.join(r["out"], "profiling", "imputation_comparison.json"))
    assert comp["columns"]["spend"]["masked_per_repeat"] > 0


def test_masked_comparison_does_not_see_the_hidden_values():
    from datamedic.impute_eval import compare_methods
    rng = np.random.default_rng(1)
    df = pd.DataFrame({ROW_ID: range(1, 301), "x": rng.normal(0, 1, 300)})
    res = compare_methods(df, "x", [{"name": "mean", "method": "mean"}], seed=1)
    m = res["methods"]["mean"]
    # with leakage the error would be ~0; for a standard normal the MAE of the mean is about 0.8
    assert 0.6 < m["nmae"] < 1.0 and m["coverage"] == 1.0


def test_every_imputed_cell_has_an_evidence_record_without_a_numeric_confidence(tmp_path):
    r = _pipeline(tmp_path, _grouped())
    ev = read_str_csv(os.path.join(r["out"], "audit", "imputation_evidence.csv"))
    ch = read_str_csv(os.path.join(r["out"], "audit", "cell_changes.csv"))
    imp_ops = {e["op_id"] for e in _log(r["out"]).values() if e.get("op") == "impute"}
    assert len(ev) == int(ch["op_id"].isin(imp_ops).sum()) > 0
    assert not any("confidence" in c for c in ev.columns)
    assert set(ev["evidence_level"]) <= {"strong supporting evidence", "moderate supporting evidence",
                                         "limited supporting evidence", "insufficient evidence"}
    assert (ev["validated"] == "True").all() and (ev["limitations"] != "").all()
    assert ev["support_n"].astype(int).min() >= 30


def test_tiny_groups_fall_back_and_are_graded_down(tmp_path):
    from datamedic.evidence import classify_evidence
    recs = [{"op_id": "OP-1", ROW_ID: 1, "column": "x", "method": "group_median", "imputed_value": "5", "support_n": 2,
             "group": "rare", "group_n": 2, "dispersion": "", "gap_length": "", "uses_future_values": False,
             "fallback_used": False, "source": ""},
            {"op_id": "OP-1", ROW_ID: 2, "column": "x", "method": "group_median", "imputed_value": "5", "support_n": 200,
             "group": "missing", "group_n": 0, "dispersion": "", "gap_length": "", "uses_future_values": False,
             "fallback_used": True, "source": ""}]
    ev = classify_evidence(recs, {}, None, {})
    assert ev["evidence_level"].tolist() == ["insufficient evidence", "limited supporting evidence"]


def test_littles_test_separates_random_from_value_related_missingness():
    from datamedic.missingness import littles_mcar
    rng = np.random.default_rng(5)
    n = 600
    a = rng.normal(0, 1, n)
    b = a * 0.8 + rng.normal(0, 0.6, n)
    mcar = pd.DataFrame({"a": a, "b": b.copy()})
    mcar.loc[rng.random(n) < 0.2, "b"] = np.nan
    mar = pd.DataFrame({"a": a, "b": b.copy()})
    mar.loc[a > 0.5, "b"] = np.nan
    assert littles_mcar(mcar)["p_value"] > 0.01
    assert littles_mcar(mar)["p_value"] < 0.001


def test_missingness_diagnostics_find_structural_and_joint_patterns(tmp_path):
    n = 200
    df = pd.DataFrame({"has_car": ["yes", "no"] * (n // 2), "car_make": ["vw", ""] * (n // 2),
                       "car_year": ["2015", ""] * (n // 2), "x": range(n)})
    df.loc[df.index[:10], "car_make"] = ""
    r = _pipeline(tmp_path, df)
    m = read_json(os.path.join(r["out"], "profiling", "missingness.json"))
    assert any(p["a"] in ("car_make", "car_year") and p["b"] in ("car_make", "car_year") for p in m["missing_together"])
    assert any(s["when_missing"] == "car_year" and s["always_missing"] == "car_make" for s in m["structural_candidates"])
    assert "cannot" in m["caveat"]


# ---------------------------------------------------------------- encoding and Excel safety

def test_mojibake_is_repaired_only_where_exact(tmp_path):
    df = pd.DataFrame({"city": ["cafÃ©", "SÃ£o Paulo", "Zürich", "plain"] * 5, "n": range(20)})
    r = _pipeline(tmp_path, df)
    assert any(o["op"] == "fix_encoding" for o in r["plan"]["operations"])
    c = _cleaned(r["out"])
    assert set(c["city"]) == {"café", "São Paulo", "Zürich", "plain"}
    assert _passed(r["val"], "revert")


def test_excel_copies_store_formula_like_text_as_text(tmp_path):
    openpyxl = pytest.importorskip("openpyxl")
    df = pd.DataFrame({"name": ["=HYPERLINK(\"http://x\")", "ok", "=1+1"] * 5, "v": range(15)})
    r = _pipeline(tmp_path, df)
    wb = openpyxl.load_workbook(os.path.join(r["out"], "cleaned_data", "cleaned_dataset.xlsx"))
    ws = wb.active
    cells = [c for row in ws.iter_rows() for c in row if isinstance(c.value, str) and c.value.startswith("=")]
    assert cells and all(c.data_type == "s" for c in cells)


# ---------------------------------------------------------------- contracts

def test_contract_checks_before_and_after_and_reports_conflicts(tmp_path):
    df = pd.DataFrame({"id": [f"A{i:03d}" for i in range(40)], "age": ["30"] * 38 + ["-1", "200"],
                       "status": ["open", "closed"] * 20})
    contract = {"rules": [
        {"id": "C1", "rule": "range", "column": "age", "min": 0, "max": 120, "provenance": "confirmed"},
        {"id": "C2", "rule": "pattern", "column": "id", "regex": r"A\d{3}", "provenance": "user"},
        {"id": "C3", "rule": "allowed", "column": "status", "values": ["open", "closed"], "provenance": "user"},
        {"id": "C4", "rule": "allowed", "column": "status", "values": ["pending"], "provenance": "provisional"},
        {"id": "C5", "rule": "required_columns", "columns": ["id", "age", "region"], "provenance": "user"}]}
    r = _pipeline(tmp_path, df, contract=contract)
    con = r["val"]["contract"]
    rules = {x["id"]: x for x in con["rules"]}
    assert rules["C1"]["violations_before_cleaning"] == 2 and rules["C1"]["violations"] == 0  # 'age' rule set them missing
    assert rules["C2"]["passed"] and rules["C3"]["passed"]
    assert not rules["C4"]["passed"] and rules["C4"]["severity"] == "warning"
    assert not rules["C5"]["passed"] and r["val"]["status"] == "FAILED"
    assert any(set(c["rules"]) == {"C3", "C4"} for c in con["conflicts"])
    qd = r["val"]["quality_dimensions"]
    assert qd["accuracy"]["status"] == "unverified" and "score" not in json.dumps(qd).lower().replace("no overall score", "")


# ---------------------------------------------------------------- analysis

def test_variable_ranking_and_multivariate_anomalies(tmp_path):
    rng = np.random.default_rng(7)
    n = 400
    x = rng.normal(100, 10, n)
    df = pd.DataFrame({"constantish": [1.0] * (n - 3) + [2.0, 3.0, 4.0], "revenue": x * 3 + rng.normal(0, 5, n),
                       "cost": x * 2 + rng.normal(0, 4, n), "visits": x + rng.normal(0, 3, n)})
    df.loc[0, ["revenue", "cost"]] = [100, 400]   # unusual combination, each value ordinary on its own
    r = _pipeline(tmp_path, df, eda=True)
    sel = [m["column"] for m in r["eda"]["variable_selection"]["measures"]]
    assert sel.index("revenue") < sel.index("constantish")
    an = r["eda"]["multivariate"]["anomalies"]
    assert an["flagged"] >= 1 and an["share_pct"] < 5
    assert an["rows"][0][ROW_ID] == 1


def test_anomaly_check_declines_discrete_scales(tmp_path):
    df = pd.DataFrame({"rating": [1, 2, 3, 4, 5] * 20, "stars": [5, 4, 3, 2, 1] * 20})
    r = _pipeline(tmp_path, df, eda=True)
    assert r["eda"]["multivariate"]["anomalies"]["status"].startswith("not run")


# ---------------------------------------------------------------- restore and report

def test_restore_undoes_one_step_in_a_copy(tmp_path):
    r = _pipeline(tmp_path, _grouped())
    out = r["out"]
    from datamedic.clean import restore_step
    h = sha256_file(os.path.join(out, "cleaned_data", "cleaned_dataset.csv"))
    op = next(e for e in _log(out).values() if e.get("op") == "impute" and str(e["status"]).startswith("applied"))
    df, rep = restore_step(out, op["op_id"])
    assert rep["cells_restored"] == op["affected"] and op["statistical_basis"]["indicator_column"] in rep["columns_dropped"]
    assert (df["spend"] == "").sum() == 30
    assert sha256_file(os.path.join(out, "cleaned_data", "cleaned_dataset.csv")) == h


def test_html_report_is_offline_escaped_and_complete(tmp_path):
    df = pd.DataFrame({"id": range(30), "comment": ["</script><script>alert(1)</script>", "<b>x</b>"] * 15,
                       "amount": [1.5, None, 3.0] * 10, "email": [f"user{i}@mail.com" for i in range(30)]})
    r = _pipeline(tmp_path, df, eda=True, report=True)
    html = open(os.path.join(r["out"], "reports", "data_audit_report.html"), encoding="utf-8").read()
    assert "<script>alert(1)</script>" not in html and "<b>x</b>" not in html
    assert not re.search(r"""(src|href)=['"]https?://""", html)
    for title in ("Cleaned data preview", "Data lineage", "Audit and recovery", "Missing values", "Validation"):
        assert title in html
    assert "user1@mail.com" not in html            # sensitive column masked in the preview
    assert "<svg" in html and "preview-data" in html


def test_interactive_report_embeds_plotly_inline_or_explains(tmp_path):
    from datamedic.report import build_reports
    days = pd.date_range("2024-01-01", periods=120, freq="D").strftime("%Y-%m-%d")
    df = pd.DataFrame({"date": days, "sales": np.arange(120) * 2.0 + 5, "units": np.arange(120) % 7 + 1.0})
    r = _pipeline(tmp_path, df, eda=True)
    build_reports(r["out"], interactive=True)
    html = open(os.path.join(r["out"], "reports", "data_audit_report.html"), encoding="utf-8").read()
    try:
        import plotly  # noqa: F401
        assert "Interactive charts" in html and "Plotly.newPlot" in html
        # the library is inline: no script or stylesheet is fetched from the network
        assert not re.search(r"""<(script|link)[^>]+(src|href)=["']https?://""", html)
    except ImportError:
        assert "plotly is not installed" in html


def test_charts_use_the_shared_theme_and_optional_svg(tmp_path):
    from datamedic import charts
    if not charts.available():
        pytest.skip("matplotlib not installed")
    charts.configure({"charts": {"formats": ["png", "svg"]}})
    try:
        p = charts.distribution(pd.Series(np.random.default_rng(0).normal(0, 1, 200)), "x", str(tmp_path / "d.png"))
        assert os.path.exists(p) and os.path.exists(str(tmp_path / "d.svg"))
        assert charts.library() in ("seaborn + matplotlib", "matplotlib")
    finally:
        charts.configure({"charts": {"formats": ["png"]}})


def test_terminal_output_is_plain_when_captured():
    dm = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "dm.py")
    env = {k: v for k, v in os.environ.items() if not k.startswith("DATAMEDIC_")}
    env["DATAMEDIC_PYTHON"] = sys.executable
    res = subprocess.run([sys.executable, dm, "check"], capture_output=True, text=True, env=env)
    assert res.returncode == 0 and "\x1b[" not in res.stdout and "seaborn" in res.stdout


def test_repeated_dates_do_not_make_a_zero_time_step(tmp_path):
    # transaction data: many rows per day; the step must be measured between distinct dates
    days = pd.date_range("2024-01-01", periods=60, freq="D")
    rng = np.random.default_rng(2)
    d = rng.choice(days.delete([20, 21, 22]), 600)
    df = pd.DataFrame({"date": pd.Series(d).dt.strftime("%Y-%m-%d"), "amount": rng.gamma(2, 50, 600).round(2)})
    r = _pipeline(tmp_path, df, eda=True)
    ts = r["eda"]["time_series"]
    assert ts["applicable"] and ts.get("native_step", "").startswith("1 days")
    assert ts["missing_steps"] == 3


def test_compare_and_restore_commands(tmp_path):
    r = _pipeline(tmp_path, _grouped())
    dm = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts", "dm.py")
    env = {k: v for k, v in os.environ.items() if not k.startswith("DATAMEDIC_")}
    env["DATAMEDIC_PYTHON"] = sys.executable
    res = subprocess.run([sys.executable, dm, "compare", r["out"], "spend", "--group-by", "region"],
                         capture_output=True, text=True, env=env)
    assert res.returncode == 0, res.stderr
    assert "group_median by region" in res.stdout and "limitation" in res.stdout
    assert os.path.exists(os.path.join(r["out"], "analysis", "imputation_comparison_spend.json"))
    op = next(e for e in _log(r["out"]).values() if e.get("op") == "impute")
    res = subprocess.run([sys.executable, dm, "restore", r["out"], "--op", op["op_id"]], capture_output=True, text=True,
                         env=env)
    assert res.returncode == 0, res.stderr
    assert os.path.exists(os.path.join(r["out"], "restored", f"cleaned_without_{op['op_id']}.csv"))
    res = subprocess.run([sys.executable, dm, "restore", r["out"], "--op", "OP-999"], capture_output=True, text=True,
                         env=env)
    assert res.returncode == 1 and "not in the transformation log" in res.stderr
