"""Execute a cleaning plan with a complete, reversible audit trail.

Every value change is detected by comparing canonical strings before and after each
operation, so the change log cannot miss a change an operation made. Removed rows and
columns are kept in their original (raw) form. `revert` rebuilds the loaded table from
the cleaned data and the audit files and checks it against the original.
"""

import csv
import os

import numpy as np
import pandas as pd

from .common import (ROW_ID, DataMedicError, canon_frame, canon_series, mask_value, now_iso, optional,
                     parse_bool, parse_date, parse_number, write_json)

STAGE = {
    "rename_columns": "structure", "drop_rows": "structure", "trim_whitespace": "standardise text",
    "standardize_missing": "missing values", "convert_type": "data types", "standardize_categories": "standardise text",
    "replace_values": "corrections", "set_missing_where": "invalid values", "drop_columns": "structure",
    "drop_duplicates": "duplicates", "impute": "missing values", "winsorize": "outliers", "flag_rows": "review flags",
}
OP_ORDER = ["rename_columns", "drop_rows", "trim_whitespace", "standardize_missing", "convert_type",
            "standardize_categories", "replace_values", "set_missing_where", "drop_columns", "drop_duplicates",
            "impute", "winsorize", "flag_rows"]


def _is_missing(s):
    return s.isna() | s.map(lambda v: isinstance(v, str) and v == "")


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
    def __init__(self, raw, plan, sensitive=()):
        self.raw = raw
        self.df = raw.copy()
        self.plan = plan
        self.seed = plan.get("settings", {}).get("seed", 42)
        self.sensitive = set(sensitive)
        self.log, self.changes, self.removed, self.flags = [], [], [], []
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

    def _flag(self, op_id, mask, column, reason, classification, value_col=None):
        vc = value_col or column
        rows = self.df.loc[mask]
        vals = canon_series(rows[vc]) if vc in rows.columns else pd.Series("", index=rows.index)
        for rid, v in zip(rows[ROW_ID].tolist(), vals.tolist()):
            if vc in self.sensitive:
                v = mask_value(v)
            self.flags.append({"flag_id": f"FLAG-{len(self.flags) + 1:05d}", "op_id": op_id, ROW_ID: int(rid),
                               "column": column or "", "value": v, "reason": reason, "classification": classification})
        return int(mask.sum())

    # ------------------------------------------------------------ run
    def run(self):
        ops = self.plan.get("operations", [])
        order = {k: i for i, k in enumerate(OP_ORDER)}
        for op in sorted(ops, key=lambda o: (order.get(o["op"], 99), ops.index(o))):
            entry = {"op_id": op["op_id"], "timestamp": now_iso(), "stage": STAGE.get(op["op"], "other"),
                     "op": op["op"], "column": op.get("params", {}).get("column") or
                     ", ".join(op.get("params", {}).get("columns", []) or []),
                     "issue": op.get("issue", ""), "rationale": op.get("rationale", ""),
                     "evidence": op.get("evidence", {}), "params": op.get("params", {}),
                     "decided_by": op.get("decided_by", "auto"), "needs_review": op.get("needs_review", False)}
            if not op.get("enabled", True):
                entry.update(status="not applied (disabled in plan)", affected=0, reversible=True,
                             reversal="nothing to undo")
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
            self.removed.append({"op_id": op_id, ROW_ID: r, "reason": reason, "retained_row_id": ""})
        self.df = self.df.loc[~mask].copy()
        return {"affected": len(ids), "method": "remove rows to quarantine", "row_count_change": -len(ids),
                "reversal": "rows are stored with original values in audit/removed_records.csv"}

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

    def op_drop_duplicates(self, op_id, reason, subset=None, keep="first", ignore_case=False):
        cols = subset or [c for c in self.df.columns if c != ROW_ID]
        key = canon_frame(self.df[cols])
        if ignore_case:
            key = key.apply(lambda s: s.str.casefold())
        if keep == "most_complete":
            filled = (canon_frame(self.df.drop(columns=[ROW_ID])) != "").sum(axis=1)
            order = filled.sort_values(ascending=False, kind="stable").index
            key_o = key.loc[order]
            dup_o = key_o.duplicated(keep="first")
            dup = dup_o.reindex(self.df.index)
        else:
            dup = key.duplicated(keep=keep)
        groups = key.astype(str).agg("\x1f".join, axis=1)
        kept = {}
        for idx in self.df.index[~dup]:
            kept.setdefault(groups[idx], int(self.df.at[idx, ROW_ID]))
        ids = []
        for idx in self.df.index[dup]:
            r = int(self.df.at[idx, ROW_ID])
            ids.append(r)
            self.removed.append({"op_id": op_id, ROW_ID: r, "reason": reason, "retained_row_id": kept.get(groups[idx], "")})
        self.df = self.df.loc[~dup].copy()
        return {"affected": len(ids), "method": f"remove duplicates (keep {keep})",
                "details": {"columns_compared": "all" if subset is None else cols, "ignore_case": ignore_case},
                "row_count_change": -len(ids),
                "reversal": "removed rows with original values in audit/removed_records.csv"}

    def op_impute(self, op_id, column, method, value=None, group_by=None, add_indicator=True, order_by=None,
                  features=None, fit_on=None, limit=None, n_neighbors=5):
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
        elif method in ("group_median", "group_mean", "group_mode"):
            if not group_by:
                raise DataMedicError(f"{op_id}: {method} needs group_by")
            gcols = group_by if isinstance(group_by, list) else [group_by]
            fitdf = self.df[fit_rows & ~miss]
            agg = {"group_median": "median", "group_mean": "mean"}.get(method)
            if agg:
                table = fitdf.groupby(gcols, dropna=False)[column].agg(agg)
                overall = float(getattr(_num(fitdf[column]), agg)())
            else:
                table = fitdf.groupby(gcols, dropna=False)[column].agg(lambda x: x.mode().iloc[0] if len(x.mode()) else None)
                m = fitdf[column].mode()
                overall = m.iloc[0] if len(m) else None
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
        elif method in ("interpolate", "ffill", "bfill"):
            if not order_by:
                raise DataMedicError(f"{op_id}: {method} needs order_by (a date or sequence column)")
            order = self.df.sort_values([order_by, ROW_ID], kind="stable").index
            ordered = _num(s.loc[order]) if method == "interpolate" else s.loc[order]
            if method == "interpolate":
                t = self.df.loc[order, order_by]
                if pd.api.types.is_datetime64_any_dtype(t):
                    tmp = pd.Series(ordered.values, index=pd.DatetimeIndex(t.values))
                    filled = tmp.interpolate(method="time", limit_area="inside", limit=limit).values
                else:
                    filled = ordered.interpolate(method="linear", limit_area="inside", limit=limit).values
                res = pd.Series(filled, index=order)
            elif method == "ffill":
                res = ordered.ffill(limit=limit)
            else:
                res = ordered.bfill(limit=limit)
            res = res.reindex(self.df.index)
            if str(s.dtype) == "Int64":
                res = res.round().astype("Int64")
            out = s.copy()
            out[miss] = res[miss]
            basis.update(order_by=order_by, limit=limit, note="no extrapolation beyond the first/last observed value"
                         if method == "interpolate" else "")
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
        still = int(_is_missing(self.df[column]).sum())
        return {"affected": counts.get(column, 0), "method": f"{method} imputation", "statistical_basis": basis,
                "details": {"missing_before": n_miss, "missing_after": still},
                "reversal": "imputed cells listed with original (empty) values in audit/cell_changes.csv"
                + (f"; indicator column {basis.get('indicator_column')} marks them" if add_indicator else "")}

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

    def op_flag_rows(self, op_id, rule, reason, column=None, classification="for review"):
        mask = rule_mask(self.df, column, rule)
        n = self._flag(op_id, mask, column, reason, classification)
        return {"affected": 0, "flagged": n, "method": "flag for review (values unchanged)",
                "status": f"flagged {n} rows" if n else "no rows matched", "reversal": "nothing changed"}


def _dist(s):
    x = _num(s).dropna()
    if not len(x):
        return {}
    return {"n": int(len(x)), "mean": float(x.mean()), "median": float(x.median()),
            "std": float(x.std(ddof=1)) if len(x) > 1 else 0.0}


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
        if c in engine.added_columns:
            kind = "boolean"
        schema["columns"][c] = {"type": kind, "pandas_dtype": dt, "added_by_cleaning": c in engine.added_columns,
                                "sensitive": c in engine.sensitive, **{k: v for k, v in engine.schema.get(c, {}).items()
                                                                       if k != "type"}}
    schema["renamed_columns"] = engine.renames
    write_json(os.path.join(cd, "schema.json"), schema)
    if excel and optional("openpyxl") is not None and len(cleaned) < 1_048_000:
        try:
            cleaned.to_excel(os.path.join(cd, "cleaned_dataset.xlsx"), index=False)
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
    rem = pd.DataFrame(engine.removed, columns=["op_id", ROW_ID, "reason", "retained_row_id"])
    if len(rem):
        orig = raw.loc[rem[ROW_ID].tolist()].reset_index()
        rem = rem.merge(orig, on=ROW_ID, how="left")
    _write_csv(os.path.join(ad, "removed_records.csv"), rem)
    if engine.removed_columns:
        rc = raw[list(engine.removed_columns)].reset_index()
        _write_csv(os.path.join(ad, "removed_columns.csv"), rc)
    fl = pd.DataFrame(engine.flags, columns=["flag_id", "op_id", ROW_ID, "column", "value", "reason", "classification"])
    _write_csv(os.path.join(ad, "flagged_records.csv"), fl)
    write_json(os.path.join(ad, "revert_info.json"), {
        "added_columns": engine.added_columns, "renames": engine.renames,
        "removed_columns": engine.removed_columns, "original_column_order": [c for c in engine.raw.columns],
        "op_order": [e["op_id"] for e in engine.log]})
    return cleaned


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
        rem = rem.drop(columns=["op_id", "reason", "retained_row_id"])
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
