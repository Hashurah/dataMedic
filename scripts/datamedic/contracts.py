"""Data quality contracts: rules the cleaned data must meet, with their provenance.

A contract is a JSON file (OUT/contract.json is picked up automatically) with a list of rules. Each
rule says where it came from: "confirmed" (verified business rule), "user" (the user's expectation),
"inferred" (suggested by the data) or "provisional" (an assumption to check). Rules that contradict
each other are reported as conflicts and never resolved silently. Passing a contract shows the data
is consistent with the rules; it does not show the values are accurate.
"""

import os
import re

import pandas as pd

from .clean import _is_missing, _num, condition_mask
from .common import DataMedicError, canon_series, canon_value, read_json

PROVENANCE = ("confirmed", "user", "inferred", "provisional")
RULES = ("required_columns", "type", "range", "unique", "allowed", "not_null", "date_range", "pattern", "compare",
         "conditional", "reference")
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
    raise DataMedicError(f"unknown rule {k}")
