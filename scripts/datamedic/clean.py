"""Execute a cleaning plan with a complete, reversible audit trail.

Every value change is detected by comparing canonical strings before and after each
operation, so the change log cannot miss a change an operation made. Removed rows and
columns are kept in their original (raw) form. `revert` rebuilds the loaded table from
the cleaned data and the audit files and checks it against the original.
"""

import csv
import os
import re

import numpy as np
import pandas as pd

from .common import (ROW_ID, DataMedicError, canon_frame, canon_series, canon_value, mask_value, now_iso, optional,
                     parse_bool, parse_date, parse_number, read_json, repair_mojibake, write_excel_safe, write_json)

STAGE = {
    "rename_columns": "structure", "drop_rows": "structure", "trim_whitespace": "standardise text",
    "fix_encoding": "standardise text", "standardize_missing": "missing values", "extract_currency": "units",
    "convert_type": "data types", "convert_currency": "units",
    "standardize_categories": "standardise text", "replace_values": "corrections", "set_missing_where": "invalid values",
    "drop_duplicates": "duplicates", "drop_columns": "structure", "apply_rule": "domain rules",
    "quarantine_rows": "row decisions", "impute": "missing values", "winsorize": "outliers", "flag_rows": "review flags",
}
# Fixed stage order. Each stage needs the ones before it: placeholders are normalised before empty-row and type
# checks; types before numeric rules; duplicates are judged on every recorded column, before any column is dropped
# and before rules or imputation fill values; quarantined rows are set aside before imputation statistics are fitted.
# Currency markers are read before type conversion strips them; conversion to one currency runs on the final amounts.
OP_ORDER = ["rename_columns", "drop_rows", "trim_whitespace", "fix_encoding", "standardize_missing", "extract_currency",
            "convert_type", "standardize_categories", "replace_values", "set_missing_where", "drop_duplicates",
            "drop_columns", "apply_rule", "quarantine_rows", "impute", "convert_currency", "winsorize", "flag_rows"]
RISK_RANK = {"safe": 0, "moderate": 1, "high": 2}
DEFAULT_RISK = {
    "rename_columns": "safe", "trim_whitespace": "safe", "standardize_missing": "safe",
    "standardize_categories": "safe", "flag_rows": "safe", "fix_encoding": "moderate", "convert_type": "moderate",
    "replace_values": "moderate", "set_missing_where": "moderate", "drop_duplicates": "moderate", "impute": "moderate",
    "apply_rule": "moderate", "extract_currency": "safe", "convert_currency": "moderate", "drop_rows": "high",
    "drop_columns": "high", "quarantine_rows": "high", "winsorize": "high",
}
# methods whose filled value can depend on observations later in time
FUTURE_METHODS = {"interpolate", "bfill"}
# symbol to ISO code; "$" is also the sign of CAD, AUD and others, so pass `symbols` when the source says so
CURRENCY_SYMBOLS = {"$": "USD", "€": "EUR", "£": "GBP", "¥": "JPY", "₦": "NGN", "₹": "INR", "₩": "KRW", "₽": "RUB"}
_ISO = r"USD|EUR|GBP|NGN|JPY|CAD|AUD|INR|CHF|CNY|KRW|RUB|ZAR|NZD|SEK|NOK|DKK|MXN|BRL"
_CURRENCY_TOKEN = re.compile(rf"^\s*[+-]?\s*([$€£¥₦₹₩₽]|(?:{_ISO})(?![A-Za-z]))"
                             rf"|([$€£¥₦₹₩₽]|(?<![A-Za-z])(?:{_ISO}))\s*$", re.I)


def op_risk(op):
    """Risk level of a plan operation: the plan's own value, else a default by operation type."""
    if op.get("risk") in RISK_RANK:
        return op["risk"]
    if op["op"] == "drop_rows" and op.get("params", {}).get("empty"):
        return "safe"
    return DEFAULT_RISK.get(op["op"], "high")


def _as_list(x):
    return [] if x is None else list(x) if isinstance(x, (list, tuple)) else [x]


def _is_missing(s):
    return s.isna() | s.map(lambda v: isinstance(v, str) and v == "")


def _run_lengths(miss):
    """For a missing-value mask in sequence order, the length of the missing run each position belongs to (0 if present)."""
    m = miss.to_numpy(dtype=bool)
    run = pd.Series(m.astype(int), index=miss.index).groupby(np.cumsum(~m)).transform("sum")
    return run.where(m, 0)


def condition_mask(df, when):
    """Rows matching every condition in a rule's `when` list. A missing value only matches 'missing'."""
    m = pd.Series(True, index=df.index)
    for c in _as_list(when):
        col, how, val = c["column"], c.get("op", "eq"), c.get("value")
        if col not in df.columns:
            raise DataMedicError(f"rule condition refers to column '{col}', which is not in the data")
        s = df[col]
        miss = _is_missing(s)
        cs = canon_series(s)
        if how == "missing":
            hit = miss
        elif how == "not_missing":
            hit = ~miss
        elif how == "eq":
            hit = cs == canon_value(val)
        elif how == "ne":
            hit = ~miss & (cs != canon_value(val))
        elif how == "in":
            hit = cs.isin([canon_value(v) for v in val])
        elif how == "not_in":
            hit = ~miss & ~cs.isin([canon_value(v) for v in val])
        elif how in ("lt", "le", "gt", "ge"):
            x = _num(s)
            hit = {"lt": x < float(val), "le": x <= float(val), "gt": x > float(val), "ge": x >= float(val)}[how].fillna(False)
        else:
            raise DataMedicError(f"unknown condition operator '{how}'")
        m &= hit
    return m


def _num(s):
    if pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s):
        return s.astype("float64")
    return pd.to_numeric(s, errors="coerce")


def rule_mask(df, column, rule, today=None):
    """Boolean mask of rows matching a rule. Missing values never match."""
    today = today or pd.Timestamp.now().normalize()
    if "row_ids" in rule:
        return df[ROW_ID].isin(rule["row_ids"])
    if "date_order" in rule:
        a, b = rule["date_order"]
        sa, sb = pd.to_datetime(df[a], errors="coerce"), pd.to_datetime(df[b], errors="coerce")
        try:
            return (sb < sa).fillna(False)
        except TypeError:
            return (sb.dt.tz_localize(None) < sa.dt.tz_localize(None)).fillna(False)
    if "total_mismatch" in rule:
        q, p, t = rule["total_mismatch"]
        tol = rule.get("tolerance", 0.01)
        expected = _num(df[q]) * _num(df[p])
        tot = _num(df[t])
        diff = (expected - tot).abs()
        return (diff > tol * np.maximum(1.0, tot.abs())).fillna(False)
    if "duplicate_key" in rule:
        cols = rule["duplicate_key"]
        key = canon_frame(df[cols])
        keyed = ~(key == "").any(axis=1)
        return key.duplicated(keep=False) & keyed
    if "values" in rule:
        return canon_series(df[column]).isin([str(v) for v in rule["values"]])
    s = df[column]
    if "future" in rule:
        d = pd.to_datetime(s, errors="coerce")
        if getattr(d.dt, "tz", None) is not None:
            d = d.dt.tz_convert(None)
        return (d > today + pd.Timedelta(days=rule.get("grace_days", 1))).fillna(False)
    if "before" in rule:
        d = pd.to_datetime(s, errors="coerce")
        if getattr(d.dt, "tz", None) is not None:
            d = d.dt.tz_convert(None)
        return (d < pd.Timestamp(rule["before"])).fillna(False)
    x = _num(s)
    if "outlier" in rule:
        method = rule["outlier"]
        k = float(rule.get("k", {"iqr": 1.5, "iqr_log": 1.5, "mad": 3.5, "z": 3.0}.get(method, 1.5)))
        v = x.dropna()
        if len(v) < 4:
            return pd.Series(False, index=df.index)
        if method == "iqr_log":
            pos = v[v > 0]
            if len(pos) < 4 or (v <= 0).any():
                method = "iqr"
            else:
                lv = np.log(pos)
                q1, q3 = lv.quantile(0.25), lv.quantile(0.75)
                lo, hi = np.exp(q1 - k * (q3 - q1)), np.exp(q3 + k * (q3 - q1))
                return ((x < lo) | (x > hi)).fillna(False)
        if method == "iqr":
            q1, q3 = v.quantile(0.25), v.quantile(0.75)
            lo, hi = q1 - k * (q3 - q1), q3 + k * (q3 - q1)
            return ((x < lo) | (x > hi)).fillna(False)
        if method == "mad":
            med = v.median()
            mad = (v - med).abs().median()
            if mad == 0:
                return pd.Series(False, index=df.index)
            return ((0.6745 * (x - med).abs() / mad) > k).fillna(False)
        if method == "z":
            sd = v.std()
            if not sd:
                return pd.Series(False, index=df.index)
            return (((x - v.mean()) / sd).abs() > k).fillna(False)
        raise DataMedicError(f"Unknown outlier method {method}")
    m = pd.Series(False, index=df.index)
    if "lt" in rule:
        m |= (x < float(rule["lt"])).fillna(False)
    if "gt" in rule:
        m |= (x > float(rule["gt"])).fillna(False)
    if "outside" in rule:
        lo, hi = rule["outside"]
        m |= ((x < float(lo)) | (x > float(hi))).fillna(False)
    if "not_integer" in rule:
        m |= ((x % 1) != 0).fillna(False)
    return m


class Engine:
    def __init__(self, raw, plan, sensitive=(), max_risk=None, collect_evidence=True, base_dir=None):
        self.raw = raw
        self.base_dir = base_dir or os.getcwd()  # relative file paths in the plan (rates files) resolve here
        self.df = raw.copy()
        self.plan = plan
        self.seed = plan.get("settings", {}).get("seed", 42)
        self.sensitive = set(sensitive)
        if max_risk is not None and max_risk not in RISK_RANK:
            raise DataMedicError(f"unknown risk level '{max_risk}' (use safe, moderate or high)")
        self.max_risk = max_risk
        self.log, self.changes, self.removed, self.flags = [], [], [], []
        self.evidence = []          # one record per imputed or rule-filled cell
        self.collect_evidence = collect_evidence
        self.rule_cells = {}        # (row id, column) -> (rule id, value) for conflict detection
        self.applied = set()
        self.removed_columns = {}
        self.added_columns, self.renames = [], {}
        self.schema = {}

    # ------------------------------------------------------------ bookkeeping
    def _diff(self, op_id, cols, before):
        counts = {}
        for c in cols:
            if c not in self.df.columns or c not in before:
                continue
            after = canon_series(self.df[c])
            b = before[c]
            mask = b != after
            n = int(mask.sum())
            counts[c] = n
            if n:
                rid = self.df.loc[mask, ROW_ID].tolist()
                for r, o, nw in zip(rid, b[mask].tolist(), after[mask].tolist()):
                    self.changes.append((op_id, int(r), c, o, nw))
        return counts

    def _flag(self, op_id, mask, column, reason, classification, value_col=None, exclude_from=None):
        vc = value_col or column
        rows = self.df.loc[mask]
        vals = canon_series(rows[vc]) if vc in rows.columns else pd.Series("", index=rows.index)
        excl = ", ".join(_as_list(exclude_from))
        for rid, v in zip(rows[ROW_ID].tolist(), vals.tolist()):
            if vc in self.sensitive:
                v = mask_value(v)
            self.flags.append({"flag_id": f"FLAG-{len(self.flags) + 1:05d}", "op_id": op_id, ROW_ID: int(rid),
                               "column": column or "", "value": v, "reason": reason, "classification": classification,
                               "exclude_from": excl})
        return int(mask.sum())

    # ------------------------------------------------------------ run
    def run(self):
        ops = self.plan.get("operations", [])
        order = {k: i for i, k in enumerate(OP_ORDER)}
        ordered = sorted(ops, key=lambda o: (order.get(o["op"], 99), ops.index(o)))
        pos = {o["op_id"]: i for i, o in enumerate(ordered)}
        for op in ordered:
            risk = op_risk(op)
            entry = {"op_id": op["op_id"], "timestamp": now_iso(), "stage": STAGE.get(op["op"], "other"),
                     "op": op["op"], "column": op.get("params", {}).get("column") or
                     ", ".join(op.get("params", {}).get("columns", []) or []),
                     "issue": op.get("issue", ""), "rationale": op.get("rationale", ""),
                     "evidence": op.get("evidence", {}), "params": op.get("params", {}),
                     "decided_by": op.get("decided_by", "auto"), "needs_review": op.get("needs_review", False),
                     "risk": risk, "execution_position": pos[op["op_id"]] + 1,
                     **({"depends_on": op["depends_on"]} if op.get("depends_on") else {})}
            if not op.get("enabled", True):
                entry.update(status="not applied (disabled in plan)", affected=0, reversible=True,
                             reversal="nothing to undo")
                self.log.append(entry)
                continue
            deps = _as_list(op.get("depends_on"))
            late = [d for d in deps if pos.get(d, -1) > pos[op["op_id"]]]
            unknown = [d for d in deps if d not in pos]
            if late or unknown:
                why = (f"depends on {', '.join(late)}, which runs later in the stage order" if late
                       else f"depends on {', '.join(unknown)}, which is not in the plan")
                entry.update(status="failed", error=why, affected=0, reversible=True, reversal="nothing applied")
                self.log.append(entry)
                continue
            waiting = [d for d in deps if d not in self.applied]
            if waiting:
                entry.update(status=f"not applied (depends on {', '.join(waiting)}, which was not applied)", affected=0,
                             reversible=True, reversal="nothing to undo")
                self.log.append(entry)
                continue
            if self.max_risk and RISK_RANK[risk] > RISK_RANK[self.max_risk]:
                entry.update(status=f"not applied (risk '{risk}' is above this run's limit '{self.max_risk}')",
                             affected=0, reversible=True, reversal="nothing to undo")
                self.log.append(entry)
                continue
            fn = getattr(self, "op_" + op["op"], None)
            if fn is None:
                raise DataMedicError(f"{op['op_id']}: unknown operation '{op['op']}'")
            try:
                result = fn(op["op_id"], **op.get("params", {}))
            except DataMedicError:
                raise
            except Exception as exc:  # report the failing operation, keep the run honest
                entry.update(status="failed", error=f"{type(exc).__name__}: {exc}", affected=0, reversible=True,
                             reversal="nothing applied")
                self.log.append(entry)
                continue
            entry.update(result)
            entry.setdefault("status", "applied" if entry.get("affected") else "applied (no values changed)")
            entry.setdefault("reversible", True)
            self.applied.add(op["op_id"])
            self.log.append(entry)
        return self

    # ------------------------------------------------------------ operations
    def op_rename_columns(self, op_id, mapping):
        missing = [k for k in mapping if k not in self.df.columns]
        if missing:
            raise DataMedicError(f"{op_id}: columns not found: {missing}")
        self.df = self.df.rename(columns=mapping)
        self.renames.update(mapping)
        for old, new in mapping.items():
            if old in self.sensitive:
                self.sensitive.add(new)
        return {"affected": len(mapping), "method": "rename", "details": mapping,
                "reversal": "rename back using the mapping"}

    def op_drop_rows(self, op_id, reason, rule=None, row_ids=None, empty=False):
        if empty:
            body = canon_frame(self.df.drop(columns=[ROW_ID]))
            mask = (body.apply(lambda s: s.str.strip()) == "").all(axis=1)
        else:
            mask = rule_mask(self.df, (rule or {}).get("column"), rule or {"row_ids": row_ids or []})
        ids = self.df.loc[mask, ROW_ID].astype(int).tolist()
        for r in ids:
            self.removed.append({"op_id": op_id, ROW_ID: r, "reason": reason, "retained_row_id": "",
                                 "dm_disposition": "removed", "dm_differing_columns": ""})
        self.df = self.df.loc[~mask].copy()
        return {"affected": len(ids), "method": "remove rows", "row_count_change": -len(ids),
                "reversal": "rows are stored with original values in audit/removed_records.csv"}

    def op_quarantine_rows(self, op_id, reason, rule, column=None):
        """Set rows aside: out of the trusted cleaned data, kept whole in audit/quarantined_records.csv."""
        mask = rule_mask(self.df, column or rule.get("column"), rule)
        ids = self.df.loc[mask, ROW_ID].astype(int).tolist()
        for r in ids:
            self.removed.append({"op_id": op_id, ROW_ID: r, "reason": reason, "retained_row_id": "",
                                 "dm_disposition": "quarantined", "dm_differing_columns": ""})
        self.df = self.df.loc[~mask].copy()
        return {"affected": len(ids), "method": "quarantine rows (kept outside the cleaned data until resolved)",
                "row_count_change": -len(ids),
                "reversal": "rows are stored with original values in audit/quarantined_records.csv"}

    def op_fix_encoding(self, op_id, columns):
        """Repair text garbled by a wrong encoding (UTF-8 read as Windows-1252/Latin-1), only where the repair is exact."""
        cols = self._str_cols(columns)
        before = {c: canon_series(self.df[c]) for c in cols}
        for c in cols:
            self.df[c] = self.df[c].map(lambda v: repair_mojibake(v) if isinstance(v, str) else v)
        counts = self._diff(op_id, cols, before)
        return {"affected": sum(counts.values()), "details": counts,
                "method": "re-decode UTF-8 text that was read as Windows-1252 (only exact round trips)",
                "reversal": "original values in audit/cell_changes.csv"}

    def op_apply_rule(self, op_id, rule_id, when, set, source, only_if_missing=True, description=""):
        """Set values from a documented business rule. Conflicts with an earlier rule on the same cell fail the step."""
        if not source:
            raise DataMedicError(f"{op_id}: rule {rule_id} has no 'source'; a rule needs documented provenance")
        mask = condition_mask(self.df, when)
        cols = list(set)
        missing_cols = [c for c in cols if c not in self.df.columns]
        if missing_cols:
            raise DataMedicError(f"{op_id}: rule {rule_id} sets columns not in the data: {missing_cols}")
        targets = {}
        conflicts = []
        for c, v in set.items():
            t = mask & _is_missing(self.df[c]) if only_if_missing else mask
            targets[c] = t
            cv = canon_value(v)
            for rid in self.df.loc[t, ROW_ID].astype(int):
                prev = self.rule_cells.get((rid, c))
                if prev and prev[1] != cv:
                    conflicts.append(f"row {rid} {c}: {prev[0]} set '{prev[1]}', {rule_id} wants '{cv}'")
        if conflicts:
            raise ValueError(f"rule {rule_id} conflicts with an earlier rule on {len(conflicts)} cells; resolve the "
                             f"rules in the plan (first: {conflicts[0]})")
        before = {c: canon_series(self.df[c]) for c in cols}
        for c, v in set.items():
            t = targets[c]
            s = self.df[c]
            if pd.api.types.is_bool_dtype(s) or str(s.dtype) == "boolean":
                val = parse_bool(str(v))
            elif pd.api.types.is_numeric_dtype(s):
                val = float(v)
            elif pd.api.types.is_datetime64_any_dtype(s):
                val = pd.Timestamp(v)
            else:
                val = str(v)
            col = s.copy()
            col[t] = val
            self.df[c] = col
            for rid in self.df.loc[t, ROW_ID].astype(int):
                self.rule_cells[(rid, c)] = (rule_id, canon_value(v))
                self.evidence.append({"op_id": op_id, ROW_ID: int(rid), "column": c, "method": "domain rule",
                                      "imputed_value": canon_value(v), "support_n": "", "group": "", "group_n": "",
                                      "dispersion": "", "gap_length": "", "uses_future_values": False,
                                      "fallback_used": False, "source": f"{rule_id}: {source}"})
        counts = self._diff(op_id, cols, before)
        return {"affected": sum(counts.values()), "details": {"rule_id": rule_id, "rows_matching": int(mask.sum()),
                                                              "cells_set": counts, "only_if_missing": only_if_missing},
                "method": f"business rule {rule_id}", "statistical_basis": {"source": source, "description": description},
                "reversal": "original values in audit/cell_changes.csv"}

    def _str_cols(self, columns):
        return [c for c in columns if c in self.df.columns and self.df[c].dtype == object]

    def op_trim_whitespace(self, op_id, columns, collapse_internal=False):
        cols = self._str_cols(columns)
        before = {c: canon_series(self.df[c]) for c in cols}
        for c in cols:
            s = self.df[c]
            out = s.map(lambda v: v.strip() if isinstance(v, str) else v)
            if collapse_internal:
                out = out.map(lambda v: " ".join(v.split()) if isinstance(v, str) else v)
            self.df[c] = out
        counts = self._diff(op_id, cols, before)
        return {"affected": sum(counts.values()), "details": counts, "method": "strip leading/trailing spaces"
                + (" and collapse repeated spaces" if collapse_internal else ""),
                "reversal": "original values in audit/cell_changes.csv"}

    def op_standardize_missing(self, op_id, column, tokens):
        before = {column: canon_series(self.df[column])}
        toks = set(tokens)
        self.df[column] = self.df[column].map(lambda v: None if isinstance(v, str) and v.strip() in toks else v)
        counts = self._diff(op_id, [column], before)
        return {"affected": counts.get(column, 0), "method": "placeholder to missing", "details": {"tokens": tokens},
                "reversal": "original placeholders in audit/cell_changes.csv"}

    def op_extract_currency(self, op_id, column, to_column=None, symbols=None, unmarked="Unmarked"):
        """Copy the currency marker of each amount into its own column, so the amount can become a number without
        losing it. The amount column is not changed here. Values with no marker get `unmarked`; missing amounts stay
        missing in both columns."""
        to_column = to_column or f"{column}_currency"
        if to_column in self.df.columns:
            raise DataMedicError(f"{op_id}: column {to_column} already exists; choose another to_column")
        table = {**CURRENCY_SYMBOLS, **(symbols or {})}

        def code(v):
            if v is None or (isinstance(v, float) and np.isnan(v)) or (isinstance(v, str) and not v.strip()):
                return None
            m = _CURRENCY_TOKEN.search(str(v))
            if not m:
                return unmarked
            tok = (m.group(1) or m.group(2)).strip()
            return table.get(tok, table.get(tok.upper(), tok.upper()))

        out = self.df[column].map(code)
        self.df.insert(list(self.df.columns).index(column) + 1, to_column, out)
        self.added_columns.append(to_column)
        self.schema[to_column] = {"type": "string", "derived_from": column}
        counts = {str(k): int(v) for k, v in out.value_counts().items()}
        return {"affected": int(out.notna().sum()), "method": "currency marker copied to its own column",
                "details": {"column_added": to_column, "counts": counts, "symbol_table": table},
                "added_columns": [to_column], "status": f"applied: {to_column} added",
                "reversal": f"drop column {to_column}; the amount column was not changed by this step"}

    def op_convert_type(self, op_id, column, to, decimal=".", date_order=None, allow_compact=False,
                        on_error="set_missing", unit=None):
        s = self.df[column]
        before = {column: canon_series(s)}
        present = ~_is_missing(s)
        if to in ("number", "integer"):
            parsed = s.map(lambda v: parse_number(v, decimal) if isinstance(v, str) else
                           (float(v) if v is not None and not pd.isna(v) else None))
            vals = pd.to_numeric(parsed, errors="coerce").astype("float64")
        elif to in ("date", "datetime"):
            parsed = s.map(lambda v: parse_date(v, date_order, allow_compact) if isinstance(v, str) else v)
            aware = parsed.map(lambda d: d is not None and getattr(d, "tzinfo", None) is not None)
            naive = parsed.map(lambda d: d is not None and getattr(d, "tzinfo", None) is None)
            if aware.any() and naive.any():
                raise DataMedicError(f"{op_id}: column '{column}' mixes values with and without time zones. "
                                     "Decide how naive values should be interpreted before converting.")
            vals = pd.to_datetime(parsed, errors="coerce", utc=bool(aware.any()))
        elif to == "boolean":
            vals = s.map(lambda v: parse_bool(v) if isinstance(v, str) else v).astype("boolean")
        elif to == "string":
            vals = s.map(lambda v: None if v is None or (not isinstance(v, str) and pd.isna(v)) else str(v))
        else:
            raise DataMedicError(f"{op_id}: unknown target type '{to}'")
        failed = present & vals.isna()
        n_fail = int(failed.sum())
        if n_fail and on_error == "abort":
            raise DataMedicError(f"{op_id}: {n_fail} values in '{column}' could not be converted to {to}")
        if n_fail:
            self._flag(op_id, failed, column, f"could not be read as {to}; set to missing (original kept in the change log)",
                       "conversion failure", value_col=column)
        if to == "integer":
            if (vals.dropna() % 1 != 0).any():
                to = "number"
            else:
                vals = vals.astype("Int64")
        self.df[column] = vals
        start = len(self.changes)
        counts = self._diff(op_id, [column], before)
        fmt_only = 0
        if to in ("number", "integer"):
            for _, _, _, o, nw in self.changes[start:]:
                try:
                    fmt_only += nw != "" and float(o.strip()) == float(nw)
                except ValueError:
                    pass
        self.schema[column] = {"type": to, **({"unit": unit} if unit else {}),
                               **({"date_order": date_order} if date_order else {})}
        return {"affected": counts.get(column, 0), "method": f"convert to {to}",
                "details": {"failed_conversions": n_fail, "format_only_changes": fmt_only, "decimal": decimal,
                            "date_order": date_order},
                "reversal": "original text values in audit/cell_changes.csv",
                **({"status": f"applied with {n_fail} failed conversions flagged"} if n_fail else {})}

    def _map_values(self, op_id, column, mapping, method):
        before = {column: canon_series(self.df[column])}
        m = {str(k): v for k, v in mapping.items()}
        cs = before[column]
        hit = cs.isin(list(m)) & (cs != "")
        col = self.df[column].astype(object).copy()
        col[hit] = cs[hit].map(m)
        self.df[column] = col if self.df[column].dtype == object else col.astype(self.df[column].dtype)
        counts = self._diff(op_id, [column], before)
        return {"affected": counts.get(column, 0), "method": method, "details": {"mapping": mapping},
                "reversal": "original values in audit/cell_changes.csv"}

    def op_standardize_categories(self, op_id, column, mapping):
        return self._map_values(op_id, column, mapping, "merge label variants into one spelling")

    def op_replace_values(self, op_id, column, mapping, reason=""):
        return self._map_values(op_id, column, mapping, "replace values (" + (reason or "correction") + ")")

    def op_set_missing_where(self, op_id, column, rule, reason, classification="confirmed invalid"):
        mask = rule_mask(self.df, column, rule)
        self._flag(op_id, mask, column, reason + "; value set to missing", classification)
        before = {column: canon_series(self.df[column])}
        col = self.df[column].copy()
        col[mask] = None if col.dtype == object else np.nan
        if str(col.dtype) == "Int64":
            col[mask] = pd.NA
        self.df[column] = col
        counts = self._diff(op_id, [column], before)
        return {"affected": counts.get(column, 0), "method": "set invalid values to missing",
                "details": {"rule": rule}, "reversal": "original values in audit/cell_changes.csv"}

    def op_drop_columns(self, op_id, columns, reason):
        cols = [c for c in columns if c in self.df.columns]
        for c in cols:
            self.removed_columns[c] = op_id
        self.df = self.df.drop(columns=cols)
        return {"affected": len(cols), "method": "remove columns", "details": {"columns": cols, "reason": reason},
                "column_count_change": -len(cols),
                "reversal": "original column values in audit/removed_columns.csv"}

    def op_drop_duplicates(self, op_id, reason, subset=None, keep="first", ignore_case=False, order_by=None):
        """Remove repeated records. `subset` is a business key; rows that share it but differ elsewhere are
        conflicting duplicates, and which one is kept follows `keep` (first, last, most_complete, latest, earliest)."""
        cols = subset or [c for c in self.df.columns if c != ROW_ID]
        missing_cols = [c for c in cols if c not in self.df.columns]
        if missing_cols:
            raise DataMedicError(f"{op_id}: duplicate key columns not in the data: {missing_cols}")
        key = canon_frame(self.df[cols])
        if ignore_case:
            key = key.apply(lambda s: s.str.casefold())
        if keep in ("most_complete", "latest", "earliest"):
            if keep == "most_complete":
                filled = (canon_frame(self.df.drop(columns=[ROW_ID])) != "").sum(axis=1)
                order = filled.sort_values(ascending=False, kind="stable").index
            else:
                if not order_by or order_by not in self.df.columns:
                    raise DataMedicError(f"{op_id}: keep='{keep}' needs order_by (a date or sequence column)")
                t = self.df[order_by]
                t = t if pd.api.types.is_datetime64_any_dtype(t) else _num(t)
                order = t.sort_values(ascending=(keep == "earliest"), kind="stable", na_position="last").index
            dup = key.loc[order].duplicated(keep="first").reindex(self.df.index)
        else:
            dup = key.duplicated(keep=keep)
        groups = key.astype(str).agg("\x1f".join, axis=1)
        kept = {}
        for idx in self.df.index[~dup]:
            kept.setdefault(groups[idx], idx)
        body = canon_frame(self.df.drop(columns=[ROW_ID]))
        ids, conflicting = [], 0
        for idx in self.df.index[dup]:
            r = int(self.df.at[idx, ROW_ID])
            ids.append(r)
            k = kept.get(groups[idx])
            differ = [c for c in body.columns if k is not None and body.at[idx, c] != body.at[k, c]]
            conflicting += bool(differ)
            self.removed.append({"op_id": op_id, ROW_ID: r, "reason": reason,
                                 "retained_row_id": int(self.df.at[k, ROW_ID]) if k is not None else "",
                                 "dm_disposition": "removed", "dm_differing_columns": ", ".join(differ)})
        self.df = self.df.loc[~dup].copy()
        return {"affected": len(ids), "method": f"remove duplicates (keep {keep})",
                "details": {"columns_compared": "all" if subset is None else cols, "ignore_case": ignore_case,
                            "removed_rows_that_differ_from_the_kept_row": conflicting,
                            **({"order_by": order_by} if order_by else {})},
                "row_count_change": -len(ids),
                "reversal": "removed rows with original values in audit/removed_records.csv"}

    def op_impute(self, op_id, column, method, value=None, group_by=None, add_indicator=True, order_by=None,
                  features=None, fit_on=None, limit=None, n_neighbors=5, source=None, mapping=None, match="exact",
                  max_gap=None, max_gap_duration=None):
        s = self.df[column]
        miss = _is_missing(s)
        n_miss = int(miss.sum())
        before = {column: canon_series(s)}
        fit_rows = pd.Series(True, index=self.df.index)
        if fit_on:
            fit_rows = canon_series(self.df[fit_on["column"]]).isin([str(v) for v in fit_on["values"]])
        numeric = pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s)
        stats_before = _dist(s) if numeric else None
        basis = {}
        out = s.copy()
        if n_miss == 0:
            return {"affected": 0, "method": method, "status": "applied (no missing values)", "reversal": "nothing to undo"}
        # per-cell evidence, filled in by each method; index = rows that were missing
        ev = pd.DataFrame(index=self.df.index[miss], columns=["support_n", "group", "group_n", "dispersion",
                                                              "gap_length", "fallback_used"], dtype=object)
        ev["fallback_used"] = False
        if method in ("mean", "median", "mode", "constant"):
            fit = s[fit_rows & ~miss]
            if method == "mean":
                v = float(_num(fit).mean())
            elif method == "median":
                v = float(_num(fit).median())
            elif method == "mode":
                m = fit.mode(dropna=True)
                if not len(m):
                    raise DataMedicError(f"{op_id}: no values to take a mode from")
                v = m.iloc[0]
                basis["mode_frequency"] = int((fit == v).sum())
                basis["tied_modes"] = int(len(m))
            else:
                v = value
            if str(s.dtype) == "Int64" and v is not None and not float(v).is_integer():
                basis["rounded_from"] = v
                v = int(round(v))
            out = s.copy()
            out[miss] = v
            basis.update(value=v, fitted_on_rows=int((fit_rows & ~miss).sum()))
            if method != "constant":
                ev["support_n"] = basis["fitted_on_rows"]
                ev["dispersion"] = _spread(fit) if numeric else _share(fit, v)
        elif method in ("group_median", "group_mean", "group_mode"):
            if not group_by:
                raise DataMedicError(f"{op_id}: {method} needs group_by")
            gcols = _as_list(group_by)
            fitdf = self.df[fit_rows & ~miss]
            agg = {"group_median": "median", "group_mean": "mean"}.get(method)
            if agg:
                table = fitdf.groupby(gcols, dropna=False)[column].agg(agg)
                overall = float(getattr(_num(fitdf[column]), agg)())
            else:
                table = fitdf.groupby(gcols, dropna=False)[column].agg(lambda x: x.mode().iloc[0] if len(x.mode()) else None)
                m = fitdf[column].mode()
                overall = m.iloc[0] if len(m) else None
            sizes = fitdf.groupby(gcols, dropna=False)[column].count().to_dict()
            spread = fitdf.groupby(gcols, dropna=False)[column].agg(_spread if numeric else
                                                                    (lambda x: _share(x, x.mode().iloc[0]) if len(x.mode()) else "")).to_dict()
            keys = self.df[gcols].apply(tuple, axis=1) if len(gcols) > 1 else self.df[gcols[0]]
            lookup = table.to_dict()
            fills = keys.map(lambda k: lookup.get(k, None))
            fallback = miss & fills.isna()
            fills = fills.where(~fills.isna(), overall)
            if str(s.dtype) == "Int64":
                fills = fills.map(lambda x: None if x is None or pd.isna(x) else int(round(float(x))))
            out = s.copy()
            out[miss] = fills[miss]
            basis.update(groups=len(lookup), group_values={str(k): v for k, v in list(lookup.items())[:50]},
                         fallback_value=overall, fallback_rows=int(fallback.sum()))
            k_miss = keys[miss]
            ev["group"] = k_miss.map(str)
            ev["group_n"] = k_miss.map(lambda k: sizes.get(k, 0))
            ev["support_n"] = ev["group_n"]
            ev["dispersion"] = k_miss.map(lambda k: spread.get(k, ""))
            ev["fallback_used"] = fallback[miss]
            ev.loc[ev["fallback_used"].astype(bool), "support_n"] = int(len(fitdf))
        elif method in ("interpolate", "ffill", "bfill"):
            if not order_by:
                raise DataMedicError(f"{op_id}: {method} needs order_by (a date or sequence column)")
            gcols = _as_list(group_by)
            order = self.df.sort_values(gcols + [order_by, ROW_ID], kind="stable").index
            res = pd.Series(np.nan, index=self.df.index, dtype="float64" if method == "interpolate" else object)
            runs = pd.Series(0, index=self.df.index)
            span = pd.Series(pd.NaT, index=self.df.index, dtype="timedelta64[ns]")
            dup_ts = 0
            seqs = (self.df.loc[order].groupby(gcols, sort=False, dropna=False).indices.values() if gcols
                    else [np.arange(len(order))])
            for posn in seqs:
                idx = order[posn]
                t = self.df.loc[idx, order_by]
                is_time = pd.api.types.is_datetime64_any_dtype(t)
                dup_ts += int(t.duplicated().sum())
                ordered = _num(s.loc[idx]) if method == "interpolate" else s.loc[idx]
                if method == "interpolate":
                    if is_time:
                        tmp = pd.Series(ordered.values, index=pd.DatetimeIndex(t.values))
                        filled = tmp.interpolate(method="time", limit_area="inside", limit=limit).values
                    else:
                        filled = ordered.interpolate(method="linear", limit_area="inside", limit=limit).values
                    r = pd.Series(filled, index=idx)
                elif method == "ffill":
                    r = ordered.ffill(limit=limit)
                else:
                    r = ordered.bfill(limit=limit)
                res.loc[idx] = r.values
                runs.loc[idx] = _run_lengths(miss.loc[idx]).values
                if is_time:
                    obs_t = t.where(~miss.loc[idx])
                    prev_t, next_t = obs_t.ffill(), obs_t.bfill()
                    sp = {"interpolate": next_t - prev_t, "ffill": t - prev_t, "bfill": next_t - t}[method]
                    span.loc[idx] = sp.values
            withheld = pd.Series(False, index=self.df.index)
            if max_gap is not None:
                withheld |= miss & (runs > int(max_gap))
            if max_gap_duration:
                withheld |= miss & (span > pd.Timedelta(max_gap_duration)).fillna(False)
            res[withheld] = np.nan if method == "interpolate" else None
            if str(s.dtype) == "Int64":
                res = pd.to_numeric(res, errors="coerce").round().astype("Int64")
            out = s.copy()
            out[miss] = res[miss]
            basis.update(order_by=order_by, limit=limit, group_by=gcols or None, max_gap=max_gap,
                         max_gap_duration=max_gap_duration, uses_future_values=method in FUTURE_METHODS,
                         left_missing_because_gap_too_long=int(withheld.sum()), duplicate_timestamps=dup_ts,
                         note="no extrapolation beyond the first/last observed value" if method == "interpolate" else "")
            if dup_ts:
                basis["warning"] = (f"{dup_ts} repeated values of {order_by} within a series; neighbours at the same "
                                    "time are ordered by row id, which is arbitrary")
            ev["gap_length"] = runs[miss]
            ev["support_n"] = 2 if method == "interpolate" else 1
            ev["dispersion"] = span[miss].map(lambda d: "" if pd.isna(d) else f"bracketing span {d}")
            if gcols:
                gk = self.df[gcols].astype(str).agg(" / ".join, axis=1)
                ev["group"] = gk[miss]
        elif method == "lookup":
            if source not in self.df.columns or not mapping:
                raise DataMedicError(f"{op_id}: lookup needs an existing 'source' column and a 'mapping'")
            keys = canon_series(self.df[source])
            if match == "first_word":
                keys = keys.str.split().str[0].fillna("")
            fills = keys.map(lambda k: mapping.get(k))
            hit = miss & fills.notna()
            out = s.copy()
            out[hit] = fills[hit]
            basis.update(source=source, match=match, filled=int(hit.sum()), left_missing=int((miss & ~hit).sum()))
            ev["group"] = keys[miss].map(lambda k: f"{source}={k}")
        elif method in ("knn", "iterative"):
            sk = optional("sklearn")
            if sk is None:
                raise DataMedicError(f"{op_id}: {method} imputation needs scikit-learn (pip install scikit-learn)")
            feats = [f for f in (features or []) if f in self.df.columns and f != column]
            if not feats:
                raise DataMedicError(f"{op_id}: {method} imputation needs numeric 'features'")
            mat = pd.concat([_num(self.df[column])] + [_num(self.df[f]) for f in feats], axis=1)
            mean, sd = mat[fit_rows].mean(), mat[fit_rows].std().replace(0, 1)
            z = (mat - mean) / sd
            if method == "knn":
                from sklearn.impute import KNNImputer
                imp = KNNImputer(n_neighbors=n_neighbors)
            else:
                from sklearn.experimental import enable_iterative_imputer  # noqa: F401
                from sklearn.impute import IterativeImputer
                imp = IterativeImputer(random_state=self.seed, max_iter=10)
            imp.fit(z[fit_rows].values)
            filled = imp.transform(z.values)[:, 0] * sd.iloc[0] + mean.iloc[0]
            res = pd.Series(filled, index=self.df.index)
            if str(s.dtype) == "Int64":
                res = res.round().astype("Int64")
            out = s.copy()
            out[miss] = res[miss]
            basis.update(features=feats, standardised=True, seed=self.seed,
                         **({"n_neighbors": n_neighbors} if method == "knn" else {}))
            ev["support_n"] = n_neighbors if method == "knn" else int((fit_rows & ~miss).sum())
        else:
            raise DataMedicError(f"{op_id}: unknown imputation method '{method}'")
        self.df[column] = out
        if add_indicator:
            ind = f"{column}_was_missing"
            k = 2
            while ind in self.df.columns:
                ind = f"{column}_was_missing_{k}"
                k += 1
            self.df[ind] = miss.astype(bool)
            self.added_columns.append(ind)
            basis["indicator_column"] = ind
        counts = self._diff(op_id, [column], before)
        if numeric:
            basis["distribution_before"] = stats_before
            basis["distribution_after"] = _dist(self.df[column])
        if fit_on:
            basis["fit_on"] = fit_on
        if source and method != "lookup":
            basis["source"] = source
        if self.collect_evidence:
            filled_now = miss & ~_is_missing(self.df[column])
            rec = ev.loc[self.df.index[filled_now]].copy()
            for k in ("support_n", "group", "group_n", "dispersion"):
                rec[k] = rec[k].astype(object).where(rec[k].notna(), "")
            rec["gap_length"] = rec["gap_length"].map(lambda g: "" if pd.isna(g) else int(g))
            rec["fallback_used"] = rec["fallback_used"].astype(bool)
            rec.insert(0, "imputed_value", canon_series(self.df.loc[filled_now, column]).values)
            rec.insert(0, "method", method)
            rec.insert(0, "column", column)
            rec.insert(0, ROW_ID, self.df.loc[filled_now, ROW_ID].astype(int).values)
            rec.insert(0, "op_id", op_id)
            rec["uses_future_values"] = method in FUTURE_METHODS
            rec["source"] = (source or "") if method != "lookup" else f"mapping on {source}"
            self.evidence.extend(rec.to_dict("records"))
        still = int(_is_missing(self.df[column]).sum())
        return {"affected": counts.get(column, 0), "method": f"{method} imputation", "statistical_basis": basis,
                "details": {"missing_before": n_miss, "missing_after": still},
                "reversal": "imputed cells listed with original (empty) values in audit/cell_changes.csv"
                + (f"; indicator column {basis.get('indicator_column')} marks them" if add_indicator else "")}

    def op_convert_currency(self, op_id, column, currency_column, rates_file, target, date_column=None,
                            output_column=None, decimals=2):
        """Convert amounts to one currency with rates the user supplies, into new columns. The amount and currency
        columns are kept as they are. `rates_file` is a CSV with `currency` and `rate` (units of `target` per one
        unit of `currency`) and, optionally, `date`: then each row uses the latest rate on or before its
        `date_column` value. Rows with no matching rate are left unconverted and flagged, never guessed."""
        if not target:
            raise ValueError("set params.target to the reporting currency (for example 'USD')")
        path = rates_file if os.path.isabs(rates_file) else os.path.join(self.base_dir, rates_file)
        if not os.path.exists(path):
            raise ValueError(f"rates file not found: {path}")
        from .common import sha256_file
        rates = pd.read_csv(path, dtype=str, keep_default_na=False, encoding="utf-8")
        rates.columns = [c.strip().lower() for c in rates.columns]
        if not {"currency", "rate"} <= set(rates.columns):
            raise ValueError("the rates file needs 'currency' and 'rate' columns")
        rates["currency"] = rates["currency"].str.strip().str.upper()
        rates["rate"] = pd.to_numeric(rates["rate"], errors="coerce")
        if rates["rate"].isna().any() or (rates["rate"] <= 0).any():
            raise ValueError("every rate must be a positive number")
        dated = "date" in rates.columns
        amt = _num(self.df[column])
        if amt.notna().sum() == 0 and (~_is_missing(self.df[column])).any():
            raise ValueError(f"{column} is not numeric; enable its convert_type step first")
        cur = self.df[currency_column].map(
            lambda v: str(v).strip().upper() if isinstance(v, str) and v.strip() else None)
        rate = pd.Series(np.nan, index=self.df.index)
        rate_date = pd.Series(pd.NaT, index=self.df.index, dtype="datetime64[ns]")
        tgt = str(target).strip().upper()
        if dated:
            if not date_column:
                raise ValueError("the rates file has dates; set params.date_column to the transaction date column")
            rates["date"] = pd.to_datetime(rates["date"], errors="coerce")
            if rates["date"].isna().any():
                raise ValueError("every rate needs a readable date (YYYY-MM-DD)")
            when = pd.to_datetime(self.df[date_column], errors="coerce")
            if getattr(when.dt, "tz", None) is not None:
                when = when.dt.tz_convert(None)
            for c, g in rates.sort_values("date").groupby("currency"):
                rows = (cur == c) & when.notna()
                if not rows.any():
                    continue
                left = pd.DataFrame({"when": when[rows].astype("datetime64[ns]").values,
                                     "i": self.df.index[rows]}).sort_values("when")
                right = g[["date", "rate"]].astype({"date": "datetime64[ns]"})
                m = pd.merge_asof(left, right, left_on="when", right_on="date", direction="backward")
                rate.loc[m["i"].values] = m["rate"].values
                rate_date.loc[m["i"].values] = m["date"].values
        else:
            dup = rates["currency"][rates["currency"].duplicated()].unique().tolist()
            if dup:
                raise ValueError(f"several rates for {', '.join(dup)} but no 'date' column to choose between them")
            rate = cur.map(dict(zip(rates["currency"], rates["rate"]))).astype(float)
        rate[cur == tgt] = 1.0
        out_col = output_column or f"{column}_{tgt}"
        rate_col = f"{column}_fx_rate"
        added = [out_col, rate_col] + ([f"{column}_fx_rate_date"] if dated else [])
        clash = [c for c in added if c in self.df.columns]
        if clash:
            raise ValueError(f"columns already exist: {', '.join(clash)}; set output_column")
        converted = (amt * rate).round(decimals)
        pos = list(self.df.columns).index(column) + 1
        self.df.insert(pos, out_col, converted)
        self.df.insert(pos + 1, rate_col, rate)
        self.schema[out_col] = {"type": "number", "derived_from": column, "currency": tgt}
        self.schema[rate_col] = {"type": "number", "derived_from": currency_column}
        if dated:
            self.df.insert(pos + 2, added[2], rate_date.dt.strftime("%Y-%m-%d").where(rate_date.notna(), None))
            self.schema[added[2]] = {"type": "date", "derived_from": rates_file}
        self.added_columns.extend(added)
        no_rate = amt.notna() & rate.isna()
        reasons = cur[no_rate].fillna("currency missing").value_counts().to_dict()
        n_flag = self._flag(op_id, no_rate, column, f"not converted to {tgt}: no rate for this currency"
                            + (" on or before this date" if dated else ""), "not converted")
        n_conv = int(converted.notna().sum())
        return {"affected": n_conv, "flagged": n_flag, "method": f"converted to {tgt} with supplied rates",
                "statistical_basis": {"rates_file": rates_file, "rates_sha256": sha256_file(path), "target": tgt,
                                      "rate_rule": "latest rate on or before the row's date" if dated
                                      else "one rate per currency",
                                      "currencies_with_rates": sorted(rates["currency"].unique().tolist()),
                                      "decimals": decimals, "output_column": out_col, "rate_column": rate_col},
                "details": {"converted": n_conv, "not_converted": int(no_rate.sum()),
                            "not_converted_by_currency": {str(k): int(v) for k, v in reasons.items()}},
                "added_columns": added, "status": f"applied: {n_conv} converted, {int(no_rate.sum())} without a rate",
                "reversal": f"drop columns {', '.join(added)}; {column} and {currency_column} were not changed"}

    def op_winsorize(self, op_id, column, lower_q=0.01, upper_q=0.99, fit_on=None):
        s = _num(self.df[column])
        fit = s
        if fit_on:
            fit = s[canon_series(self.df[fit_on["column"]]).isin([str(v) for v in fit_on["values"]])]
        lo, hi = float(fit.quantile(lower_q)), float(fit.quantile(upper_q))
        before = {column: canon_series(self.df[column])}
        clipped = s.clip(lower=lo, upper=hi)
        self.df[column] = clipped.astype("Int64") if str(self.df[column].dtype) == "Int64" and \
            (clipped.dropna() % 1 == 0).all() else clipped
        counts = self._diff(op_id, [column], before)
        return {"affected": counts.get(column, 0), "method": "winsorise", "statistical_basis":
                {"lower_bound": lo, "upper_bound": hi, "lower_q": lower_q, "upper_q": upper_q},
                "reversal": "original values in audit/cell_changes.csv"}

    def op_flag_rows(self, op_id, rule, reason, column=None, classification="for review", exclude_from=None):
        """Flag rows for review. `exclude_from` (column names, or ["all"]) also keeps the rows out of those
        columns' statistics in EDA without deleting anything (analytical exclusion)."""
        mask = rule_mask(self.df, column, rule)
        n = self._flag(op_id, mask, column, reason, classification, exclude_from=exclude_from)
        excl = _as_list(exclude_from)
        return {"affected": 0, "flagged": n, "method": "flag for review (values unchanged)"
                + (f"; excluded from analysis of {', '.join(excl)}" if excl else ""),
                "status": f"flagged {n} rows" if n else "no rows matched", "reversal": "nothing changed"}


def _dist(s):
    x = _num(s).dropna()
    if not len(x):
        return {}
    return {"n": int(len(x)), "mean": float(x.mean()), "median": float(x.median()),
            "std": float(x.std(ddof=1)) if len(x) > 1 else 0.0}


def _spread(s):
    x = _num(s).dropna()
    if len(x) < 2:
        return "n < 2, spread unknown"
    return f"IQR {x.quantile(0.75) - x.quantile(0.25):.4g}, sd {x.std(ddof=1):.4g}"


def _share(s, v):
    s = s.dropna()
    return f"{100 * float((s == v).mean()):.1f}% of observed values are '{v}'" if len(s) else ""


# ---------------------------------------------------------------- writing outputs

def _write_csv(path, df):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_csv(path, index=False, encoding="utf-8", lineterminator="\n", quoting=csv.QUOTE_MINIMAL)


def write_outputs(engine, out_dir, raw_meta, excel=True):
    cd, ad = os.path.join(out_dir, "cleaned_data"), os.path.join(out_dir, "audit")
    cleaned = canon_frame(engine.df)
    _write_csv(os.path.join(cd, "cleaned_dataset.csv"), cleaned)
    schema = {"row_id_column": ROW_ID, "columns": {}}
    for c in engine.df.columns:
        if c == ROW_ID:
            continue
        dt = str(engine.df[c].dtype)
        kind = engine.schema.get(c, {}).get("type") or (
            "boolean" if dt in ("bool", "boolean") else "integer" if dt == "Int64" else
            "number" if dt.startswith("float") else "datetime" if dt.startswith("datetime") else "string")
        if c in engine.added_columns and not engine.schema.get(c, {}).get("type"):
            kind = "boolean"  # imputation indicators
        schema["columns"][c] = {"type": kind, "pandas_dtype": dt, "added_by_cleaning": c in engine.added_columns,
                                "sensitive": c in engine.sensitive, **{k: v for k, v in engine.schema.get(c, {}).items()
                                                                       if k != "type"}}
    schema["renamed_columns"] = engine.renames
    write_json(os.path.join(cd, "schema.json"), schema)
    if excel and optional("openpyxl") is not None and len(cleaned) < 1_048_000:
        try:
            write_excel_safe({"cleaned_data": cleaned}, os.path.join(cd, "cleaned_dataset.xlsx"))
        except Exception as exc:  # Excel output is optional
            engine.log.append({"op_id": "OUTPUT", "status": f"Excel copy not written: {exc}"})

    log_rows = []
    for e in engine.log:
        row = dict(e)
        log_rows.append(row)
    write_json(os.path.join(ad, "transformation_log.json"), {"operations": log_rows})
    flat = pd.DataFrame([{k: (v if not isinstance(v, (dict, list)) else str(v)) for k, v in r.items()} for r in log_rows])
    _write_csv(os.path.join(ad, "transformation_log.csv"), flat)

    ch = pd.DataFrame(engine.changes, columns=["op_id", ROW_ID, "column", "old_value", "new_value"])
    _write_csv(os.path.join(ad, "cell_changes.csv"), ch)

    raw = engine.raw.set_index(ROW_ID)
    rem = pd.DataFrame(engine.removed, columns=REMOVED_META)
    if len(rem):
        orig = raw.loc[rem[ROW_ID].tolist()].reset_index()
        rem = rem.merge(orig, on=ROW_ID, how="left")
    _write_csv(os.path.join(ad, "removed_records.csv"), rem)
    quar = os.path.join(ad, "quarantined_records.csv")
    if len(rem) and (rem["dm_disposition"] == "quarantined").any():
        _write_csv(quar, rem[rem["dm_disposition"] == "quarantined"])
    elif os.path.exists(quar):
        os.remove(quar)  # left over from an earlier run of a different plan
    if engine.removed_columns:
        rc = raw[list(engine.removed_columns)].reset_index()
        _write_csv(os.path.join(ad, "removed_columns.csv"), rc)
    fl = pd.DataFrame(engine.flags, columns=["flag_id", "op_id", ROW_ID, "column", "value", "reason", "classification",
                                             "exclude_from"])
    _write_csv(os.path.join(ad, "flagged_records.csv"), fl)
    from .evidence import classify_evidence, row_status
    comp_path = os.path.join(out_dir, "profiling", "imputation_comparison.json")
    comparison = read_json(comp_path) if os.path.exists(comp_path) else {}
    ev = classify_evidence(engine.evidence, comparison, engine.plan.get("settings", {}).get("config"),
                           {o["op_id"]: o.get("params", {}) for o in engine.plan.get("operations", [])})
    _write_csv(os.path.join(ad, "imputation_evidence.csv"), ev)
    _write_csv(os.path.join(ad, "row_status.csv"), row_status(engine.raw[ROW_ID], engine.log, ch, rem, fl))
    write_json(os.path.join(ad, "revert_info.json"), {
        "added_columns": engine.added_columns, "renames": engine.renames,
        "removed_columns": engine.removed_columns, "original_column_order": [c for c in engine.raw.columns],
        "op_order": [e["op_id"] for e in engine.log], "max_risk": engine.max_risk})
    return cleaned


REMOVED_META = ["op_id", ROW_ID, "reason", "retained_row_id", "dm_disposition", "dm_differing_columns"]


# ---------------------------------------------------------------- revert

def read_str_csv(path):
    return pd.read_csv(path, dtype=str, keep_default_na=False, na_filter=False, encoding="utf-8")


def revert(out_dir):
    """Rebuild the loaded original table (as strings) from cleaned data plus the audit files."""
    ad = os.path.join(out_dir, "audit")
    from .common import read_json
    info = read_json(os.path.join(ad, "revert_info.json"))
    log = read_json(os.path.join(ad, "transformation_log.json"))["operations"]
    df = read_str_csv(os.path.join(out_dir, "cleaned_data", "cleaned_dataset.csv"))
    df[ROW_ID] = df[ROW_ID].astype(int)
    df.index = df[ROW_ID].tolist()
    changes = read_str_csv(os.path.join(ad, "cell_changes.csv"))
    by_op = {k: g for k, g in changes.groupby("op_id", sort=False)} if len(changes) else {}
    df = df.drop(columns=[c for c in info["added_columns"] if c in df.columns])
    for e in reversed(log):
        if e.get("op") == "rename_columns" and str(e.get("status", "")).startswith("applied"):
            df = df.rename(columns={v: k for k, v in e["params"]["mapping"].items()})
        g = by_op.get(e["op_id"])
        if g is None:
            continue
        for row in g[::-1].itertuples(index=False):
            rid = int(getattr(row, ROW_ID))
            col = row.column
            if col in df.columns and rid in df.index:
                df.at[rid, col] = row.old_value
    rc_path = os.path.join(ad, "removed_columns.csv")
    if os.path.exists(rc_path):
        rc = read_str_csv(rc_path)
        rc[ROW_ID] = rc[ROW_ID].astype(int)
        rc = rc.set_index(ROW_ID)
        for c in rc.columns:
            df[c] = rc[c].reindex(df.index).fillna("")
    rem = read_str_csv(os.path.join(ad, "removed_records.csv"))
    if len(rem):
        rem[ROW_ID] = rem[ROW_ID].astype(int)
        rem = rem.drop(columns=[c for c in REMOVED_META if c != ROW_ID and c in rem.columns])
        rem.index = rem[ROW_ID].tolist()
        df = pd.concat([df, rem[[c for c in rem.columns]]], axis=0)
    cols = info["original_column_order"]
    df = df.sort_values(ROW_ID, kind="stable").reset_index(drop=True)
    missing = [c for c in cols if c not in df.columns]
    if missing:
        raise DataMedicError(f"Revert could not restore columns: {missing}")
    df = df[cols]
    df[ROW_ID] = df[ROW_ID].astype(int)
    return df.astype({c: object for c in cols if c != ROW_ID})


def frames_equal(a, b):
    """Compare two all-text frames; returns (equal, details)."""
    if list(a.columns) != list(b.columns):
        return False, {"columns_a": list(a.columns), "columns_b": list(b.columns)}
    if len(a) != len(b):
        return False, {"rows_a": len(a), "rows_b": len(b)}
    a2, b2 = a.reset_index(drop=True).astype(str), b.reset_index(drop=True).astype(str)
    diff = a2 != b2
    n = int(diff.values.sum())
    if n:
        r, c = np.argwhere(diff.values)[0]
        return False, {"differing_cells": n, "first": {"row": int(r), "column": a2.columns[c],
                                                       "a": a2.iat[r, c], "b": b2.iat[r, c]}}
    return True, {}


# ---------------------------------------------------------------- selective restore

def restore_step(out_dir, op_id):
    """The cleaned data with one step undone. Returns (table, report); writes nothing.

    Cells the step changed go back to their value before the step. A cell that a later step changed again is
    still restored, and the report lists it, because the later change is lost. Rows the step removed come back
    with their original values (no other step is applied to them). An imputation's indicator column is dropped
    and a dropped column comes back from removed_columns.csv.
    """
    from .common import read_json
    ad = os.path.join(out_dir, "audit")
    log = read_json(os.path.join(ad, "transformation_log.json"))["operations"]
    entry = next((e for e in log if e.get("op_id") == op_id), None)
    if entry is None:
        raise DataMedicError(f"{op_id} is not in the transformation log")
    if not str(entry.get("status", "")).startswith(("applied", "flagged")):
        raise DataMedicError(f"{op_id} was not applied ({entry.get('status')}); there is nothing to restore")
    df = read_str_csv(os.path.join(out_dir, "cleaned_data", "cleaned_dataset.csv"))
    df.index = df[ROW_ID].astype(int).tolist()
    info = read_json(os.path.join(ad, "revert_info.json"))
    changes = read_str_csv(os.path.join(ad, "cell_changes.csv"))
    order = {e["op_id"]: i for i, e in enumerate(log)}
    mine = changes[changes["op_id"] == op_id]
    later = changes[changes["op_id"].map(order) > order[op_id]]
    later_keys = set(zip(later[ROW_ID].astype(int), later["column"]))
    report = {"op_id": op_id, "op": entry.get("op"), "cells_restored": 0, "cells_changed_again_later": 0,
              "rows_restored": 0, "columns_restored": [], "columns_dropped": [], "notes": []}
    for row in mine.itertuples(index=False):
        rid, col = int(getattr(row, ROW_ID)), row.column
        if col in df.columns and rid in df.index:
            df.at[rid, col] = row.old_value
            report["cells_restored"] += 1
            report["cells_changed_again_later"] += (rid, col) in later_keys
    if report["cells_changed_again_later"]:
        report["notes"].append(f"{report['cells_changed_again_later']} restored cells had been changed again by later "
                               "steps; those later changes are not in this copy")
    ind = (entry.get("statistical_basis") or {}).get("indicator_column")
    for c in ([ind] if ind else []) + list(entry.get("added_columns") or []):
        if c in df.columns:
            df = df.drop(columns=[c])
            report["columns_dropped"].append(c)
    if entry.get("op") == "drop_columns":
        rc = read_str_csv(os.path.join(ad, "removed_columns.csv"))
        rc.index = rc[ROW_ID].astype(int).tolist()
        for c in [c for c, op in info["removed_columns"].items() if op == op_id]:
            name = info["renames"].get(c, c)
            df[name] = rc[c].reindex(df.index).fillna("")
            report["columns_restored"].append(name)
    if entry.get("op") in ("drop_rows", "drop_duplicates", "quarantine_rows"):
        rem = read_str_csv(os.path.join(ad, "removed_records.csv"))
        rem = rem[rem["op_id"] == op_id].drop(columns=[c for c in REMOVED_META if c != ROW_ID and c in rem.columns])
        rem = rem.rename(columns=info["renames"])
        rem.index = rem[ROW_ID].astype(int).tolist()
        for c in df.columns:
            if c not in rem.columns:
                rem[c] = ""
        df = pd.concat([df, rem[list(df.columns)]], axis=0)
        report["rows_restored"] = int(len(rem))
        if len(rem):
            report["notes"].append("restored rows carry their original values; the other cleaning steps were not "
                                   "applied to them")
    df = df.sort_values(ROW_ID, key=lambda s: s.astype(int), kind="stable").reset_index(drop=True)
    return df, report
