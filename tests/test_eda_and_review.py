"""EDA statistics are correct, findings are evidence-based, and the review catches invented numbers and AI prose."""

import os

import pandas as pd
import pytest

from datamedic.clean import read_str_csv
from datamedic.common import read_json
from datamedic.relations import relate
from datamedic.review import check_numbers, check_prose, record_calc, review, STYLE, OVERCLAIM


def test_descriptive_statistics_match_independent_calculation(runs):
    r = runs("simple_sales")
    df = read_str_csv(os.path.join(r["out"], "cleaned_data", "cleaned_dataset.csv"))
    x = pd.to_numeric(df["revenue"])
    s = r["eda"]["univariate"]["numeric"]["revenue"]
    assert s["mean"] == pytest.approx(x.mean())
    assert s["median"] == pytest.approx(x.median())
    assert s["std"] == pytest.approx(x.std(ddof=1))
    assert s["q1"] == pytest.approx(x.quantile(0.25)) and s["q3"] == pytest.approx(x.quantile(0.75))
    assert s["skewness"] == pytest.approx(x.skew())


def test_domain_and_roles(runs):
    p = runs("simple_sales")["profile"]["profile"]
    assert p["domain"]["candidates"][0]["domain"] == "sales and retail"
    roles = {c["column"]: c["roles"] for c in p["columns_profile"]}
    assert "identifier" in roles["order_id"] and "identifier" in roles["customer_id"]
    assert "measure" in roles["revenue"] and "time" in roles["order_date"]


def test_correlated_variables_found_with_collinearity_warning(runs):
    e = runs("correlated")["eda"]
    pairs = {(p["a"], p["b"]) for p in e["multivariate"]["highly_correlated_pairs"]}
    assert ("ad_spend", "clicks") in pairs or ("clicks", "ad_spend") in pairs
    assert max(v for v in e["multivariate"]["vif"].values() if v) > 10
    noise = [c for c in e["correlations"] if "noise" in (c["a"], c["b"])]
    assert all(abs(c["spearman_rho"]) < 0.2 for c in noise)
    assert any(c["type"] == "collinearity" for c in e["finding_candidates"])


def test_tests_record_hypotheses_choice_and_correction(runs):
    tests = runs("simple_sales")["eda"]["tests"]
    assert tests["correction"].startswith("Benjamini-Hochberg")
    for t in tests["results"]:
        for k in ("test", "p_value", "q_value_bh", "null_hypothesis", "effect_size", "why"):
            assert k in t, (k, t.get("test"))
    g = [t for t in tests["results"] if t["family"] == "group comparison"]
    assert all("assumptions" in t for t in g)


def test_group_difference_detected_where_planted(runs):
    e = runs("simple_sales")["eda"]
    cands = {c["title"] for c in e["finding_candidates"]}
    assert "unit_price differs by product_category" in cands
    assert not any("region" in c and "differs" in c for c in cands)  # region was random


def test_seasonality_and_trend_in_time_series(runs):
    ts = runs("timeseries")["eda"]["time_series"]
    assert ts["applicable"] and ts["frequency"] == "week"
    assert ts["series"]["daily_sales"]["trend"]["direction"] == "upward"


def test_relational_integrity(data):
    res = relate(f"{data['multitable'][0]}:customer_id", f"{data['multitable'][1]}:customer_id")
    assert res["parent"]["key_is_unique"] is False
    assert res["child"]["orphan_rows"] == 3  # C9, C8 and c2 ("C2 " matches after trimming)
    assert res["child"]["orphans_matching_after_case_or_space_fix"] == 1  # c2


def test_number_check_accepts_real_numbers_and_rejects_invented_ones():
    pool = {123.0, 15.0, 0.184, 1234.5678, 42512.3}
    ok = "Final rows: 123. Duplicates removed: 15. Growth was 18.4%. Mean 1,234.57. About 42,500 customers."
    assert check_numbers(ok, pool) == []
    bad = check_numbers("Revenue rose by 37.2% to 9,876.", pool)
    assert {b["number"] for b in bad} == {"37.2%", "9,876"}


def test_prose_checks_find_ai_tells_and_overclaiming():
    t = "This pivotal finding — a testament to growth — proves that discounts drive sales."
    style = {w["why"] for w in check_prose(t, STYLE)}
    claims = {w["found"].lower() for w in check_prose(t, OVERCLAIM)}
    assert "em or en dash; use a comma, colon, period or parentheses" in style
    assert "stock AI word" in style
    assert {"proves", "drive"} <= claims
    assert check_prose("Revenue was 18.4% higher in Q2 than in Q1.", STYLE + OVERCLAIM) == []


def test_review_end_to_end(runs, tmp_path):
    import shutil
    r = runs("simple_sales")
    out = str(tmp_path / "rev")
    shutil.copytree(r["out"], out)
    facts = read_json(os.path.join(out, "reports", "summary_facts.json"))
    rd = os.path.join(out, "reports")
    with open(os.path.join(rd, "executive_summary.md"), "w", encoding="utf-8") as fh:
        fh.write(f"# Executive summary\n\nThe file has {facts['rows_before']} rows. Revenue grew 999.9% overnight.\n")
    with open(os.path.join(rd, "insights_and_recommendations.md"), "w", encoding="utf-8") as fh:
        fh.write("# Insights\n\nNothing notable.\n")
    res = review(out)
    assert res["status"] == "PROBLEMS"
    assert [p["number"] for p in res["problems"] if p["check"] == "number traceable to results"] == ["999.9%"]
    v = record_calc(out, "made_up_growth", "(1099.9 - 110) / 110 * 100 / 9 + 0")
    assert v == pytest.approx(99.98989, rel=1e-4)
    with open(os.path.join(rd, "executive_summary.md"), "w", encoding="utf-8") as fh:
        fh.write(f"# Executive summary\n\nThe file has {facts['rows_before']} rows. Validation status: {facts['validation_status']}.\n")
    assert review(out)["status"] == "OK"


def test_calc_rejects_code():
    from datamedic.common import DataMedicError
    with pytest.raises(DataMedicError):
        record_calc(".", "x", "__import__('os').system('echo hi')")
