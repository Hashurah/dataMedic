"""Cleaning decisions on datasets with planted problems."""

import os

import pandas as pd
import pytest

from datamedic.clean import read_str_csv
from datamedic.common import ROW_ID, read_json, sha256_file


def ops(run, op=None, column=None):
    plan = read_json(os.path.join(run["out"], "plan.json"))
    return [o for o in plan["operations"] if (op is None or o["op"] == op)
            and (column is None or o["params"].get("column") == column)]


def cleaned(run):
    return read_str_csv(os.path.join(run["out"], "cleaned_data", "cleaned_dataset.csv"))


def log(run):
    return read_json(os.path.join(run["out"], "audit", "transformation_log.json"))["operations"]


def audit(run, name):
    return read_str_csv(os.path.join(run["out"], "audit", name))


def test_original_file_is_never_modified(runs, data):
    before = sha256_file(data["simple_sales"])
    r = runs("simple_sales")
    assert sha256_file(r["src"]) == before
    ref = os.path.join(r["out"], "original_reference", os.path.basename(r["src"]))
    assert sha256_file(ref) == before
    assert not os.access(ref, os.W_OK)


def test_every_operation_is_logged_with_its_reasoning(runs):
    for e in log(runs("heavy_missing")):
        for k in ("op_id", "timestamp", "stage", "op", "status", "rationale", "reversible", "reversal"):
            assert k in e, (k, e["op_id"])


def test_missing_mar_uses_group_imputation_and_leaves_heavy_missing_alone(runs):
    r = runs("heavy_missing")
    inc = ops(r, "impute", "income")[0]
    assert inc["params"]["method"] == "group_median" and inc["params"]["group_by"] == "region"
    assert "MAR plausible" in inc["evidence"]["mechanism_assessment"]
    hours = ops(r, "impute", "hours_online")[0]
    assert hours["enabled"] is False and hours["needs_review"] is True
    assert not ops(r, "impute", "respondent_id")
    df = cleaned(r)
    assert (df["income"] != "").all()
    assert (df["hours_online"] == "").sum() == 224


def test_imputed_values_are_marked_exactly(runs):
    r = runs("heavy_missing")
    df = cleaned(r)
    ch = audit(r, "cell_changes.csv")
    op = ops(r, "impute", "age")[0]["op_id"]
    imputed_rows = set(ch[(ch.op_id == op) & (ch.column == "age")][ROW_ID].astype(int))
    flagged_rows = set(df[df["age_was_missing"] == "true"][ROW_ID].astype(int))
    assert imputed_rows == flagged_rows and len(imputed_rows) == 23


def test_skewed_data_uses_median_not_mean(runs):
    r = runs("skewed")
    o = ops(r, "impute", "income")[0]
    assert o["params"]["method"] == "median"
    applied = next(e for e in log(r) if e["op_id"] == o["op_id"])
    raw = pd.read_csv(r["src"])["income"]
    assert applied["statistical_basis"]["value"] == pytest.approx(raw.median())


def test_duplicates_exact_and_formatting_variants_removed_and_recoverable(runs):
    r = runs("duplicates")
    rem = audit(r, "removed_records.csv")
    assert len(rem) == 15  # 10 exact copies + 5 that differ only in case and spacing
    df = cleaned(r)
    kept = set(df[ROW_ID].astype(int))
    assert set(rem["retained_row_id"].astype(int)) <= kept
    raw = pd.read_csv(r["src"], dtype=str, keep_default_na=False)
    for _, row in rem.iterrows():
        orig = raw.iloc[int(row[ROW_ID]) - 1]
        assert all(orig[c] == row[c] for c in raw.columns)


def test_conflicting_identifiers_are_flagged_not_removed(runs):
    r = runs("duplicates")
    fl = audit(r, "flagged_records.csv")
    dup = fl[fl.classification == "duplicate identifier"]
    assert len(dup) == 6  # 3 IDs, each on 2 rows with different amounts
    assert len(cleaned(r)) == 123


def test_outliers_are_flagged_never_deleted(runs):
    r = runs("outliers")
    df = cleaned(r)
    assert len(df) == 200
    assert {"1000", "950", "-500"} <= set(df["reading"])
    fl = audit(r, "flagged_records.csv")
    assert {"1000", "950", "-500"} <= set(fl[fl.column == "reading"]["value"])
    assert not ops(r, "winsorize") and not ops(r, "drop_rows")


def test_legitimate_heavy_tail_is_not_treated_as_errors(runs):
    r = runs("legit_extremes")
    assert not ops(r, "set_missing_where")
    df = cleaned(r)
    raw = pd.read_csv(r["src"])
    assert pd.to_numeric(df["annual_revenue"]).sum() == pytest.approx(raw["annual_revenue"].sum())
    fl = audit(r, "flagged_records.csv")
    assert (fl.column == "annual_revenue").sum() < 0.1 * len(df)


def test_mixed_date_formats_parse_with_evidence_and_invalid_dates_flagged(runs):
    r = runs("mixed_dates")
    o = ops(r, "convert_type", "event_date")[0]
    assert o["enabled"] and o["params"]["date_order"] == "dmy"
    df = cleaned(r)
    assert df["event_date"].tolist()[:5] == ["2024-01-05", "2024-01-25", "2024-02-13", "2024-03-03", "2024-01-07"]
    fl = audit(r, "flagged_records.csv")
    assert (fl.classification == "conversion failure").sum() == 3
    ch = audit(r, "cell_changes.csv")
    assert (ch.old_value == "2024-13-45").sum() == 3


def test_ambiguous_dates_are_left_for_a_decision(runs):
    r = runs("ambiguous_dates")
    o = ops(r, "convert_type", "visit_date")[0]
    assert o["enabled"] is False and o["needs_review"] is True
    assert cleaned(r)["visit_date"].tolist()[0] == "01/02/2024"


def test_category_variants_merged_but_similar_words_only_suggested(runs):
    r = runs("labels")
    df = cleaned(r)
    assert set(df["gender"]) == {"Male", "Female"}
    assert "electronics " not in set(df["category"]) and "CLOTHING" not in set(df["category"])
    fuzzy = [o for o in ops(r, "standardize_categories", "category") if not o["enabled"]]
    assert fuzzy and fuzzy[0]["params"]["mapping"] == {"Electronic": "Electronics"}
    assert "Electronic" in set(df["category"])


def test_mixed_types_currency_percent_identifiers_booleans(runs):
    r = runs("mixed_types")
    df = cleaned(r)
    assert df["zip_code"].tolist()[:3] == ["02139", "10001", "00501"]
    assert set(df["in_stock"]) == {"true", "false"}
    v = df.set_index(ROW_ID)
    assert v.loc["3", "value"] == "1200"
    # 'n/a' and 'twelve' become missing, then the low-share imputation fills them and marks them
    assert v.loc["4", "value_was_missing"] == "true" and v.loc["5", "value_was_missing"] == "true"
    ch = audit(r, "cell_changes.csv")
    first = ch[ch.column == "value"].groupby(ROW_ID).first()
    assert first.loc["4", "old_value"] == "n/a" and first.loc["5", "old_value"] == "twelve"
    fl = audit(r, "flagged_records.csv")
    assert ((fl.column == "value") & (fl.classification == "unit inconsistency")).sum() == 1
    assert ((fl.column == "value") & (fl.classification == "conversion failure")).sum() == 1
    price = ops(r, "convert_type", "price")[0]
    assert price["enabled"] is False and "currenc" in price["issue"]


def test_misleading_placeholders(runs):
    r = runs("placeholders")
    df = cleaned(r)
    assert "None" in set(df["allergies"])  # a real answer, not missing
    assert "N/A" in set(df["notes"])
    w = df.set_index(ROW_ID)
    assert w.loc["4", "weight_kg_was_missing"] == "true" and w.loc["5", "weight_kg_was_missing"] == "true"
    ch = audit(r, "cell_changes.csv")
    first = ch[ch.column == "weight_kg"].groupby(ROW_ID).first()
    assert first.loc["4", "old_value"] == "-999" and first.loc["5", "old_value"] == "N/A"
    assert "0" in set(df["visits"])  # zero is a real count
    sent = [o for o in ops(r, "set_missing_where", "weight_kg") if o["params"]["rule"].get("values") == [-999]]
    assert sent and sent[0]["enabled"]


def test_empty_columns_dropped_and_recoverable_constant_kept(runs):
    r = runs("empty_cols")
    df = cleaned(r)
    assert "notes" not in df.columns and "legacy_code" not in df.columns and "constant" in df.columns
    rc = audit(r, "removed_columns.csv")
    assert set(rc["legacy_code"]) == {"NULL"}


def test_impossible_values_and_logic(runs):
    r = runs("invalid_logic")
    df = cleaned(r).set_index(ROW_ID)
    assert df.loc["30", "age"] == "" and df.loc["29", "discount_pct"] == ""
    assert df.loc["30", "quantity"] == "-3"  # unusual but possible: flagged, kept
    fl = audit(r, "flagged_records.csv")
    assert (fl.classification == "logical inconsistency").sum() == 3  # 2 end-before-start, 1 total mismatch
    reasons = " ".join(fl.reason)
    assert "before" in reasons and "differs from" in reasons


def test_sensitive_values_are_masked_in_reports(runs):
    r = runs("sensitive")
    with open(os.path.join(r["out"], "reports", "data_audit_report.html"), encoding="utf-8") as fh:
        html = fh.read()
    for secret in ("person10@example.com", "109-45-6009", "Person10 Surname10", "+1 555-0110"):
        assert secret not in html
    prof = read_json(os.path.join(r["out"], "profiling", "initial_profile.json"))
    email = next(c for c in prof["columns_profile"] if c["column"] == "email")
    assert "sensitive" in email["roles"] and all("***" in e for e in email["examples"])
    with open(os.path.join(r["out"], "reports", "exploratory_data_analysis.md"), encoding="utf-8") as fh:
        assert "person1@" not in fh.read()


def test_time_series_interpolation_and_gaps(runs):
    r = runs("timeseries")
    o = ops(r, "impute", "daily_sales")[0]
    assert o["params"]["method"] == "interpolate" and o["params"]["order_by"] == "date"
    ts = r["eda"]["time_series"]
    assert ts["missing_steps"] == 7
    assert any(g["missing_steps"] == 7 for g in ts["gaps"])


def test_train_test_split_prevents_leakage(tmp_path):
    from datamedic.pipeline import run_clean, run_profile
    df = pd.DataFrame({"row": range(100), "split": ["train"] * 80 + ["test"] * 20,
                       "x": [1.0] * 79 + [None] + [1000.0] * 19 + [None]})
    p = tmp_path / "split.csv"
    df.to_csv(p, index=False)
    out = str(tmp_path / "o")
    run_profile(str(p), out)
    run_clean(str(p), os.path.join(out, "plan.json"), out)
    lg = read_json(os.path.join(out, "audit", "transformation_log.json"))["operations"]
    imp = next(e for e in lg if e["op"] == "impute")
    assert imp["params"]["fit_on"]["column"] == "split"
    assert imp["statistical_basis"]["value"] == 1.0  # the test rows (1000) were not used


def test_knn_imputation_runs_when_requested(tmp_path):
    pytest.importorskip("sklearn")
    import json
    from datamedic.pipeline import run_clean, run_profile
    rng = __import__("numpy").random.default_rng(0)
    a = rng.normal(0, 1, 200)
    df = pd.DataFrame({"a": a, "b": a * 2 + rng.normal(0, 0.1, 200)})
    df.loc[:9, "b"] = None
    p = tmp_path / "k.csv"
    df.to_csv(p, index=False)
    out = str(tmp_path / "o")
    run_profile(str(p), out)
    plan = read_json(os.path.join(out, "plan.json"))
    o = next(x for x in plan["operations"] if x["op"] == "impute")
    o["params"] = {"column": "b", "method": "knn", "features": ["a"], "add_indicator": True}
    o["decided_by"] = "agent"
    with open(os.path.join(out, "plan.json"), "w") as fh:
        json.dump(plan, fh)
    run_clean(str(p), os.path.join(out, "plan.json"), out)
    res = read_str_csv(os.path.join(out, "cleaned_data", "cleaned_dataset.csv"))
    filled = pd.to_numeric(res["b"])[:10]
    assert ((filled - 2 * a[:10]).abs() < 0.5).all()


def test_repeated_runs_are_identical(runs, tmp_path):
    from datamedic.pipeline import run_clean
    r = runs("labels")
    out2 = str(tmp_path / "again")
    run_clean(r["src"], os.path.join(r["out"], "plan.json"), out2, write_reference=False, quiet=True)
    a = sha256_file(os.path.join(r["out"], "cleaned_data", "cleaned_dataset.csv"))
    b = sha256_file(os.path.join(out2, "cleaned_data", "cleaned_dataset.csv"))
    assert a == b


EXAMPLE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "examples", "messy_sales.csv")


def test_example_discrete_scales_not_filled_and_emails_are_identifiers(tmp_path):
    from datamedic.pipeline import run_profile
    r = run_profile(EXAMPLE, str(tmp_path / "o"))
    plan = r["plan"]
    imp = {o["params"]["column"]: o for o in plan["operations"] if o["op"] == "impute"}
    assert imp["Rating"]["enabled"] is False and imp["Discount %"]["enabled"] is False
    roles = {c["column"]: c["roles"] for c in r["profile"]["columns_profile"]}
    assert {"identifier", "sensitive"} <= set(roles["Customer Email"])
    assert not [o for o in plan["operations"]
                if o["op"] == "standardize_categories" and o["params"]["column"] == "Customer Email"]
    region = next(o for o in plan["operations"] if o["op"] == "standardize_categories" and o["params"]["column"] == "Region")
    assert region["params"]["mapping"] == {"WEST": "West", "NORTH": "North", "SOUTH": "South", "EAST": "East"}


def test_declined_proposals_are_not_open_decisions():
    from datamedic.report import _declined, _status_counts
    log = [{"op_id": "OP-1", "op": "impute", "status": "not applied (disabled in plan)", "needs_review": True},
           {"op_id": "OP-2", "op": "impute", "status": "not applied (disabled in plan)", "needs_review": False}]
    assert [e["op_id"] for e in _status_counts(log)[2]] == ["OP-1"]
    assert [e["op_id"] for e in _declined(log)] == ["OP-2"]


def test_log_scale_only_for_squashed_positive_data():
    from datamedic.charts import needs_log
    assert needs_log([10, 12, 11, 13, 9, 14, 18000])
    assert not needs_log([10, 12, 11, 13, 9, 14, 30])
    assert not needs_log([-5, 12, 11, 13, 9, 14, 18000])
