"""Final review: check deliverables exist and agree with the computed results.

- Every number written in the narrative reports must match a computed value (from the JSON
  results, or a derived calculation recorded with `dm.py calc`).
- Prose is checked for AI-writing tells (from the humanizer rules) and for causal or
  certain language the analysis cannot support.
"""

import ast
import math
import operator
import os
import re

from .common import DataMedicError, now_iso, read_json, write_json

NARRATIVE = ["reports/executive_summary.md", "reports/insights_and_recommendations.md"]
REQUIRED = ["cleaned_data/cleaned_dataset.csv", "cleaned_data/schema.json", "audit/transformation_log.json",
            "audit/transformation_log.csv", "audit/cell_changes.csv", "audit/removed_records.csv",
            "audit/flagged_records.csv", "audit/validation_results.json", "profiling/initial_profile.json",
            "profiling/final_profile.json", "analysis/eda_results.json", "reports/data_quality_report.md",
            "reports/exploratory_data_analysis.md", "reports/data_audit_report.html",
            "metadata/analysis_manifest.json", "metadata/reproducibility_config.json", "plan.json"] + NARRATIVE

STYLE = [
    # em dashes anywhere; en dashes only as connectors (spaced), since 25–34 is a correct range
    (r"—|\s–\s", "em or en dash; use a comma, colon, period or parentheses"),
    (r"(?<!-)--(?!-)", "double hyphen used as a dash"),
    (r"\bnot (just|only|merely)\b[^.]{0,80}\bbut\b", "'not just X but Y' contrast"),
    (r"\bit'?s not\b[^.]{0,60}[,;] it'?s\b", "'it's not X, it's Y' contrast"),
    (r"\b(delve|delves|delving|tapestry|testament|showcase|showcases|showcasing|underscore|underscores|pivotal|"
     r"vibrant|intricate|meticulous|meticulously|garner|bolstered|interplay|enduring)\b", "stock AI word"),
    (r"\b(crucial|key insights?|valuable insights?|deep dive)\b", "inflated wording"),
    (r"\b(let'?s dive|let'?s explore|here'?s what you need to know|without further ado|here'?s the thing)\b", "staged run-up"),
    (r"\b(in conclusion|in summary|overall,|ultimately,)\b", "summary closer (end on the last concrete fact)"),
    (r"\b(i hope this helps|let me know|feel free to|great question)\b", "chat residue"),
    (r"\b(additionally|furthermore|moreover),", "filler transition"),
    (r"\b(game[- ]changer|unlock|seamless(ly)?|cutting[- ]edge|world[- ]class|revolutionary)\b", "sales language"),
    (r"\bit is (important|worth) (to note|noting)\b", "throat-clearing"),
    (r"\b(plays a (key|crucial|vital) role|stands as|serves as)\b", "avoids plain 'is'"),
]
OVERCLAIM = [
    (r"\b(proves?|proven|confirms? that|demonstrates? that)\b", "certainty the data may not support"),
    (r"\b(caused|causes|causing|leads? to|drives?|driven by|results? in|because of|due to)\b",
     "causal wording; observational data shows association unless the design supports causation"),
    (r"\b(will (increase|decrease|grow|fall|rise|improve)|guarantees?)\b", "prediction stated as certain"),
]
SKIP_NUM = re.compile(r"^(19|20)\d{2}$")


def _numbers_from(obj, acc):
    if isinstance(obj, bool):
        return
    if isinstance(obj, (int, float)):
        if isinstance(obj, float) and not math.isfinite(obj):
            return
        acc.add(float(obj))
    elif isinstance(obj, dict):
        for v in obj.values():
            _numbers_from(v, acc)
    elif isinstance(obj, (list, tuple)):
        for v in obj:
            _numbers_from(v, acc)
    elif isinstance(obj, str):
        for m in re.findall(r"-?\d[\d,]*\.?\d*", obj):
            try:
                acc.add(float(m.replace(",", "")))
            except ValueError:
                pass


def evidence_numbers(out):
    acc = set()
    rels = ["audit/validation_results.json", "metadata/analysis_manifest.json", "profiling/initial_profile.json",
            "profiling/final_profile.json", "profiling/issues.json", "audit/transformation_log.json",
            "reports/summary_facts.json"]
    ad = os.path.join(out, "analysis")
    if os.path.isdir(ad):  # eda_results.json, derived.json and any extra analysis the agent saved there
        rels += ["analysis/" + f for f in sorted(os.listdir(ad)) if f.endswith(".json")]
    for rel in rels:
        p = os.path.join(out, rel)
        if os.path.exists(p):
            _numbers_from(read_json(p), acc)
    return acc


def _matches(x, raw, is_pct, pool):
    """True if some computed value rounds to the written number (sign ignored: 'fell 12%' vs -12)."""
    digits = raw.replace(",", "").lstrip("-")
    if "." in digits:
        tol = 0.5 * 10 ** (-len(digits.split(".")[1]))
    else:
        tz = len(digits) - len(digits.rstrip("0")) if abs(x) >= 1000 else 0
        tol = 0.5 * 10 ** tz
    ax = abs(x)
    for v in pool:
        av = abs(v)
        if abs(av - ax) <= tol + 1e-9:
            return True
        if is_pct and abs(av * 100 - ax) <= tol + 1e-9:
            return True
    return False


NUM_RE = re.compile(r"(?<![\w.#/-])(-?\d{1,3}(?:,\d{3})+(?:\.\d+)?|-?\d+(?:\.\d+)?)(\s?%| ?percent| ?pp| ?percentage points)?(?![\w/])")


def check_numbers(text, pool):
    bad = []
    for ln, line in enumerate(text.splitlines(), 1):
        s = line.strip()
        if s.startswith("#") or s.startswith("<!--"):
            continue
        clean = re.sub(r"`[^`]*`", "", line)
        clean = re.sub(r"\]\([^)]*\)", "]", clean)
        clean = re.sub(r"\b(OP|ISSUE|FLAG|C|Q)-?\d+\b", "", clean)
        clean = re.sub(r"^\s*\d+\.\s", "", clean)
        for m in NUM_RE.finditer(clean):
            raw = m.group(1)
            if SKIP_NUM.match(raw):
                continue
            val = float(raw.replace(",", ""))
            if "." not in raw and abs(val) <= 10:
                continue
            is_pct = bool(m.group(2))
            if not _matches(val, raw, is_pct, pool):
                bad.append({"line": ln, "number": raw + (m.group(2) or ""), "text": s[:160]})
    return bad


def check_prose(text, rules):
    out = []
    for ln, line in enumerate(text.splitlines(), 1):
        if line.strip().startswith(("|", "```", "    ")):
            continue
        body = re.sub(r"`[^`]*`", "", line)
        for rx, why in rules:
            for m in re.finditer(rx, body, flags=re.I):
                out.append({"line": ln, "found": m.group(0), "why": why, "text": line.strip()[:160]})
    return out


def review(out):
    problems, warnings = [], []
    missing = [f for f in REQUIRED if not os.path.exists(os.path.join(out, f))]
    for f in missing:
        (problems if f in NARRATIVE or not f.startswith("reports/") or f.endswith(".html") else warnings).append(
            {"check": "deliverable exists", "file": f})
    val_p = os.path.join(out, "audit", "validation_results.json")
    val = read_json(val_p) if os.path.exists(val_p) else None
    if val and val["status"] == "FAILED":
        problems.append({"check": "validation", "detail": "validation FAILED; the cleaned data must not be described as validated"})
    man = read_json(os.path.join(out, "metadata", "analysis_manifest.json"))
    pool = evidence_numbers(out)
    nums, prose, claims = {}, {}, {}
    for rel in NARRATIVE:
        p = os.path.join(out, rel)
        if not os.path.exists(p):
            continue
        with open(p, encoding="utf-8") as fh:
            text = fh.read()
        if "{{" in text or "TODO" in text:
            problems.append({"check": "template filled in", "file": rel, "detail": "placeholders remain"})
        nums[rel] = check_numbers(text, pool)
        prose[rel] = check_prose(text, STYLE)
        claims[rel] = check_prose(text, OVERCLAIM)
        for b in nums[rel]:
            problems.append({"check": "number traceable to results", "file": rel, **b})
        if val and val["status"] != "PASSED" and re.search(r"validation (status:?\s*)?(passed|succeeded)\b(?! with)", text, re.I):
            problems.append({"check": "validation status reported correctly", "file": rel,
                             "detail": f"text says passed but status is {val['status']}"})
    for rel, items in prose.items():
        for i in items:
            warnings.append({"check": "writing style", "file": rel, **i})
    for rel, items in claims.items():
        for i in items:
            warnings.append({"check": "claim strength", "file": rel, **i})
    stages = man.get("stages", {})
    status = "OK" if not problems else "PROBLEMS"
    res = {"status": status, "reviewed_at": now_iso(), "problems": problems, "warnings": warnings,
           "evidence_values_indexed": len(pool), "stages": stages,
           "note": "Problems must be fixed before reporting completion. Warnings need a look: keep a flagged "
                   "word only if it is accurate and plain."}
    write_json(os.path.join(out, "metadata", "final_review.json"), res)
    stages["final_review"] = {"status": "done" if not problems else "problems", "at": now_iso()}
    man["stages"] = stages
    write_json(os.path.join(out, "metadata", "analysis_manifest.json"), man)
    return res


# ---------------------------------------------------------------- derived calculations

_OPS = {ast.Add: operator.add, ast.Sub: operator.sub, ast.Mult: operator.mul, ast.Div: operator.truediv,
        ast.Pow: operator.pow, ast.USub: operator.neg, ast.UAdd: operator.pos}


def _eval(node):
    if isinstance(node, ast.Expression):
        return _eval(node.body)
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)):
        return node.value
    if isinstance(node, ast.BinOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.left), _eval(node.right))
    if isinstance(node, ast.UnaryOp) and type(node.op) in _OPS:
        return _OPS[type(node.op)](_eval(node.operand))
    raise DataMedicError("Only numbers and + - * / ** ( ) are allowed in a calculation.")


def record_calc(out, name, expression, source=""):
    """Evaluate arithmetic on numbers taken from the results and store it, so review can trace it."""
    value = float(_eval(ast.parse(expression, mode="eval")))
    p = os.path.join(out, "analysis", "derived.json")
    data = read_json(p) if os.path.exists(p) else {"calculations": []}
    data["calculations"] = [c for c in data["calculations"] if c["name"] != name]
    data["calculations"].append({"name": name, "expression": expression, "value": value, "source": source,
                                 "at": now_iso()})
    write_json(p, data)
    return value
