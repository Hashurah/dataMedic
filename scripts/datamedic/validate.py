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


def validate(out_dir, replay=True):
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
    dups = int(body.duplicated().sum())
    dedup_ran = any(e.get("op") == "drop_duplicates" and str(e.get("status", "")).startswith("applied") for e in log)
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
    # 10. imputation sanity: no missing left where imputation ran, variance shrinkage warning
    for e in log:
        if e.get("op") == "impute" and str(e.get("status", "")).startswith("applied"):
            c = e["params"]["column"]
            left = e.get("details", {}).get("missing_after", 0)
            ck.add(f"imputation filled {c}", left == 0 or e["params"].get("method") in ("interpolate", "ffill", "bfill"),
                   WARNING, detail=f"{left} still missing" + (" (no extrapolation at the ends)" if left else ""))
            sb = e.get("statistical_basis", {})
            b, a = sb.get("distribution_before", {}), sb.get("distribution_after", {})
            if b.get("std") and a.get("std") is not None:
                shrink = 1 - a["std"] / b["std"]
                ck.add(f"imputation kept the spread of {c}", shrink < 0.2, WARNING,
                       detail=f"standard deviation changed by {-shrink * 100:.1f}%")
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
            run_clean(load_from, os.path.join(out_dir, "plan.json"), tmp, lo, write_reference=False, quiet=True)
            h1 = sha256_file(os.path.join(out_dir, "cleaned_data", "cleaned_dataset.csv"))
            h2 = sha256_file(os.path.join(tmp, "cleaned_data", "cleaned_dataset.csv"))
            ck.add("replaying the plan reproduces the cleaned data", h1 == h2, detail=f"{h1[:12]} vs {h2[:12]}")
        except Exception as exc:
            ck.add("replaying the plan reproduces the cleaned data", False, detail=f"{type(exc).__name__}: {exc}")
        finally:
            shutil.rmtree(tmp, ignore_errors=True)
    crit = [c for c in ck.items if not c["passed"] and c["severity"] == CRITICAL]
    warn = [c for c in ck.items if not c["passed"] and c["severity"] != CRITICAL]
    status = "FAILED" if crit else "PASSED WITH WARNINGS" if warn else "PASSED"
    review = [o["op_id"] for o in plan["operations"] if o.get("needs_review") and not o.get("enabled")]
    result = {"status": status, "critical_failures": len(crit), "warnings": len(warn), "checks": ck.items,
              "open_review_items": review,
              "note": "Disabled operations marked needs_review are decisions still open; they are listed, not failures."}
    write_json(os.path.join(out_dir, "audit", "validation_results.json"), result)
    return result
