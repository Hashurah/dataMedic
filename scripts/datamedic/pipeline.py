"""Stage orchestration: output folders, the manifest, and the profile and clean stages."""

import os
import platform
import shutil
import stat

from . import __version__
from .clean import Engine, write_outputs
from .common import ROW_ID, DataMedicError, load_config, now_iso, optional, read_json, sha256_file, write_json
from .loader import load_table
from .planner import build_plan
from .profile import profile_table

STAGES = ["discover", "profile", "understand", "diagnose", "plan", "clean", "audit", "validate", "explore",
          "interpret", "report", "final_review"]


def versions():
    out = {"python": platform.python_version(), "platform": platform.platform(), "datamedic": __version__}
    for m in ("pandas", "numpy", "scipy", "matplotlib", "sklearn", "openpyxl", "pyarrow"):
        mod = optional(m)
        out[m] = getattr(mod, "__version__", "installed") if mod else "not installed"
    return out


def manifest_path(out_dir):
    return os.path.join(out_dir, "metadata", "analysis_manifest.json")


def load_manifest(out_dir):
    p = manifest_path(out_dir)
    if not os.path.exists(p):
        raise DataMedicError(f"No dataMedic run found in {out_dir} (missing metadata/analysis_manifest.json). "
                             "Run 'profile' first.")
    return read_json(p)


def mark(out_dir, stage, status="done", note=""):
    man = load_manifest(out_dir)
    man.setdefault("stages", {})[stage] = {"status": status, "at": now_iso(), **({"note": note} if note else {})}
    write_json(manifest_path(out_dir), man)
    return man


def default_out(path):
    stem = os.path.splitext(os.path.basename(path))[0]
    return os.path.join(os.getcwd(), "datamedic_output", stem)


def _copy_reference(path, out_dir):
    dst_dir = os.path.join(out_dir, "original_reference")
    os.makedirs(dst_dir, exist_ok=True)
    dst = os.path.join(dst_dir, os.path.basename(path))
    if not os.path.exists(dst):
        shutil.copy2(path, dst)
        try:
            os.chmod(dst, stat.S_IREAD | stat.S_IRGRP | stat.S_IROTH)
        except OSError:
            pass
    return os.path.relpath(dst, out_dir)


def run_profile(path, out_dir, sheet=None, table=None, block=None, sample=None, cfg_path=None, force_plan=False):
    cfg = read_json(cfg_path) if cfg_path else load_config()
    seed = cfg.get("seed", 42)
    raw, meta = load_table(path, sheet=sheet, table=table, block=block, sample=sample, seed=seed)
    os.makedirs(out_dir, exist_ok=True)
    big = meta["size_bytes"] > cfg["large_data"]["warn_file_mb"] * 1_000_000
    ref = _copy_reference(path, out_dir) if not big else None
    prof = profile_table(raw, meta, cfg)
    write_json(os.path.join(out_dir, "profiling", "initial_profile.json"), prof)
    plan, issues = build_plan(raw, prof, meta, cfg)
    lo = {k: v for k, v in {"sheet": meta.get("sheet") if sheet else None, "table": table, "block": block,
                            "sample": sample, "seed": seed}.items() if v is not None}
    plan["load_options"] = lo
    plan_path = os.path.join(out_dir, "plan.json")
    if os.path.exists(plan_path) and not force_plan:
        old = read_json(plan_path)
        edited = any(o.get("decided_by") != "auto" for o in old.get("operations", []))
        if edited or old.get("dataset", {}).get("sha256") == meta["sha256"]:
            write_json(os.path.join(out_dir, "plan.proposed.json"), plan)
            plan_written = "plan.proposed.json (plan.json already exists and was kept)"
        else:
            write_json(plan_path, plan)
            plan_written = "plan.json"
    else:
        write_json(plan_path, plan)
        plan_written = "plan.json"
    write_json(os.path.join(out_dir, "profiling", "issues.json"), {"issues": issues})
    if meta.get("malformed_rows"):
        import pandas as pd
        os.makedirs(os.path.join(out_dir, "audit"), exist_ok=True)
        pd.DataFrame(meta["malformed_rows"]).to_csv(os.path.join(out_dir, "audit", "malformed_rows.csv"), index=False)
    man = {
        "tool": "dataMedic", "version": __version__, "created": now_iso(),
        "input": {"path": os.path.abspath(path), "file": meta["file"], "sha256": meta["sha256"],
                  "size_bytes": meta["size_bytes"], "format": meta["format"], "encoding": meta.get("encoding"),
                  "delimiter": meta.get("delimiter"), "sheet": meta.get("sheet"), "table": meta.get("table"),
                  "rows": meta["rows"], "columns": meta["columns"], "reference_copy": ref,
                  "reference_note": None if ref else "file is large; no copy made. The sha256 identifies the original."},
        "load_options": lo,
        "analysis_scope": {"sampled": bool(meta.get("sampled")), "rows_analysed": meta["rows"],
                           "rows_in_file": meta.get("rows_before_sampling", meta["rows"]),
                           "note": "sample-based; findings describe the sample" if meta.get("sampled") else "full dataset"},
        "warnings": meta.get("warnings", []),
        "software": versions(), "random_seed": seed,
        "stages": {s: {"status": "done", "at": now_iso()} for s in ("discover", "profile", "understand", "diagnose", "plan")},
    }
    if big:
        man["warnings"].append("Large file: no reference copy was made; the original must stay where it is.")
    write_json(manifest_path(out_dir), man)
    write_json(os.path.join(out_dir, "metadata", "reproducibility_config.json"), {
        "input_sha256": meta["sha256"], "load_options": lo, "seed": seed, "config": cfg,
        "commands": ["profile", "clean", "validate", "eda", "report", "review"], "software": versions()})
    return {"out_dir": out_dir, "profile": prof, "issues": issues, "plan": plan, "plan_file": plan_written, "meta": meta}


def run_clean(path, plan_path, out_dir, load_options=None, write_reference=True, quiet=False):
    plan = read_json(plan_path)
    lo = load_options if load_options is not None else plan.get("load_options", {})
    raw, meta = load_table(path, sheet=lo.get("sheet"), table=lo.get("table"), block=lo.get("block"),
                           sample=lo.get("sample"), seed=lo.get("seed", 42))
    want = plan.get("dataset", {}).get("sha256")
    if want and want != meta["sha256"]:
        raise DataMedicError("This plan was made for a different version of the file (sha256 mismatch). "
                             "Run 'profile' again on the current file.")
    os.makedirs(out_dir, exist_ok=True)
    if out_dir and not os.path.exists(os.path.join(out_dir, "plan.json")):
        shutil.copy2(plan_path, os.path.join(out_dir, "plan.json"))
    sensitive = plan.get("roles", {}).get("sensitive", [])
    eng = Engine(raw, plan, sensitive=sensitive).run()
    cleaned = write_outputs(eng, out_dir, meta)
    if quiet:
        return eng
    from .profile import profile_table as _pt
    final = _pt(cleaned, {**meta, "file": "cleaned_dataset.csv"})
    write_json(os.path.join(out_dir, "profiling", "final_profile.json"), final)
    man = load_manifest(out_dir)
    man["stages"]["clean"] = {"status": "done", "at": now_iso()}
    man["stages"]["audit"] = {"status": "done", "at": now_iso()}
    man["plan_sha256"] = sha256_file(plan_path)
    man["result"] = {"rows_before": int(len(raw)), "rows_after": int(len(eng.df)),
                     "columns_before": int(raw.shape[1] - 1), "columns_after": int(eng.df.shape[1] - 1),
                     "cells_changed": len(eng.changes), "rows_removed": len(eng.removed), "rows_flagged":
                         len({f[ROW_ID] for f in eng.flags}), "flags": len(eng.flags),
                     "operations_applied": sum(1 for e in eng.log if str(e.get("status", "")).startswith("applied")),
                     "operations_disabled": sum(1 for e in eng.log if "disabled" in str(e.get("status", ""))),
                     "operations_failed": sum(1 for e in eng.log if e.get("status") == "failed")}
    write_json(manifest_path(out_dir), man)
    return eng
