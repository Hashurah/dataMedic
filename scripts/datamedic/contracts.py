"""Data quality contracts: rules the cleaned data must meet, with their provenance.

A contract is a JSON file (OUT/contract.json is picked up automatically) with a list of rules. Each
rule says where it came from: "confirmed" (verified business rule), "user" (the user's expectation),
"inferred" (suggested by the data) or "provisional" (an assumption to check). Rules that contradict
each other are reported as conflicts and never resolved silently. Passing a contract shows the data
is consistent with the rules; it does not show the values are accurate.
"""

import math
import os
import re

import pandas as pd

from .clean import _is_missing, _num, condition_mask
from .common import DataMedicError, canon_series, canon_value, norm_label, read_json

PROVENANCE = ("confirmed", "user", "inferred", "provisional")
RULES = ("required_columns", "type", "range", "unique", "allowed", "not_null", "date_range", "pattern", "compare",
         "conditional", "reference", "category_shares", "mean", "missing_share")
# drift rules compare a batch with a saved baseline; their allowed band is z standard errors of the difference
# between two samples unless the rule gives an explicit tolerance
DRIFT_RULES = ("category_shares", "mean", "missing_share")
ON_FAIL = ("warn", "halt")
_OPS = {"<": "lt", "<=": "le", ">": "gt", ">=": "ge", "==": "eq", "!=": "ne"}


def load_contract(path):
    if not os.path.exists(path):
        raise DataMedicError(f"Contract file not found: {path}")
    c = read_json(path)
    rules = c.get("rules")
    if not isinstance(rules, list):
        raise DataMedicError("A contract needs a 'rules' list")
    for i, r in enumerate(rules, 1):
        r.setdefault("id", f"R{i:02d}")
        if r.get("rule") not in RULES:
            raise DataMedicError(f"contract rule {r['id']}: unknown rule '{r.get('rule')}' (one of {', '.join(RULES)})")
        r.setdefault("provenance", "user")
        if r["provenance"] not in PROVENANCE:
            raise DataMedicError(f"contract rule {r['id']}: provenance must be one of {', '.join(PROVENANCE)}")
        r.setdefault("severity", "critical" if r["provenance"] in ("confirmed", "user") else "warning")
        if r.get("on_fail", "warn") not in ON_FAIL:
            raise DataMedicError(f"contract rule {r['id']}: on_fail must be 'warn' or 'halt'")
        if r.get("on_fail") == "halt":
            r["severity"] = "critical"  # a rule that stops the pipeline cannot be a mere warning
    c["_path"] = os.path.abspath(path)
    return c


def conflicts(contract):
    """Pairs of rules that cannot both hold. Reported, not resolved."""
    out = []
    by_col = {}
    for r in contract["rules"]:
        if r.get("column"):
            by_col.setdefault(r["column"], []).append(r)
    for col, rs in by_col.items():
        ranges = [r for r in rs if r["rule"] == "range"]
        for i, a in enumerate(ranges):
            for b in ranges[i + 1:]:
                lo = max(a.get("min", float("-inf")), b.get("min", float("-inf")))
                hi = min(a.get("max", float("inf")), b.get("max", float("inf")))
                if lo > hi:
                    out.append({"rules": [a["id"], b["id"]], "column": col, "problem": "ranges do not overlap"})
        allowed = [r for r in rs if r["rule"] == "allowed"]
        for i, a in enumerate(allowed):
            for b in allowed[i + 1:]:
                if not {canon_value(v) for v in a["values"]} & {canon_value(v) for v in b["values"]}:
                    out.append({"rules": [a["id"], b["id"]], "column": col, "problem": "allowed value lists share nothing"})
        types = {r["type"] for r in rs if r["rule"] == "type"}
        if len(types) > 1:
            out.append({"rules": [r["id"] for r in rs if r["rule"] == "type"], "column": col,
                        "problem": f"declared with different types: {', '.join(sorted(types))}"})
        for a in ranges:
            for b in allowed:
                nums = [float(v) for v in b["values"] if _isnum(v)]
                if nums and len(nums) == len(b["values"]) and not any(
                        a.get("min", float("-inf")) <= v <= a.get("max", float("inf")) for v in nums):
                    out.append({"rules": [a["id"], b["id"]], "column": col, "problem": "no allowed value is inside the range"})
    return out


def _isnum(v):
    try:
        float(v)
        return True
    except (TypeError, ValueError):
        return False


def _date(v):
    return pd.Timestamp.now().normalize() if str(v).lower() == "today" else pd.Timestamp(v)


def evaluate(contract, tv, text, base_dir=None):
    """Check every rule on a typed view `tv` and its text form `text` (canonical strings). One record per rule."""
    res = []
    for r in contract["rules"]:
        rec = {"id": r["id"], "rule": r["rule"], "provenance": r["provenance"], "severity": r["severity"],
               "on_fail": r.get("on_fail", "warn"), "column": r.get("column", ""),
               "description": r.get("description", ""), "source": r.get("source", "")}
        try:
            failing, detail = _check(r, tv, text, base_dir)
        except KeyError as exc:
            failing, detail = None, f"column {exc} not in the data"
        rec["violations"] = failing
        rec["passed"] = failing == 0
        rec["detail"] = detail
        res.append(rec)
    return res


def _col(tv, c):
    if c not in tv.columns:
        raise KeyError(c)
    return tv[c]


def _check(r, tv, text, base_dir):
    k = r["rule"]
    if k == "required_columns":
        missing = [c for c in r["columns"] if c not in tv.columns]
        return len(missing), f"missing: {', '.join(missing)}" if missing else ""
    if k == "type":
        c, t = r["column"], r["type"]
        s, raw = _col(tv, c), text[c]
        present = raw != ""
        if t in ("number", "integer"):
            x = pd.to_numeric(s, errors="coerce")
            bad = present & (x.isna() | ((x % 1 != 0) if t == "integer" else False))
        elif t in ("date", "datetime"):
            bad = present & pd.to_datetime(s, errors="coerce").isna()
        elif t == "boolean":
            bad = present & ~raw.str.lower().isin(["true", "false"])
        else:
            bad = pd.Series(False, index=s.index)
        return int(bad.sum()), f"{int(bad.sum())} values are not {t}"
    if k == "range":
        x = _num(_col(tv, r["column"]))
        bad = ((x < r.get("min", float("-inf"))) | (x > r.get("max", float("inf")))).fillna(False)
        return int(bad.sum()), f"{int(bad.sum())} values outside [{r.get('min', '-inf')}, {r.get('max', 'inf')}]"
    if k == "unique":
        cols = r.get("columns") or [r["column"]]
        for c in cols:
            _col(tv, c)
        key = text[cols]
        keyed = ~(key == "").any(axis=1)
        d = int(key[keyed].duplicated().sum())
        return d, f"{d} repeated {'+'.join(cols)} values"
    if k == "allowed":
        raw = text[_col(tv, r["column"]).name]
        bad = (raw != "") & ~raw.isin([canon_value(v) for v in r["values"]])
        ex = sorted(raw[bad].unique().tolist())[:5]
        return int(bad.sum()), f"{int(bad.sum())} other values" + (f" (e.g. {', '.join(ex)})" if ex else "")
    if k == "not_null":
        s = _col(tv, r["column"])
        miss = int(_is_missing(s).sum())
        allowed = int(r.get("max_missing_share", 0) * len(s))
        return max(0, miss - allowed), f"{miss} missing" + (f" (up to {allowed} allowed)" if allowed else "")
    if k == "date_range":
        d = pd.to_datetime(_col(tv, r["column"]), errors="coerce")
        if getattr(d.dt, "tz", None) is not None:
            d = d.dt.tz_convert(None)
        bad = pd.Series(False, index=d.index)
        if "min" in r:
            bad |= (d < _date(r["min"])).fillna(False)
        if "max" in r:
            bad |= (d > _date(r["max"])).fillna(False)
        return int(bad.sum()), f"{int(bad.sum())} dates outside the allowed period"
    if k == "pattern":
        raw = text[_col(tv, r["column"]).name]
        rx = re.compile(r["regex"])
        bad = (raw != "") & ~raw.map(lambda v: bool(rx.fullmatch(v)))
        return int(bad.sum()), f"{int(bad.sum())} values do not match {r['regex']}"
    if k == "compare":
        a, b = _col(tv, r["left"]), _col(tv, r["right"])
        if pd.api.types.is_datetime64_any_dtype(a) or pd.api.types.is_datetime64_any_dtype(b):
            a, b = pd.to_datetime(a, errors="coerce"), pd.to_datetime(b, errors="coerce")
        else:
            a, b = _num(a), _num(b)
        op = _OPS.get(r["op"], r["op"])
        ok = {"lt": a < b, "le": a <= b, "gt": a > b, "ge": a >= b, "eq": a == b, "ne": a != b}[op]
        both = a.notna() & b.notna()
        bad = both & ~ok.fillna(False)
        return int(bad.sum()), f"{int(bad.sum())} rows where {r['left']} {r['op']} {r['right']} is false"
    if k == "conditional":
        when = condition_mask(tv, r["when"])
        then = condition_mask(tv, r["then"])
        bad = when & ~then
        return int(bad.sum()), f"{int(bad.sum())} of {int(when.sum())} matching rows break the rule"
    if k == "reference":
        path = r["file"] if os.path.isabs(r["file"]) or not base_dir else os.path.join(base_dir, r["file"])
        from .loader import load_table
        ref, _ = load_table(path, sheet=r.get("sheet"), table=r.get("table"))
        keys = set(canon_series(ref[r["key"]]))
        raw = text[_col(tv, r["column"]).name]
        bad = (raw != "") & ~raw.isin(keys)
        return int(bad.sum()), f"{int(bad.sum())} values not found in {os.path.basename(path)}:{r['key']}"
    if k in DRIFT_RULES:
        return _drift(r, tv)
    raise DataMedicError(f"unknown rule {k}")


# ---------------------------------------------------------------- drift against a baseline

def _share_band(r, p0, n0, n1):
    """Allowed absolute difference for a share: the rule's tolerance, else z standard errors of p1 - p0."""
    if r.get("tolerance") is not None:
        return float(r["tolerance"])
    p = min(max(p0, 0.5 / n0), 1 - 0.5 / n0)  # keep a share of 0 or 1 from giving a zero-width band
    return float(r.get("z", 3)) * math.sqrt(p * (1 - p) * (1 / n0 + 1 / max(n1, 1)))


def _pct(x):
    return f"{100 * x:.1f}%"


def _drift(r, tv):
    k, c = r["rule"], r["column"]
    s = _col(tv, c)
    n0 = int(r["n"])
    if k == "category_shares":
        vals = canon_series(s[~_is_missing(s)])
        n1 = len(vals)
        if n1 == 0:
            return 1, "no recorded values to compare with the baseline"
        # levels are matched ignoring case and spacing: a batch whose cleaning chose another spelling of the same
        # label has not drifted, but the spelling change is reported
        names, spelled = {}, []
        for lv in r["shares"]:
            names.setdefault(norm_label(lv), str(lv))
        now = {}
        for lv, v in vals.value_counts(normalize=True).items():
            key = norm_label(lv)
            now[key] = now.get(key, 0.0) + float(v)
            if key in names and names[key] != lv:
                spelled.append(f"{lv} (baseline {names[key]})")
            names.setdefault(key, lv)
        base = {}
        for lv, v in r["shares"].items():
            base[norm_label(lv)] = base.get(norm_label(lv), 0.0) + float(v)
        new_levels = r.get("new_levels", "violation")
        moved = []
        for key in sorted(set(base) | set(now)):
            p0, p1 = base.get(key, 0.0), now.get(key, 0.0)
            if key not in base and new_levels == "ignore":
                continue
            band = _share_band(r, p0, n0, n1)
            if abs(p1 - p0) > band:
                moved.append((abs(p1 - p0) - band, f"{names[key]} {_pct(p0)} to {_pct(p1)}" +
                              (" (new level)" if key not in base else f" (allowed +/-{100 * band:.1f} points)")))
        moved.sort(key=lambda m: -m[0])
        detail = f"{len(moved)} of {len(set(base) | set(now))} levels moved outside the band, n = {n1}"
        detail += ": " + "; ".join(m[1] for m in moved[:5]) if moved else ""
        if spelled:
            detail += f"; spelled differently from the baseline (not counted as drift): {', '.join(spelled[:5])}"
        return len(moved), detail
    if k == "missing_share":
        ind = r.get("indicator")
        if ind and ind in tv.columns:
            miss = tv[ind].astype("boolean").fillna(False) | _is_missing(s)
        else:
            miss = _is_missing(s)
        n1 = len(s)
        p0, p1 = float(r["share"]), float(miss.mean()) if n1 else 0.0
        band = _share_band(r, p0, n0, n1)
        bad = int(abs(p1 - p0) > band)
        return bad, (f"missing {_pct(p1)} against {_pct(p0)} in the baseline "
                     f"(allowed +/-{100 * band:.1f} points, n = {n1})")
    x = _num(s)
    ind = r.get("indicator")
    if ind and ind in tv.columns:
        x = x[~tv[ind].astype("boolean").fillna(False)]
    x = x.dropna()
    n1 = len(x)
    if n1 == 0:
        return 1, "no numeric values to compare with the baseline"
    m0, m1, sd = float(r["mean"]), float(x.mean()), float(r.get("sd") or 0.0)
    if r.get("tolerance") is not None:
        band = float(r["tolerance"])
    elif r.get("relative_tolerance") is not None:
        band = abs(m0) * float(r["relative_tolerance"])
    else:
        band = float(r.get("z", 3)) * sd * math.sqrt(1 / n0 + 1 / n1)
    bad = int(abs(m1 - m0) > band)
    return bad, f"mean {m1:,.4g} against {m0:,.4g} in the baseline (allowed +/-{band:,.4g}, n = {n1})"


def build_baseline(out_dir, z=3.0, halt=False, max_levels=None):
    """A contract describing a cleaned run, for checking later batches: required columns, types, missing shares,
    category shares and means. Every rule is 'inferred'. Drift rules warn by default; `halt` makes them stop the
    pipeline (validation fails and eda refuses to run)."""
    from .clean import read_str_csv
    from .common import load_config, now_iso, read_json
    from .validate import typed_view
    schema = read_json(os.path.join(out_dir, "cleaned_data", "schema.json"))
    plan = read_json(os.path.join(out_dir, "plan.json"))
    roles = plan.get("roles", {})
    raw = read_str_csv(os.path.join(out_dir, "cleaned_data", "cleaned_dataset.csv"))
    tv = typed_view(raw, schema)
    max_levels = max_levels or load_config("default.json")["eda"]["max_group_levels"]
    cols = schema["columns"]
    skip_values = set(roles.get("identifier", [])) | set(roles.get("sensitive", []))
    added = {c for c, i in cols.items() if i.get("added_by_cleaning")}
    indicators = {c[: -len("_was_missing")]: c for c in added if c.endswith("_was_missing")}
    n = len(tv)
    src = f"baseline from {out_dir} ({n} cleaned rows), {now_iso()[:10]}"
    act = "halt" if halt else "warn"
    rules = [{"rule": "required_columns", "columns": [c for c in cols if c not in added]}]
    for c, info in cols.items():
        if c in indicators.values():
            continue  # an imputation indicator is folded into its column's missing share
        if info["type"] in ("number", "integer", "date", "datetime", "boolean"):
            rules.append({"rule": "type", "column": c, "type": info["type"]})
        s = tv[c]
        miss = _is_missing(s)
        if c in indicators:
            miss = miss | tv[indicators[c]].astype("boolean").fillna(False)
        rules.append({"rule": "missing_share", "column": c, "share": round(float(miss.mean()), 6), "n": n, "z": z,
                      "on_fail": act, **({"indicator": indicators[c]} if c in indicators else {})})
        if c in skip_values:
            continue
        rec = s[~_is_missing(s)]
        if info["type"] in ("number", "integer"):
            x = _num(s[~miss]).dropna()  # observed values only: imputed ones would pull the mean to the fill value
            if len(x) >= 5 and x.std() > 0:
                rules.append({"rule": "mean", "column": c, "mean": round(float(x.mean()), 6),
                              "sd": round(float(x.std()), 6), "n": int(len(x)), "z": z, "on_fail": act,
                              **({"indicator": indicators[c]} if c in indicators else {})})
        elif info["type"] in ("string", "boolean"):
            vc = canon_series(rec).value_counts(normalize=True)
            if 2 <= len(vc) <= max_levels:
                rules.append({"rule": "category_shares", "column": c, "n": int(len(rec)), "z": z, "on_fail": act,
                              "shares": {str(k): round(float(v), 6) for k, v in vc.items()}})
    for i, r in enumerate(rules, 1):
        r.update(id=f"B{i:03d}", provenance="inferred", source=src)
    return {"description": "Baseline contract: later batches are compared with this run. Drift bands are z standard "
                           "errors of the difference between two samples unless a rule sets 'tolerance' (absolute) "
                           "or, for means, 'relative_tolerance'.",
            "created": now_iso(), "baseline_rows": n, "z": z, "rules": rules}
