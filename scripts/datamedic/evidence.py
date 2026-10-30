"""Imputation evidence records and per-row status.

An imputed value is an estimate, never the original. Each filled cell gets a record of how it was
estimated, what supports it and how the method did on masked known values, plus a qualitative
evidence level. There is no numeric "confidence" per cell: the inputs to that judgement are listed
instead, and the thresholds that map them to a level are in config/default.json
(`imputation_evidence`) and explained in references/imputation.md.
"""

import pandas as pd

from .common import ROW_ID, load_config

LEVELS = ["strong supporting evidence", "moderate supporting evidence", "limited supporting evidence",
          "insufficient evidence"]
NOT_ESTIMATE = "not an estimate (explicit missing label)"
MISSING_LABELS = {"missing", "unknown", "not recorded", "unspecified", "n/a", "none recorded"}
COLUMNS = [
    "op_id", ROW_ID, "column", "method", "imputed_value", "support_n", "group", "group_n", "dispersion", "gap_length",
    "uses_future_values", "fallback_used", "source", "validation_metric", "validation_value", "validation_baseline",
    "validation_n", "validated", "evidence_level", "suitable_for_downstream", "limitations",
]


def _defaults(cfg):
    base = load_config()["imputation_evidence"]
    return {**base, **((cfg or {}).get("imputation_evidence") or {})}


def _int(x):
    try:
        return int(float(x))
    except (TypeError, ValueError):
        return None


def _as_tuple(x):
    return tuple(x) if isinstance(x, (list, tuple)) else (x,) if x else ()


def _validation(comparison, column, params):
    """The masked-value result for the spec this operation used, if the comparison ran."""
    res = (comparison.get("columns") or {}).get(column) or {}
    for m in (res.get("methods") or {}).values():
        spec = m.get("spec") or {}
        if spec.get("method") == params.get("method") and                 _as_tuple(spec.get("group_by")) == _as_tuple(params.get("group_by")):
            return (m if m.get("status") == "ok" else None), res
    return None, res


def classify_evidence(records, comparison, cfg=None, op_params=None):
    th = _defaults(cfg)
    rows = []
    for r in records:
        r = dict(r)
        method, col = r["method"], r["column"]
        params = (op_params or {}).get(r["op_id"]) or {"method": method}
        val, colres = _validation(comparison or {}, col, params)
        r["validated"] = val is not None
        lim = []
        if val is not None:
            metric = "normalised MAE (MAE / sd)" if "nmae" in val else "accuracy"
            r["validation_metric"] = metric
            r["validation_value"] = val.get("nmae", val.get("accuracy"))
            r["validation_baseline"] = colres.get("baseline", {}).get("nmae", colres.get("baseline", {}).get("accuracy", ""))
            r["validation_n"] = val.get("masked_n", "")
        n = _int(r.get("support_n"))
        gap = _int(r.get("gap_length"))
        if method == "domain rule":
            level = LEVELS[0]
            lim.append("as reliable as the rule; holds only inside the rule's stated scope")
        elif method == "constant":
            if str(r.get("imputed_value", "")).strip().casefold() in MISSING_LABELS:
                level = NOT_ESTIMATE
                lim.append("marks the value as missing; it does not estimate it")
            elif r.get("source"):
                level = LEVELS[0]
                lim.append("documented fixed value; correct only if the stated source applies to every filled row")
            else:
                level = LEVELS[3]
                lim.append("a fixed value with no documented source says nothing about the true value")
        elif method == "lookup":
            level = LEVELS[2]
            lim.append("mapping supplied in the plan; its agreement with known values was not measured here")
        else:
            if n is not None and n < th["insufficient_below_support"]:
                level = LEVELS[3]
                lim.append(f"based on {n} observations")
            elif val is not None and "nmae" in val:
                good = val["nmae"] <= th["good_normalised_mae"]
                usable = val["nmae"] < th["max_normalised_mae"]
                if good and (n or 0) >= th["strong_min_support"]:
                    level = LEVELS[0]
                elif usable and (n or 0) >= th["limited_below_support"]:
                    level = LEVELS[1]
                else:
                    level = LEVELS[2]
                    lim.append("on masked known values the method was barely better than, or worse than, a constant")
            elif val is not None and "accuracy" in val:
                if val["accuracy"] >= th["good_accuracy"] and (n or 0) >= th["strong_min_support"]:
                    level = LEVELS[0]
                elif val["accuracy"] >= th["usable_accuracy"]:
                    level = LEVELS[1]
                else:
                    level = LEVELS[2]
                    lim.append("on masked known values most predictions were wrong")
            else:
                level = LEVELS[1] if (n or 0) >= th["limited_below_support"] or method in ("interpolate", "ffill", "bfill") \
                    else LEVELS[2]
                lim.append("not validated on masked values; level rests on the amount of supporting data only")
            if r.get("fallback_used") in (True, "True"):
                level = max(level, LEVELS[2], key=LEVELS.index)
                lim.append("group too small or absent; the overall value was used instead")
            if gap is not None and gap > th["max_gap_for_strong"] and method in ("interpolate", "ffill", "bfill"):
                level = max(level, LEVELS[1], key=LEVELS.index)
                lim.append(f"inside a run of {gap} consecutive missing values")
            if r.get("uses_future_values") in (True, "True"):
                lim.append("uses later observations; fine for describing the past, not for forecasting or models "
                           "evaluated in time order")
        lim.append("assumes values are missing for reasons unrelated to the value itself (not testable from the data)"
                   if level != NOT_ESTIMATE and method not in ("domain rule",) else "")
        r["evidence_level"] = level
        r["suitable_for_downstream"] = {
            LEVELS[0]: "yes; keep the _was_missing indicator in models",
            LEVELS[1]: "yes for descriptive statistics; for inference keep the indicator and check sensitivity",
            LEVELS[2]: "descriptive use only; exclude from inference or run a sensitivity analysis",
            LEVELS[3]: "no; treat as missing",
            NOT_ESTIMATE: "yes as a category; never as a measured value",
        }[level]
        r["limitations"] = "; ".join(x for x in lim if x)
        rows.append(r)
    return pd.DataFrame(rows, columns=COLUMNS)


def row_status(ids, log, changes, removed, flags):
    """One line per original row: what happened to it and whether analyses may use it."""
    op_kind = {e["op_id"]: e.get("op") for e in log}
    idx = pd.Index(ids.astype(int), name=ROW_ID)
    out = pd.DataFrame(index=idx)
    out["record_status"] = "retained"
    out["quality_flags"] = ""
    out["imputed_columns"] = ""
    out["corrected_columns"] = ""
    out["exclusion_reason"] = ""
    out["quarantine_reason"] = ""
    out["analytical_exclusions"] = ""
    if len(changes):
        ch = changes.assign(kind=changes["op_id"].map(op_kind), rid=changes[ROW_ID].astype(int))
        imp = ch[ch["kind"].isin(["impute", "apply_rule"])]
        cor = ch[ch["kind"].isin(["set_missing_where", "replace_values", "winsorize"])]
        std = ch[~ch.index.isin(imp.index) & ~ch.index.isin(cor.index)]
        for frame, col in ((imp, "imputed_columns"), (cor, "corrected_columns")):
            if len(frame):
                agg = frame.groupby("rid")["column"].agg(lambda s: ", ".join(sorted(set(s))))
                out.loc[agg.index, col] = agg.values
        out.loc[out.index.isin(std["rid"]), "record_status"] = "standardised"
        out.loc[out.index.isin(cor["rid"]), "record_status"] = "corrected"
        out.loc[out.index.isin(imp["rid"]), "record_status"] = "imputed"
    if len(flags):
        f = flags.assign(rid=flags[ROW_ID].astype(int))
        agg = f.groupby("rid")["classification"].agg(lambda s: "; ".join(sorted(set(s))))
        out.loc[agg.index, "quality_flags"] = agg.values
        ex = f[f["exclude_from"].astype(str) != ""] if "exclude_from" in f.columns else f.iloc[0:0]
        if len(ex):
            ex = ex.assign(note=ex["exclude_from"] + " (" + ex["reason"] + ")")
            agg = ex.groupby("rid")["note"].agg(lambda s: "; ".join(sorted(set(s))))
            out.loc[agg.index, "analytical_exclusions"] = agg.values
        only_flag = out.index.isin(f["rid"]) & (out["record_status"] == "retained")
        out.loc[only_flag, "record_status"] = "retained, flagged"
    if len(removed):
        rm = removed.assign(rid=removed[ROW_ID].astype(int))
        for disp, col in (("removed", "exclusion_reason"), ("quarantined", "quarantine_reason")):
            sub = rm[rm["dm_disposition"] == disp]
            out.loc[sub["rid"].values, "record_status"] = disp
            out.loc[sub["rid"].values, col] = sub["reason"].values
    out["analytical_eligibility"] = "eligible"
    out.loc[out["analytical_exclusions"] != "", "analytical_eligibility"] = "eligible except the listed exclusions"
    out.loc[out["record_status"] == "quarantined", "analytical_eligibility"] = "not in the cleaned data (quarantined)"
    out.loc[out["record_status"] == "removed", "analytical_eligibility"] = "not in the cleaned data (removed)"
    return out.reset_index()
