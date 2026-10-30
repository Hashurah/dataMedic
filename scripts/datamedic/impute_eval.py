"""Masked-value comparison of imputation methods.

Known values are hidden at random, each candidate method fills them using only the remaining data
(the cleaning engine itself does the filling, so the comparison tests the exact code that will run),
and the fills are compared with the hidden truth. Repeated with different masks.

What this does and does not show: random masking imitates values missing completely at random. If
the real values are missing because of what they are (MNAR), or depend on something unobserved, a
method can score well here and still be biased on the real gaps. The output says so.
"""

import numpy as np
import pandas as pd

from .clean import Engine, _is_missing, _num
from .common import ROW_ID, canon_series, load_config

LIMITATIONS = [
    "random masking imitates values missing completely at random; it cannot show how a method behaves if the real "
    "gaps are missing because of their own values (MNAR)",
    "errors are measured on values that were observed, which may differ from the values that are missing",
]


def _fill(df, column, spec, seed):
    params = {k: v for k, v in spec.items() if k not in ("name",)}
    params.update(column=column, add_indicator=False)
    op = {"op_id": "CMP", "op": "impute", "params": params, "enabled": True}
    eng = Engine(df, {"operations": [op], "settings": {"seed": seed}}, collect_evidence=False).run()
    entry = eng.log[0]
    if entry.get("status") == "failed":
        raise RuntimeError(entry.get("error", "failed"))
    return eng.df[column]


def _macro_f1(truth, pred):
    labels = sorted(set(truth) | set(pred))
    f1s = []
    for lab in labels:
        tp = sum(1 for t, p in zip(truth, pred) if t == lab and p == lab)
        fp = sum(1 for t, p in zip(truth, pred) if t != lab and p == lab)
        fn = sum(1 for t, p in zip(truth, pred) if t == lab and p != lab)
        if tp + fp + fn == 0:
            continue
        f1s.append(2 * tp / (2 * tp + fp + fn))
    return float(np.mean(f1s)) if f1s else 0.0


def compare_methods(df, column, candidates, cfg=None, seed=42, fit_rows=None, group_for_subgroups=None):
    """Compare candidate imputation specs for one column. `df` is the typed table (with ROW_ID)."""
    cc = {**load_config()["imputation_comparison"], **((cfg or {}).get("imputation_comparison") or {})}
    s = df[column]
    numeric = pd.api.types.is_numeric_dtype(s) and not pd.api.types.is_bool_dtype(s)
    observed = ~_is_missing(s)
    if fit_rows is not None:
        observed &= fit_rows
    obs_idx = df.index[observed]
    out = {"column": column, "kind": "numeric" if numeric else "categorical", "observed": int(len(obs_idx)),
           "limitations": LIMITATIONS, "methods": {}}
    if len(obs_idx) < cc["min_observed"]:
        out["status"] = f"skipped: only {len(obs_idx)} observed values (needs {cc['min_observed']})"
        return out
    k = int(min(cc["max_masked"], max(5, round(cc["mask_share"] * len(obs_idx)))))
    rng = np.random.default_rng(seed)
    masks = [rng.choice(obs_idx.to_numpy(), size=k, replace=False) for _ in range(int(cc["repeats"]))]
    truth_all = _num(s[obs_idx]) if numeric else canon_series(s[obs_idx])
    sd = float(truth_all.std(ddof=1)) if numeric else None
    out.update(masked_per_repeat=k, repeats=len(masks), mask_share=cc["mask_share"])
    for spec in candidates:
        name = spec.get("name") or spec["method"]
        per = []
        try:
            for m in masks:
                dm = df.copy()
                col = dm[column].astype(object) if not numeric else dm[column].copy()
                col.loc[m] = None if not numeric else np.nan
                if str(df[column].dtype) == "Int64":
                    col = col.astype("Int64")
                dm[column] = col
                filled = _fill(dm, column, spec, seed)
                if numeric:
                    t, p = _num(s.loc[m]), _num(filled.loc[m])
                    ok = p.notna()
                    err = (p[ok] - t[ok])
                    per.append({"coverage": float(ok.mean()), "mae": float(err.abs().mean()) if ok.any() else None,
                                "rmse": float(np.sqrt((err ** 2).mean())) if ok.any() else None,
                                "bias": float(err.mean()) if ok.any() else None,
                                "spread_ratio": float(p[ok].std(ddof=1) / t[ok].std(ddof=1))
                                if ok.sum() > 2 and t[ok].std(ddof=1) > 0 else None,
                                "errors": (m[ok.to_numpy()], err.abs().to_numpy())})
                else:
                    t, p = canon_series(s.loc[m]), canon_series(filled.loc[m])
                    ok = p != ""
                    per.append({"coverage": float(ok.mean()),
                                "accuracy": float((t[ok] == p[ok]).mean()) if ok.any() else None,
                                "macro_f1": _macro_f1(list(t[ok]), list(p[ok])) if ok.any() else None})
        except Exception as exc:  # a method that cannot run here is reported, not fatal
            out["methods"][name] = {"method": spec["method"], "spec": spec, "status": "could not run",
                                    "error": f"{type(exc).__name__}: {exc}"}
            continue
        rec = {"method": spec["method"], "spec": spec, "status": "ok", "masked_n": k * len(masks),
               "coverage": float(np.mean([r["coverage"] for r in per]))}
        keys = ("mae", "rmse", "bias", "spread_ratio") if numeric else ("accuracy", "macro_f1")
        for key in keys:
            vals = [r[key] for r in per if r[key] is not None]
            rec[key] = float(np.mean(vals)) if vals else None
            if key in ("mae", "accuracy") and len(vals) > 1:
                rec[key + "_sd_across_repeats"] = float(np.std(vals, ddof=1))
        if numeric and rec["mae"] is not None and sd:
            rec["nmae"] = rec["mae"] / sd
        if numeric and group_for_subgroups and group_for_subgroups in df.columns and rec["mae"] is not None:
            ids = np.concatenate([r["errors"][0] for r in per])
            errs = np.concatenate([r["errors"][1] for r in per])
            g = canon_series(df.loc[ids, group_for_subgroups]).to_numpy()
            sub = pd.DataFrame({"g": g, "e": errs}).groupby("g")["e"].agg(["mean", "count"])
            rec["subgroup_mae"] = {"by": group_for_subgroups, "groups": {
                str(i): {"mae": float(r["mean"]), "n": int(r["count"])}
                for i, r in sub.sort_values("count", ascending=False).head(10).iterrows()}}
        if rec["coverage"] < 0.95:
            rec["note"] = f"filled only {rec['coverage'] * 100:.0f}% of the masked values"
        out["methods"][name] = rec
    ok = {n: m for n, m in out["methods"].items() if m.get("status") == "ok" and m["coverage"] >= 0.95}
    if numeric:
        base = out["methods"].get("mean")
        out["baseline"] = {"method": "mean", "nmae": base.get("nmae")} if base and base.get("status") == "ok" else {}
        ranked = sorted((n for n in ok if ok[n].get("nmae") is not None), key=lambda n: ok[n]["nmae"])
    else:
        base = out["methods"].get("mode")
        out["baseline"] = {"method": "mode", "accuracy": base.get("accuracy")} if base and base.get("status") == "ok" else {}
        ranked = sorted((n for n in ok if ok[n].get("accuracy") is not None), key=lambda n: -ok[n]["accuracy"])
    out["ranking"] = ranked
    out["best"] = ranked[0] if ranked else None
    out["status"] = "ok"
    return out


def better_than(comp, chosen, cfg=None):
    """Name of a compared method that clearly beats `chosen` (by the configured margin, with no larger bias), or None."""
    cc = {**load_config()["imputation_comparison"], **((cfg or {}).get("imputation_comparison") or {})}
    if not comp or comp.get("status") != "ok" or not comp.get("best") or comp["best"] == chosen:
        return None
    best, cur = comp["methods"][comp["best"]], comp["methods"].get(chosen)
    if not cur or cur.get("status") != "ok":
        return None
    if comp["kind"] == "numeric":
        if cur.get("nmae") is None or best.get("nmae") is None:
            return None
        sd_unit = cur["mae"] / cur["nmae"] if cur["nmae"] else 0
        bias_ok = abs(best.get("bias") or 0) <= abs(cur.get("bias") or 0) + 0.1 * sd_unit
        if best["nmae"] <= (1 - cc["switch_if_error_lower_by"]) * cur["nmae"] and bias_ok:
            return comp["best"]
        return None
    if (best.get("accuracy") or 0) >= (cur.get("accuracy") or 0) + cc["switch_if_error_lower_by"]:
        return comp["best"]
    return None


def candidates_for(df, column, numeric, cats, numeric_cols, time_col=None, group_hint=None, sklearn=False):
    """Plausible candidate specs for a column, from what the table offers."""
    specs = []
    if numeric:
        specs += [{"name": "mean", "method": "mean"}, {"name": "median", "method": "median"}]
        gcands = ([group_hint] if group_hint else []) + [c for c in cats if c != group_hint]
        for g in gcands[:3]:
            specs.append({"name": f"group_median by {g}", "method": "group_median", "group_by": g})
            specs.append({"name": f"group_mean by {g}", "method": "group_mean", "group_by": g})
        if time_col:
            specs.append({"name": "interpolate", "method": "interpolate", "order_by": time_col})
        feats = [c for c in numeric_cols if c != column and _is_missing(df[c]).mean() < 0.3][:6]
        if sklearn and len(feats) >= 1:
            specs.append({"name": "knn", "method": "knn", "features": feats})
            specs.append({"name": "iterative", "method": "iterative", "features": feats})
    else:
        specs.append({"name": "mode", "method": "mode"})
        gcands = ([group_hint] if group_hint else []) + [c for c in cats if c not in (group_hint, column)]
        for g in gcands[:3]:
            specs.append({"name": f"group_mode by {g}", "method": "group_mode", "group_by": g})
    return specs
