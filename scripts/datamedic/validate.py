"""Independent validation: reload everything from disk and check the cleaned data against the original.

The checks do not trust the cleaning run's in-memory state. They re-read the original
file, the cleaned CSV and the audit files, and recompute.
"""

import os
import shutil
import tempfile

import pandas as pd

from .clean import frames_equal, read_str_csv, revert
from .common import ROW_ID, read_json, sha256_file, write_json
from .loader import load_table

CRITICAL = "critical"
WARNING = "warning"


class Checks:
    def __init__(self):
        self.items = []

    def add(self, name, passed, severity=CRITICAL, detail="", **extra):
        self.items.append({"check": name, "passed": bool(passed), "severity": severity, "detail": detail, **extra})


def typed_view(cleaned, schema):
    out = cleaned.copy()
    for c, info in schema["columns"].items():
        if c not in out.columns:
            continue
        t = info["type"]
        s = out[c].mask(out[c] == "")
        if t in ("number", "integer"):
            out[c] = pd.to_numeric(s, errors="coerce")
        elif t in ("date", "datetime"):
            out[c] = pd.to_datetime(s, errors="coerce", utc=s.astype(str).str.contains(r"[+-]\d{2}:\d{2}$", na=False).any())
        elif t == "boolean":
            out[c] = s.map({"true": True, "false": False})
        else:
            out[c] = s
    return out


def validate(out_dir, replay=True, contract_path=None):
    man = read_json(os.path.join(out_dir, "metadata", "analysis_manifest.json"))
    plan = read_json(os.path.join(out_dir, "plan.json"))
    ck = Checks()
    src = man["input"]["path"]
    ref = man["input"].get("reference_copy")
    # 1. original untouched
    if os.path.exists(src):
        h = sha256_file(src)
        ck.add("original file unchanged", h == man["input"]["sha256"], detail=f"sha256 {h[:12]}...")
    else:
        ck.add("original file unchanged", True, WARNING, detail="original path no longer exists; using reference copy")
    if ref and os.path.exists(os.path.join(out_dir, ref)):
        ck.add("reference copy intact", sha256_file(os.path.join(out_dir, ref)) == man["input"]["sha256"])
    load_from = src if os.path.exists(src) else os.path.join(out_dir, ref)
    lo = man.get("load_options", {})
    raw, meta = load_table(load_from, sheet=lo.get("sheet"), table=lo.get("table"), block=lo.get("block"),
                           sample=lo.get("sample"), seed=lo.get("seed", 42))
    cleaned = read_str_csv(os.path.join(out_dir, "cleaned_data", "cleaned_dataset.csv"))
    schema = read_json(os.path.join(out_dir, "cleaned_data", "schema.json"))
    ad = os.path.join(out_dir, "audit")
    changes = read_str_csv(os.path.join(ad, "cell_changes.csv"))
    removed = read_str_csv(os.path.join(ad, "removed_records.csv"))
    info = read_json(os.path.join(ad, "revert_info.json"))
    log = read_json(os.path.join(ad, "transformation_log.json"))["operations"]

    # 2. row accounting
    cid = cleaned[ROW_ID].astype(int)
    rid = removed[ROW_ID].astype(int) if len(removed) else pd.Series([], dtype=int)
    ok_unique = cid.is_unique
    ck.add("row ids unique in cleaned data", ok_unique)
    both = set(cid) & set(rid)
    ck.add("no row is both kept and removed", not both, detail=f"{len(both)} overlapping ids" if both else "")
    total = len(cleaned) + len(removed)
    ck.add("row accounting: original = cleaned + removed", total == len(raw) and set(cid) | set(rid) == set(raw[ROW_ID]),
           detail=f"original {len(raw)}, cleaned {len(cleaned)}, removed {len(removed)}")
    # 3. column accounting
    renames = info["renames"]
    orig_cols = [c for c in raw.columns if c != ROW_ID]
    now = set(cleaned.columns) - {ROW_ID} - set(info["added_columns"])
    expected = {renames.get(c, c) for c in orig_cols} - {renames.get(c, c) for c in info["removed_columns"]}
    ck.add("column accounting: original = cleaned + removed columns - added indicators", now == expected,
           detail=f"unexpected {sorted(now - expected)}, missing {sorted(expected - now)}" if now != expected else
           f"{len(orig_cols)} original, {len(info['removed_columns'])} removed, {len(info['added_columns'])} added")
    # 4. every changed cell is logged (independent diff against the original)
    inv = {v: k for k, v in renames.items()}
    rawi = raw.set_index(ROW_ID)
    cl = cleaned.copy()
    cl[ROW_ID] = cid
    cl = cl.set_index(ROW_ID)
    last = {}
    first = {}
    for row in changes.itertuples(index=False):
        key = (int(getattr(row, ROW_ID)), row.column)
        first.setdefault(key, row.old_value)
        last[key] = row.new_value
    unlogged, mismatched = [], []
    for c in now:
        oc = inv.get(c, c)
        if oc not in rawi.columns:
            continue
        a = rawi.loc[cl.index, oc].astype(str)
        b = cl[c].astype(str)
        diff = a != b
        for r in a.index[diff]:
            k = (r, c)
            if k not in last:
                # a change before a rename is logged under the old name
                k2 = (r, oc)
                if k2 not in last:
                    unlogged.append({"row": int(r), "column": c, "original": a[r], "cleaned": b[r]})
                    continue
                k = k2
            if last[k] != b[r] or first[k] != a[r]:
                mismatched.append({"row": int(r), "column": c})
    ck.add("every changed value is in the change log", not unlogged,
           detail=f"{len(unlogged)} changed values have no log entry" if unlogged else
           f"{len(changes)} logged changes match the data", examples=unlogged[:10])
    ck.add("change log old/new values match the data", not mismatched,
           detail=f"{len(mismatched)} mismatches" if mismatched else "", examples=mismatched[:10])
    # 5. missing values introduced only by logged operations
    new_missing = 0
    for c in now:
        oc = inv.get(c, c)
        if oc in rawi.columns:
            became = (rawi.loc[cl.index, oc].astype(str).str.strip() != "") & (cl[c] == "")
            new_missing += int(became.sum())
    logged_missing = int(((changes["new_value"] == "") & (changes["old_value"].str.strip() != "")).sum()) if len(changes) else 0
    ck.add("no unexplained new missing values", new_missing <= logged_missing,
           detail=f"{new_missing} values became missing; {logged_missing} such changes logged")
    # 6. removed records are complete
    if len(removed):
        miss_cols = [c for c in orig_cols if c not in removed.columns]
        ck.add("removed records keep every original column", not miss_cols, detail=str(miss_cols) if miss_cols else "")
        sub = removed.set_index(removed[ROW_ID].astype(int))
        same = all((sub[c].astype(str) == rawi.loc[sub.index, c].astype(str)).all() for c in orig_cols if c in sub.columns)
        ck.add("removed records equal the original rows", same)
    # 7. duplicates
    body = cleaned.drop(columns=[ROW_ID] + [c for c in info["added_columns"] if c in cleaned.columns])
    rc_path = os.path.join(ad, "removed_columns.csv")
    if info["removed_columns"] and os.path.exists(rc_path):
        # duplicates are judged on every recorded column, including ones dropped after deduplication
        rcols = read_str_csv(rc_path).set_index(ROW_ID).rename(columns=renames)
        body = body.assign(**{c: rcols[c].reindex(cleaned[ROW_ID]).values for c in rcols.columns if c not in body.columns})
    dd = [e for e in log if e.get("op") == "drop_duplicates" and str(e.get("status", "")).startswith("applied")]
    dedup_ran = bool(dd)
    dups = 0
    for e in dd or [{}]:
        sub = (e.get("params") or {}).get("subset")
        key = body[[c for c in sub if c in body.columns]] if sub else body
        if (e.get("params") or {}).get("ignore_case"):
            key = key.apply(lambda s: s.str.casefold())
        dups = max(dups, int(key.duplicated().sum()))
    ck.add("no exact duplicate rows remain" if dedup_ran else "exact duplicate rows (deduplication not run)",
           dups == 0 or not dedup_ran, CRITICAL if dedup_ran else WARNING, detail=f"{dups} duplicates")
    # 8. types parse as declared
    tv = typed_view(cleaned, schema)
    bad_types = []
    for c, inf in schema["columns"].items():
        if c in cleaned.columns:
            lost = int(((cleaned[c] != "") & tv[c].isna()).sum())
            if lost:
                bad_types.append(f"{c}: {lost} values do not parse as {inf['type']}")
    ck.add("every value matches its declared type", not bad_types, detail="; ".join(bad_types))
    # 9. constraints from the plan
    for con in plan.get("constraints", []):
        c = con.get("column") or ", ".join(con.get("columns", []))
        if con["rule"] != "date_order" and c not in tv.columns:
            ck.add(f"constraint {con['rule']} on {c}", False, WARNING, detail="column not in cleaned data")
            continue
        if con["rule"] == "unique":
            v = tv[c].dropna()
            ck.add(f"{c} is unique", v.is_unique, detail=f"{int(v.duplicated().sum())} repeated values")
        elif con["rule"] == "range":
            x = pd.to_numeric(tv[c], errors="coerce")
            bad = int(((x < con["min"]) | (x > con["max"])).sum())
            ck.add(f"{c} within [{con['min']}, {con['max']}]", bad == 0, detail=f"{bad} out of range")
        elif con["rule"] == "not_null":
            ck.add(f"{c} has no missing values", int(tv[c].isna().sum()) == 0)
        elif con["rule"] == "allowed":
            from .common import canon_value
            present = cleaned[c][cleaned[c] != ""]
            bad = int((~present.isin([canon_value(v) for v in con["values"]])).sum())
            ck.add(f"{c} only has allowed values", bad == 0, detail=f"{bad} other values")
        elif con["rule"] == "date_order":
            a, b = con["columns"]
            if a not in tv.columns or b not in tv.columns:
                ck.add(f"{b} not before {a}", False, WARNING, detail="column not in cleaned data")
                continue
            bad = int((pd.to_datetime(tv[b], errors="coerce") < pd.to_datetime(tv[a], errors="coerce")).sum())
            ck.add(f"{b} not before {a}", bad == 0, con.get("severity", WARNING), detail=f"{bad} rows")
    # 9b. data contract: user or confirmed rules, checked before and after cleaning
    contract_res = None
    cpath = contract_path or os.path.join(out_dir, "contract.json")
    if os.path.exists(cpath):
        from .contracts import conflicts, evaluate, load_contract
        contract = load_contract(cpath)
        before_text = raw.drop(columns=[ROW_ID]).rename(columns=renames)
        before_tv = typed_view(before_text, schema)
        after = evaluate(contract, tv, cleaned, base_dir=os.path.dirname(contract["_path"]))
        before = {r["id"]: r for r in evaluate(contract, before_tv, before_text,
                                               base_dir=os.path.dirname(contract["_path"]))}
        for r in after:
            r["violations_before_cleaning"] = before.get(r["id"], {}).get("violations")
            ck.add(f"contract {r['id']} ({r['rule']}, {r['provenance']})", r["passed"],
                   CRITICAL if r["severity"] == "critical" else WARNING, detail=r["detail"])
        conf = conflicts(contract)
        for cf in conf:
            ck.add(f"contract rules {' and '.join(cf['rules'])} are consistent", False, WARNING,
                   detail=f"{cf['column']}: {cf['problem']} (not resolved automatically; decide which rule holds)")
        contract_res = {"file": contract["_path"], "rules": after, "conflicts": conf}
    # 10. imputation sanity: no missing left where imputation ran, variance shrinkage warning
    for e in log:
        if e.get("op") == "impute" and str(e.get("status", "")).startswith("applied"):
            c = e["params"]["column"]
            left = e.get("details", {}).get("missing_after", 0)
            final = int(tv[c].isna().sum()) if c in tv.columns else left
            method = e["params"].get("method")
            note = (" (no extrapolation at the ends)" if method in ("interpolate", "ffill", "bfill")
                    else " (no mapping matched)" if method == "lookup" else "")
            if left and not final:
                note = " after this step; a later step filled the rest"
            ck.add(f"imputation filled {c}", left == 0 or final == 0 or method in ("interpolate", "ffill", "bfill", "lookup"),
                   WARNING, detail=f"{left} still missing" + (note if left else ""))
            sb = e.get("statistical_basis", {})
            b, a = sb.get("distribution_before", {}), sb.get("distribution_after", {})
            if b.get("std") and a.get("std") is not None:
                shrink = 1 - a["std"] / b["std"]
                ck.add(f"imputation kept the spread of {c}", shrink < 0.2, WARNING,
                       detail=f"standard deviation changed by {-shrink * 100:.1f}%")
        if e.get("op") == "convert_currency" and str(e.get("status", "")).startswith("applied"):
            sb = e.get("statistical_basis", {})
            oc, rc = sb.get("output_column"), sb.get("rate_column")
            if oc in tv.columns and rc in tv.columns:
                amt, rate, got = (pd.to_numeric(tv[c], errors="coerce") for c in (e["column"], rc, oc))
                both = amt.notna() & rate.notna()
                tol = 0.5 * 10 ** -sb.get("decimals", 2) + 1e-9
                bad = int((both & ((amt * rate - got).abs() > tol)).sum() + (~both & got.notna()).sum())
                ck.add(f"{oc} equals {e['column']} x {rc}", bad == 0,
                       detail=f"{bad} rows differ" if bad else f"{int(both.sum())} converted rows recomputed")
        if e.get("status") == "failed":
            ck.add(f"operation {e['op_id']} ran", False, detail=e.get("error", ""))
    # 11. reversibility: rebuild the original from cleaned data + audit files
    try:
        rebuilt = revert(out_dir)
        eq, det = frames_equal(rebuilt, raw)
        ck.add("revert rebuilds the original exactly", eq, detail="" if eq else str(det))
    except Exception as exc:  # report, do not crash validation
        ck.add("revert rebuilds the original exactly", False, detail=f"{type(exc).__name__}: {exc}")
    # 12. reproducibility: replay the plan and compare hashes
    if replay:
        from .pipeline import run_clean
        tmp = tempfile.mkdtemp(prefix="dm_replay_")
        try:
            run_clean(load_from, os.path.join(out_dir, "plan.json"), tmp, lo, write_reference=False, quiet=True,
                      max_risk=info.get("max_risk"))
            h1 = sha256_file(os.path.join(out_dir, "cleaned_data", "cleaned_dataset.csv"))
            h2 = sha256_file(os.path.join(tmp, "cleaned_data", "cleaned_dataset.csv"))
            ck.add("replaying the plan reproduces the cleaned data", h1 == h2, detail=f"{h1[:12]} vs {h2[:12]}")
        except Exception as exc:
            ck.add("replaying the plan reproduces the cleaned data", False, detail=f"{type(exc).__name__}: {exc}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    if os.path.exists(os.path.join(ad, "quarantined_records.csv")):
        q = read_str_csv(os.path.join(ad, "quarantined_records.csv"))
        ck.add("quarantined records are kept with their original values", len(q) == int(
            (removed.get("dm_disposition", pd.Series(dtype=str)) == "quarantined").sum()), detail=f"{len(q)} rows")
    crit = [c for c in ck.items if not c["passed"] and c["severity"] == CRITICAL]
    warn = [c for c in ck.items if not c["passed"] and c["severity"] != CRITICAL]
    status = "FAILED" if crit else "PASSED WITH WARNINGS" if warn else "PASSED"
    review = [o["op_id"] for o in plan["operations"] if o.get("needs_review") and not o.get("enabled")]
    result = {"status": status, "critical_failures": len(crit), "warnings": len(warn), "checks": ck.items,
              "open_review_items": review,
              "quality_dimensions": quality_dimensions(raw, cleaned, info, ck.items, contract_res, out_dir),
              **({"contract": contract_res} if contract_res else {}),
              "note": "Disabled operations marked needs_review are decisions still open; they are listed, not failures."}
    write_json(os.path.join(out_dir, "audit", "validation_results.json"), result)
    return result


def quality_dimensions(raw, cleaned, info, checks, contract_res, out_dir):
    """Separate measures per quality dimension, before and after cleaning. There is deliberately no single score:
    one number would let a serious problem in one dimension hide behind good results in the others."""
    added = [c for c in info["added_columns"] if c in cleaned.columns]
    body0 = raw.drop(columns=[ROW_ID])
    body1 = cleaned.drop(columns=[ROW_ID] + added)
    cells0, cells1 = body0.size, body1.size

    def blank(df):
        return int((df.apply(lambda s: s.astype(str).str.strip()) == "").values.sum())

    flags_path = os.path.join(out_dir, "audit", "flagged_records.csv")
    flags = read_str_csv(flags_path) if os.path.exists(flags_path) else pd.DataFrame()
    logic = int(flags[flags["classification"] == "logical inconsistency"][ROW_ID].nunique()) if len(flags) else 0
    validity = [c for c in checks if c["check"].startswith(("contract ", "every value matches")) or
                c["check"].endswith(("is unique", "has no missing values", "only has allowed values")) or " within [" in c["check"]]
    integrity = [c for c in checks if c["check"].startswith(("revert", "replaying", "row accounting", "column accounting",
                                                             "every changed value", "change log", "original file"))]
    def prof_missing(name, fallback, skip=()):
        p = os.path.join(out_dir, "profiling", name)
        if not os.path.exists(p):
            return fallback
        return int(sum(c["missing_count"] for c in read_json(p)["columns_profile"] if c["column"] not in skip))

    return {
        "completeness": {"missing_cells_before": prof_missing("initial_profile.json", blank(body0)),
                         "cells_before": int(cells0),
                         "missing_cells_after": prof_missing("final_profile.json", blank(body1), set(added)),
                         "cells_after": int(cells1),
                         "note": "missing includes placeholder text such as NULL or N/A; after cleaning excludes the "
                                 "_was_missing indicator columns"},
        "uniqueness": {"exact_duplicate_rows_before": int(body0.duplicated().sum()),
                       "exact_duplicate_rows_after": int(body1.duplicated().sum())},
        "validity": {"checks": len(validity), "passed": sum(c["passed"] for c in validity),
                     "contract_rules": len(contract_res["rules"]) if contract_res else 0,
                     "contract_violations_before": sum((r.get("violations_before_cleaning") or 0)
                                                       for r in contract_res["rules"]) if contract_res else None,
                     "contract_violations_after": sum((r.get("violations") or 0) for r in contract_res["rules"])
                     if contract_res else None},
        "consistency": {"rows_flagged_logically_inconsistent": logic,
                        "note": "flagged rows keep their values; each needs a decision"},
        "integrity": {"checks": len(integrity), "passed": sum(c["passed"] for c in integrity)},
        "accuracy": {"status": "unverified",
                     "note": "accuracy needs an external reference (a trusted source or ground truth); passing the "
                             "checks above shows consistency, not correctness"},
    }
