"""Tests for currency extraction and conversion, baseline drift contracts, missing-date diagnostics and per-row
anomaly scores."""

import os

import numpy as np
import pandas as pd
import pytest

from datamedic.clean import frames_equal, read_str_csv, revert
from datamedic.common import ROW_ID, DataMedicError, read_json, write_json
from datamedic.loader import load_table
from test_intelligence import _add, _cleaned, _log, _pipeline


def _ops(plan, op, column=None):
    return [o for o in plan["operations"] if o["op"] == op and (column is None or o["params"].get("column") == column)]


def _enable(plan, op, column, **params):
    for o in _ops(plan, op, column):
        o["enabled"] = True
        o["params"].update(params)


def _sales(n=40):
    marks = ["£{:.2f}", "${:.2f}", "{:.2f}", "USD {:.2f}"]
    rng = np.random.default_rng(3)
    amt = rng.uniform(10, 100, n).round(2)
    return pd.DataFrame({"order_id": range(1, n + 1),
                         "order_date": pd.date_range("2022-12-20", periods=n, freq="D").strftime("%Y-%m-%d"),
                         "amount": [marks[i % 4].format(a) for i, a in enumerate(amt)]}), amt


# ---------------------------------------------------------------- currency

def test_planner_keeps_the_currency_before_proposing_number_conversion(tmp_path):
    df, _ = _sales()
    r = _pipeline(tmp_path, df)
    ex = _ops(r["plan"], "extract_currency", "amount")
    num = _ops(r["plan"], "convert_type", "amount")
    conv = _ops(r["plan"], "convert_currency", "amount")
    assert ex and ex[0]["enabled"] and ex[0]["risk"] == "safe"
    assert num[0]["enabled"] is False and num[0]["depends_on"] == [ex[0]["op_id"]]
    assert conv[0]["enabled"] is False and conv[0]["needs_review"]
    c = _cleaned(r["out"])
    assert c["amount_currency"].tolist()[:4] == ["GBP", "USD", "Unmarked", "USD"]
    assert c["amount"].tolist()[0].startswith("£")  # extraction alone leaves the amount as it was
    assert r["val"]["status"] in ("PASSED", "PASSED WITH WARNINGS")


def test_conversion_with_one_rate_per_currency_is_recomputed_and_reversible(tmp_path):
    df, amt = _sales()

    def edit(plan):
        _enable(plan, "convert_type", "amount")
        _enable(plan, "convert_currency", "amount", target="USD", rates_file="rates.csv")
    out = str(tmp_path / "out_d")
    os.makedirs(out)
    pd.DataFrame({"currency": ["GBP", "USD"], "rate": [1.25, 1.0]}).to_csv(os.path.join(out, "rates.csv"), index=False)
    r = _pipeline(tmp_path, df, edit)
    c = _cleaned(r["out"]).set_index(ROW_ID)
    assert float(c.loc["1", "amount_USD"]) == pytest.approx(round(amt[0] * 1.25, 2))
    assert float(c.loc["2", "amount_USD"]) == pytest.approx(amt[1])
    assert c.loc["3", "amount_USD"] == ""  # unmarked: never guessed
    assert c.loc["1", "amount"] == f"{amt[0]:.2f}"
    fl = read_str_csv(os.path.join(r["out"], "audit", "flagged_records.csv"))
    assert (fl.classification == "not converted").sum() == 10
    assert any(ch["check"].startswith("amount_USD equals amount x amount_fx_rate") and ch["passed"]
               for ch in r["val"]["checks"])
    assert r["val"]["status"] in ("PASSED", "PASSED WITH WARNINGS"), \
        [ch for ch in r["val"]["checks"] if not ch["passed"]]
    raw, _ = load_table(str(tmp_path / "d.csv"))
    assert frames_equal(revert(r["out"]), raw)[0]


def test_dated_rates_use_the_latest_rate_on_or_before_each_date(tmp_path):
    df, amt = _sales()

    def edit(plan):
        _enable(plan, "convert_type", "amount")
        _enable(plan, "convert_type", "order_date")
        _enable(plan, "convert_currency", "amount", target="USD", rates_file="rates.csv", date_column="order_date")
    out = str(tmp_path / "out_d")
    os.makedirs(out)
    pd.DataFrame({"currency": ["GBP", "GBP"], "date": ["2022-12-25", "2023-01-10"],
                  "rate": [1.2, 1.3]}).to_csv(os.path.join(out, "rates.csv"), index=False)
    r = _pipeline(tmp_path, df, edit)
    c = _cleaned(r["out"]).set_index(ROW_ID)
    # GBP rows are 1, 5, 9, ...: dated 2022-12-20, 12-24, 12-28, ..., 2023-01-09, 2023-01-13
    assert c.loc["1", "amount_USD"] == "" and c.loc["5", "amount_USD"] == ""  # before the first rate
    assert float(c.loc["9", "amount_USD"]) == pytest.approx(round(amt[8] * 1.2, 2))
    assert c.loc["9", "amount_fx_rate_date"] == "2022-12-25"
    assert float(c.loc["25", "amount_USD"]) == pytest.approx(round(amt[24] * 1.3, 2))
    assert r["val"]["status"] in ("PASSED", "PASSED WITH WARNINGS")


def test_ambiguous_rates_fail_the_step_and_change_nothing(tmp_path):
    df, _ = _sales()

    def edit(plan):
        _enable(plan, "convert_type", "amount")
        _enable(plan, "convert_currency", "amount", target="USD", rates_file="rates.csv")
    out = str(tmp_path / "out_d")
    os.makedirs(out)
    pd.DataFrame({"currency": ["GBP", "GBP"], "rate": [1.2, 1.3]}).to_csv(os.path.join(out, "rates.csv"), index=False)
    r = _pipeline(tmp_path, df, edit)
    e = next(e for e in _log(r["out"]).values() if e["op"] == "convert_currency")
    assert e["status"] == "failed" and "no 'date' column" in e["error"]
    assert "amount_USD" not in _cleaned(r["out"]).columns


# ---------------------------------------------------------------- baseline drift

def _shop(n, seed, electronics=0.4, mean=50.0, missing=0.05):
    rng = np.random.default_rng(seed)
    cat = rng.choice(["Electronics", "Clothing", "Home"], n, p=[electronics, (1 - electronics) / 2, (1 - electronics) / 2])
    spend = rng.normal(mean, 10, n).round(2).astype(object)
    spend[rng.random(n) < missing] = ""
    return pd.DataFrame({"id": range(n), "category": cat, "spend": spend})


def _baseline(tmp_path, **kw):
    from datamedic.contracts import build_baseline
    b = _pipeline(tmp_path, _shop(2000, 1), name="base")
    c = build_baseline(b["out"], **kw)
    path = str(tmp_path / "baseline.json")
    write_json(path, c)
    return c, path


def _batch(tmp_path, df, path, name):
    from datamedic.validate import validate
    r = _pipeline(tmp_path, df, name=name)
    return validate(r["out"], replay=False, contract_path=path), r["out"]


def _contract_checks(val):
    return {r["rule"] + ":" + r["column"]: r for r in val["contract"]["rules"]}


def test_baseline_records_shares_means_and_missing_rates(tmp_path):
    c, _ = _baseline(tmp_path)
    kinds = {(r["rule"], r.get("column")) for r in c["rules"]}
    assert ("category_shares", "category") in kinds and ("mean", "spend") in kinds
    assert ("missing_share", "spend") in kinds and ("category_shares", "id") not in kinds
    assert all(r["provenance"] == "inferred" and r["on_fail"] == "warn" for r in c["rules"] if r["rule"] != "type"
               and r["rule"] != "required_columns")


def test_a_batch_like_the_baseline_passes_and_a_shifted_one_warns(tmp_path):
    _, path = _baseline(tmp_path)
    same, _ = _batch(tmp_path, _shop(1500, 2), path, "same")
    assert all(r["passed"] for r in same["contract"]["rules"]), \
        [r for r in same["contract"]["rules"] if not r["passed"]]
    moved, _ = _batch(tmp_path, _shop(1500, 3, electronics=0.55, mean=53.0, missing=0.15), path, "moved")
    rules = _contract_checks(moved)
    assert not rules["category_shares:category"]["passed"] and "Electronics" in rules["category_shares:category"]["detail"]
    assert not rules["mean:spend"]["passed"] and not rules["missing_share:spend"]["passed"]
    assert moved["status"] == "PASSED WITH WARNINGS"  # inferred drift rules warn by default


def test_halt_rules_fail_validation_and_stop_eda(tmp_path):
    from datamedic.eda import EDA
    _, path = _baseline(tmp_path, halt=True)
    val, out = _batch(tmp_path, _shop(1500, 3, electronics=0.6), path, "moved")
    assert val["status"] == "FAILED"
    with pytest.raises(DataMedicError, match="halt"):
        EDA(out, charts_on=False)
    assert EDA(out, charts_on=False, ignore_halt=True).run()["rows"] == 1500


def test_a_different_spelling_of_a_label_is_reported_but_not_drift(tmp_path):
    _, path = _baseline(tmp_path)
    df = _shop(1500, 2)
    df["category"] = df["category"].str.lower()
    val, _ = _batch(tmp_path, df, path, "lower")
    r = _contract_checks(val)["category_shares:category"]
    assert r["passed"] and "spelled differently" in r["detail"]


def test_an_explicit_tolerance_replaces_the_statistical_band(tmp_path):
    c, path = _baseline(tmp_path)
    for r in c["rules"]:
        if r["rule"] == "mean":
            r["relative_tolerance"] = 0.001
    write_json(path, c)
    val, _ = _batch(tmp_path, _shop(1500, 2), path, "strict")
    assert not _contract_checks(val)["mean:spend"]["passed"]


# ---------------------------------------------------------------- missing dates

def _dated(n=400, block=None, seed=0):
    rng = np.random.default_rng(seed)
    d = np.array(pd.date_range("2024-01-01", periods=n, freq="D").strftime("%Y-%m-%d"), dtype=object)
    if block:
        d[block[0]:block[1]] = ""
    else:
        d[rng.random(n) < 0.1] = ""
    d[5] = "03/04/2024"  # readable both ways
    return pd.DataFrame({"txn": range(1000, 1000 + n), "date": d, "value": rng.normal(10, 2, n).round(2)})


def _enable_dates(plan):
    _enable(plan, "convert_type", "date", date_order=None)


def test_missing_dates_in_one_block_are_reported_as_clustered_and_placed(tmp_path):
    r = _pipeline(tmp_path, _dated(block=(200, 240)), _enable_dates, eda=True)
    md = next(m for m in r["eda"]["missing_dates"] if m["column"] == "date")
    assert md["missing"] == 41
    reasons = {x["reason"].split(" (")[0]: x["rows"] for x in md["reasons"]}
    assert reasons == {"blank in the source": 40, "ambiguous day/month order": 1}
    o = md["orders"][0]
    assert o["runs_test"]["reading"].startswith("clustered") and o["longest_missing_run"] == 40
    assert md["placement"]["status"] == "placed by neighbouring rows"
    assert any(c["type"] == "missing data pattern" for c in r["eda"]["finding_candidates"])
    assert (_cleaned(r["out"])["date"] == "").sum() == 41  # analysis only: nothing is filled


def test_scattered_missing_dates_with_no_time_order_are_not_placed(tmp_path):
    df = _dated(seed=4).sample(frac=1, random_state=1).reset_index(drop=True)
    df["txn"] = np.random.default_rng(2).permutation(len(df))
    r = _pipeline(tmp_path, df, _enable_dates, eda=True)
    md = next(m for m in r["eda"]["missing_dates"] if m["column"] == "date")
    assert all(not o["runs_test"]["reading"].startswith("clustered") for o in md["orders"])
    assert md["placement"]["status"] == "not possible"


# ---------------------------------------------------------------- anomaly scores

def test_every_scored_row_gets_scores_and_percentiles(tmp_path):
    rng = np.random.default_rng(5)
    df = pd.DataFrame({"a": rng.normal(0, 1, 300).round(3), "b": rng.normal(5, 2, 300).round(3)})
    df.loc[0, ["a", "b"]] = [9.0, -9.0]
    r = _pipeline(tmp_path, df, eda=True)
    sf = r["eda"]["multivariate"]["anomalies"]["scores_file"]
    sc = pd.read_csv(os.path.join(r["out"], sf["file"]))
    assert len(sc) == 300 == sf["rows"]
    pc = [c for c in sc.columns if c.endswith("_percentile")]
    assert pc and sc[pc].min().min() > 0 and sc[pc].max().max() == 100
    assert int(sc.iloc[0][ROW_ID]) == int(_cleaned(r["out"])[ROW_ID].iloc[0])  # the planted row ranks first
    assert sc["listed_as_anomaly"].sum() == r["eda"]["multivariate"]["anomalies"]["flagged"]
