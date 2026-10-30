"""The validator must pass honest runs and catch tampering, and revert must rebuild the original."""

import json
import os
import shutil

import pandas as pd
import pytest

from datamedic.clean import frames_equal, read_str_csv, revert
from datamedic.common import read_json
from datamedic.loader import load_table
from datamedic.validate import validate

ALL = ["simple_sales", "heavy_missing", "duplicates", "outliers", "mixed_dates", "ambiguous_dates", "labels",
       "mixed_types", "skewed", "timeseries", "legit_extremes", "empty_cols", "placeholders", "correlated",
       "sensitive", "invalid_logic"]


@pytest.mark.parametrize("name", ALL)
def test_every_dataset_validates_and_reverts_exactly(runs, name):
    r = runs(name)
    assert r["validation"]["status"] in ("PASSED", "PASSED WITH WARNINGS"), \
        [c for c in r["validation"]["checks"] if not c["passed"]]
    raw, _ = load_table(r["src"])
    eq, det = frames_equal(revert(r["out"]), raw)
    assert eq, det


def test_reports_agree_with_cleaned_data(runs):
    r = runs("duplicates")
    df = read_str_csv(os.path.join(r["out"], "cleaned_data", "cleaned_dataset.csv"))
    facts = read_json(os.path.join(r["out"], "reports", "summary_facts.json"))
    assert facts["rows_after"] == len(df) == 123
    assert facts["duplicate_rows_removed"] == 15
    with open(os.path.join(r["out"], "reports", "data_quality_report.md"), encoding="utf-8") as fh:
        md = fh.read()
    assert "| Rows | 138 | 123 |" in md


def _copy(r, tmp_path, name):
    dst = str(tmp_path / name)
    shutil.copytree(r["out"], dst)
    for root, _, files in os.walk(dst):
        for f in files:
            os.chmod(os.path.join(root, f), 0o666)
    return dst


def _check(res, name):
    return next(c for c in res["checks"] if c["check"].startswith(name))


def test_validation_catches_an_unlogged_change(runs, tmp_path):
    out = _copy(runs("labels"), tmp_path, "tamper1")
    p = os.path.join(out, "cleaned_data", "cleaned_dataset.csv")
    df = read_str_csv(p)
    df.loc[5, "spend"] = "999999"
    df.to_csv(p, index=False, lineterminator="\n")
    res = validate(out, replay=False)
    assert res["status"] == "FAILED"
    assert not _check(res, "every changed value is in the change log")["passed"]
    assert not _check(res, "revert rebuilds the original")["passed"]


def test_validation_catches_a_lost_removed_record(runs, tmp_path):
    out = _copy(runs("duplicates"), tmp_path, "tamper2")
    p = os.path.join(out, "audit", "removed_records.csv")
    rem = read_str_csv(p)
    rem.iloc[1:].to_csv(p, index=False, lineterminator="\n")
    res = validate(out, replay=False)
    assert res["status"] == "FAILED"
    assert not _check(res, "row accounting")["passed"]


def test_validation_catches_a_broken_constraint(runs, tmp_path):
    out = _copy(runs("simple_sales"), tmp_path, "tamper3")
    p = os.path.join(out, "plan.json")
    plan = read_json(p)
    plan["constraints"].append({"column": "quantity", "rule": "range", "min": 1, "max": 3})
    with open(p, "w") as fh:
        json.dump(plan, fh)
    res = validate(out, replay=False)
    assert not _check(res, "quantity within")["passed"] and res["status"] == "FAILED"


def test_date_order_and_allowed_constraints(runs, tmp_path):
    out = _copy(runs("invalid_logic"), tmp_path, "constraints")
    p = os.path.join(out, "plan.json")
    plan = read_json(p)
    plan["constraints"] += [{"columns": ["start_date", "end_date"], "rule": "date_order", "severity": "warning"},
                            {"column": "unit_price", "rule": "allowed", "values": [10]}]
    with open(p, "w") as fh:
        json.dump(plan, fh)
    res = validate(out, replay=False)
    order = _check(res, "end_date not before start_date")
    assert not order["passed"] and order["severity"] == "warning" and order["detail"] == "2 rows"
    assert _check(res, "unit_price only has allowed values")["passed"]
    assert res["status"] == "PASSED WITH WARNINGS"


def test_validation_catches_a_modified_original(runs, tmp_path):
    r = runs("outliers")
    src_copy = tmp_path / "copy.csv"
    shutil.copy(r["src"], src_copy)
    out = _copy(r, tmp_path, "tamper4")
    man_p = os.path.join(out, "metadata", "analysis_manifest.json")
    man = read_json(man_p)
    man["input"]["path"] = str(src_copy)
    with open(man_p, "w") as fh:
        json.dump(man, fh)
    with open(src_copy, "a") as fh:
        fh.write("S1,1.0,1.0\n")
    res = validate(out, replay=False)
    assert not _check(res, "original file unchanged")["passed"]


def test_replay_check_passes_on_honest_run(runs):
    c = _check(runs("placeholders")["validation"], "replaying the plan")
    assert c["passed"], c


def test_cli_revert_writes_original(runs, tmp_path):
    import subprocess
    import sys
    r = runs("mixed_types")
    dst = str(tmp_path / "restored.csv")
    script = os.path.join(os.path.dirname(os.path.dirname(__file__)), "scripts", "dm.py")
    res = subprocess.run([sys.executable, script, "revert", r["out"], "--to", dst], capture_output=True, text=True)
    assert res.returncode == 0, res.stdout + res.stderr
    a = pd.read_csv(dst, dtype=str, keep_default_na=False)
    b = pd.read_csv(r["src"], dtype=str, keep_default_na=False)
    assert a.equals(b)
