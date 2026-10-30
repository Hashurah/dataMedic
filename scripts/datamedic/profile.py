"""Profile a raw (all-text) table: infer each column's type and role, and compute baseline statistics."""

import datetime as _dt
import re

import pandas as pd

from .common import (ROW_ID, date_order_evidence, decimal_evidence, load_config, mask_value, norm_label,
                     parse_bool, parse_date, parse_number, pct, split_number_text)

CURRENCY_TOKEN = re.compile(r"[$€£¥₦₹₩₽¢]|\b(USD|EUR|GBP|NGN|JPY|CAD|AUD|INR|CHF)\b", re.I)
DATE_SHAPED = re.compile(r"^\s*(\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}|\d{1,2}[ ./-]?[A-Za-z]{3,9}[ ./-]?\d{2,4})(\s|T|$)")
MOJIBAKE = re.compile(r"Ã[\x80-\xbf]|Ã.|â€|Â |�")
EMAIL = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")
PHONE = re.compile(r"^\+?[\d\s().-]{7,20}$")
CARD = re.compile(r"^\d{4}[ -]?\d{4}[ -]?\d{4}[ -]?\d{1,7}$")
SSN = re.compile(r"^\d{3}-\d{2}-\d{4}$")
IPV4 = re.compile(r"^(\d{1,3}\.){3}\d{1,3}$")

DOMAINS = {
    "sales and retail": ["sales", "revenue", "order", "product", "sku", "quantity", "qty", "price", "discount",
                         "store", "invoice", "units", "category", "basket", "retail", "item"],
    "financial": ["account", "balance", "transaction", "debit", "credit", "interest", "loan", "payment", "ledger",
                  "currency", "fx", "portfolio", "asset", "liability", "expense", "budget", "fiscal"],
    "marketing": ["campaign", "channel", "click", "clicks", "impression", "impressions", "conversion", "ctr",
                  "cpc", "cpm", "spend", "utm", "lead", "leads", "ad", "ads", "source", "medium", "roas"],
    "customer behaviour": ["session", "visit", "churn", "tenure", "signup", "login", "retention", "event",
                           "page", "engagement", "subscription", "plan", "cohort", "user"],
    "healthcare": ["patient", "diagnosis", "admission", "discharge", "bmi", "blood", "heart", "glucose", "mrn",
                   "clinic", "hospital", "icd", "dose", "symptom", "pressure", "cholesterol", "readmitted"],
    "education": ["student", "grade", "course", "school", "exam", "gpa", "teacher", "enrollment", "class",
                  "semester", "attendance", "pupil"],
    "human resources": ["employee", "salary", "department", "hire", "termination", "attrition", "job", "manager",
                        "performance", "payroll", "headcount", "overtime"],
    "operational": ["machine", "downtime", "defect", "throughput", "shift", "sensor", "ticket", "incident", "sla",
                    "uptime", "batch", "line", "yield", "maintenance"],
    "logistics": ["shipment", "delivery", "carrier", "warehouse", "freight", "route", "tracking", "dispatch",
                  "eta", "ship", "shipping", "weight", "pallet"],
    "geospatial": ["lat", "latitude", "lon", "lng", "longitude", "geometry", "coordinates", "postcode", "zip",
                   "region", "country", "city", "state"],
    "survey and questionnaire": ["response", "respondent", "question", "likert", "satisfaction", "agree",
                                 "rating", "nps", "survey", "answer", "q1", "q2", "q3"],
    "scientific and experimental": ["sample", "trial", "treatment", "control", "measurement", "specimen",
                                    "experiment", "replicate", "concentration", "temperature", "ph", "assay"],
}


def name_tokens(name):
    s = re.sub(r"([a-z])([A-Z])", r"\1_\2", str(name))
    s = re.sub(r"[^0-9A-Za-z]+", "_", s).strip("_").lower()
    return s, [t for t in s.split("_") if t]


def name_has(name, words):
    joined, toks = name_tokens(name)
    for w in words:
        if "_" in w:
            if re.search(rf"(^|_){re.escape(w)}($|_)", joined):
                return w
        elif w in toks:
            return w
    return None


def _value_counts(stripped):
    vc = stripped.value_counts(dropna=False)
    return vc


def is_sensitive(name, values, rules):
    if name_has(name, rules["sensitive_name_exclusions"]):
        joined, _ = name_tokens(name)
        if joined in rules["sensitive_name_exclusions"]:
            return None
    hit = name_has(name, rules["sensitive_name_words"])
    if hit:
        joined, toks = name_tokens(name)
        if hit == "name" and any(t in toks for t in ("product", "company", "category", "store", "brand", "item",
                                                     "region", "campaign", "channel", "file", "city", "country",
                                                     "department", "sheet", "team", "project", "event")):
            return None
        return f"column name contains '{hit}'"
    sample = [v for v in values[:500] if v]
    if not sample:
        return None
    for label, rx in (("email addresses", EMAIL), ("card-number-like values", CARD), ("SSN-like values", SSN),
                      ("IP addresses", IPV4)):
        share = sum(bool(rx.match(v)) for v in sample) / len(sample)
        if share >= 0.5:
            return f"values look like {label}"
    return None


def infer_column(name, raw, cfg, rules):
    """raw: Series of strings. Returns a profile dict for the column."""
    n = len(raw)
    stripped = raw.astype(str).str.strip()
    tokens_always = set(cfg["missing_tokens"]["always"])
    tokens_numeric = set(cfg["missing_tokens"]["in_numeric_or_date_columns"])
    tokens_text = set(cfg["missing_tokens"]["review_in_text_columns"])
    blank = stripped == ""
    always = stripped.isin(tokens_always) & ~blank
    missing = blank | always
    present = stripped[~missing]
    numeric_tokens = present.isin(tokens_numeric)
    base = present[~numeric_tokens]
    uniq = base.unique()
    ti = cfg["type_inference"]

    prof = {"column": name, "rows": n, "missing_count": int(missing.sum()), "blank_count": int(blank.sum()),
            "missing_pct": pct(int(missing.sum()), n), "non_null_count": int(n - missing.sum()),
            "missing_tokens_found": {k: int(v) for k, v in stripped[always].value_counts().items()}}
    prof["unique_count"] = int(present.nunique())
    ws = raw.astype(str)
    prof["whitespace_values"] = int(((ws != ws.str.strip()) & ~blank).sum())
    prof["repeated_internal_spaces"] = int(present.str.contains(r"\s{2,}", regex=True).sum())
    prof["mojibake_values"] = int(present.str.contains(MOJIBAKE, regex=True).sum())

    # numbers
    comma_ev, dot_ev = decimal_evidence(uniq[:5000])
    decimal = "," if comma_ev > dot_ev else "."
    num_map = {u: parse_number(u, decimal) for u in uniq}
    num_ok = base.map(lambda v: num_map.get(v) is not None)
    num_share = float(num_ok.mean()) if len(base) else 0.0
    flags = set()
    currencies = {}
    for u in uniq[:5000]:
        cm = CURRENCY_TOKEN.search(str(u).strip())
        if cm and re.search(r"\d", str(u)):
            cur = cm.group(0).strip().upper()
            currencies[cur] = currencies.get(cur, 0) + int((base == u).sum())
        if num_map.get(u) is not None:
            s, f = split_number_text(u)
            flags |= f or set()
            if re.search(r"\d[,.]\d{3}([,.]|$)", str(u).strip()):
                flags.add("thousands separators")
    pct_values = int(base.str.strip().str.endswith("%").sum()) if len(base) else 0
    # dates
    is_dateish_name = bool(name_has(name, ["date", "time", "timestamp", "dt", "day", "created", "updated", "dob",
                                            "birth", "at", "on", "when", "period", "month"]))
    dmy, mdy, amb = date_order_evidence(uniq[:5000], allow_compact=is_dateish_name)
    order = "dmy" if dmy > mdy else "mdy" if mdy > dmy else None
    date_map = {u: parse_date(u, order or "mdy", allow_compact=is_dateish_name) for u in uniq} if len(uniq) else {}
    date_ok = base.map(lambda v: date_map.get(v) is not None)
    date_share = float(date_ok.mean()) if len(base) else 0.0
    # booleans
    bool_vals = {u: parse_bool(u) for u in uniq}
    bool_share = float(base.map(lambda v: bool_vals.get(v) is not None).mean()) if len(base) else 0.0
    lower_set = {str(u).strip().casefold() for u in uniq}

    ident_word = name_has(name, rules["identifier_words"])
    # 'id', 'uuid', 'sku' ... name an identifier even when values repeat (a foreign key such as customer_id)
    strong_id = bool(name_has(name, ["id", "uuid", "guid", "sku", "key"])) or str(name).lower().endswith("id")
    unique_share = prof["unique_count"] / max(1, prof["non_null_count"])
    leading_zero = bool(base.str.match(r"^0\d+$").mean() > 0.05) if len(base) else False
    always_string = bool(name_has(name, ["zip", "postcode", "postal", "phone", "mobile", "tel", "iban", "isbn",
                                         "ean", "upc", "ssn", "account", "acct"]))

    ptype = "text"
    if prof["non_null_count"] == 0:
        ptype = "empty"
    elif bool_share >= 0.999 and len(lower_set) <= 2 and not lower_set <= {"0", "1"}:
        ptype = "boolean"
    elif lower_set <= {"0", "1"} and len(base):
        ptype = "binary"
    elif num_share >= ti["parse_share_for_conversion"] and (num_share >= date_share or not is_dateish_name):
        ptype = "integer" if all(float(num_map[u]).is_integer() for u in uniq if num_map.get(u) is not None) else "float"
    elif date_share >= ti["parse_share_for_conversion"] or (
            date_share >= 0.8 and base[~date_ok].map(lambda v: bool(DATE_SHAPED.match(v))).all()):
        # date-shaped values that do not parse (2024-13-45) are invalid dates, not a sign of a text column
        has_time = any(isinstance(d, (pd.Timestamp, _dt.datetime)) and (d.hour or d.minute or d.second)
                       for d in list(date_map.values())[:5000] if d is not None)
        ptype = "datetime" if has_time else "date"
    elif max(num_share, date_share) >= 0.5:
        ptype = "mixed"
    if ptype in ("integer", "float") and (leading_zero or always_string or
                                          (ptype == "integer" and (strong_id or (
                                              ident_word and unique_share >= ti["identifier_min_unique_share"])))):
        prof["numeric_looking_identifier"] = True
        ptype = "identifier"

    if ptype == "text" and len(base) and base.head(500).map(lambda v: bool(EMAIL.match(v))).mean() >= 0.9:
        ptype = "identifier"  # email addresses identify people; they are not categories
    if ptype == "text":
        avg_len = float(base.str.len().mean()) if len(base) else 0.0
        prof["avg_length"] = round(avg_len, 1)
        no_spaces = bool(len(base)) and not base.str.contains(" ").any()
        if (ident_word or unique_share >= 0.999) and unique_share >= ti["identifier_min_unique_share"] \
                and avg_len < 40 and prof["non_null_count"] >= 10:
            ptype = "identifier"
        elif strong_id and no_spaces and avg_len < 40:
            ptype = "identifier"
        elif avg_len >= ti["free_text_min_avg_length"] and unique_share > 0.5:
            ptype = "free_text"
        elif unique_share <= ti["categorical_max_unique_share"] or prof["unique_count"] <= ti["categorical_max_levels"] \
                and unique_share < 0.9:
            ptype = "categorical"
        elif ident_word:
            ptype = "identifier"
    prof["inferred_type"] = ptype
    prof["parse_shares"] = {"number": round(num_share, 4), "date": round(date_share, 4), "boolean": round(bool_share, 4)}
    if ptype in ("integer", "float", "mixed"):
        prof["decimal_separator"] = decimal
        prof["number_formats"] = sorted(flags)
        prof["currencies"] = currencies
        prof["percent_sign_values"] = pct_values
    if ptype in ("date", "datetime") or (ptype == "mixed" and date_share >= num_share):
        prof["date_order_evidence"] = {"day_first_only": dmy, "month_first_only": mdy, "ambiguous": amb}
        prof["date_order"] = order
    prof["non_parsing_examples"] = []
    if ptype in ("integer", "float", "mixed", "date", "datetime"):
        okmap = num_map if ptype in ("integer", "float") or (ptype == "mixed" and num_share > date_share) else date_map
        bad = base[base.map(lambda v: okmap.get(v) is None)]
        prof["non_parsing_count"] = int(len(bad))
        prof["non_parsing_examples"] = [str(v) for v in bad.value_counts().index[:8]]
    # placeholder candidates that are not in the always-missing list
    cand_set = tokens_numeric if ptype in ("integer", "float", "date", "datetime", "mixed") else tokens_text
    cands = present[present.isin(cand_set)].value_counts()
    prof["placeholder_candidates"] = {str(k): int(v) for k, v in cands.items()}

    # statistics on parsed values
    if ptype in ("integer", "float", "binary"):
        vals = present.map(lambda v: parse_number(v, decimal)).dropna().astype(float)
        prof.update(numeric_stats(vals))
    elif ptype in ("date", "datetime"):
        vals = pd.to_datetime(present.map(lambda v: date_map.get(v)).dropna(), errors="coerce", utc=False)
        vals = vals.dropna()
        if len(vals):
            try:
                mn, mx = vals.min(), vals.max()
                prof.update({"min": str(mn), "max": str(mx), "span_days": int((pd.Timestamp(mx) - pd.Timestamp(mn)).days)})
            except (TypeError, ValueError):
                prof["warning"] = "mixed time-zone awareness; min/max not computed"
    vc = present.value_counts()
    if len(vc):
        prof["mode"] = str(vc.index[0])
        prof["mode_count"] = int(vc.iloc[0])
        prof["top_values"] = [{"value": str(k), "count": int(v), "pct": pct(int(v), len(present))}
                              for k, v in vc.head(10).items()]
    prof["examples"] = [str(v) for v in present.drop_duplicates().head(5)]

    # roles
    roles = []
    sens = is_sensitive(name, list(present.head(500)), rules)
    if sens:
        roles.append("sensitive")
        prof["sensitive_reason"] = sens
    if ptype == "identifier":
        roles.append("identifier")
    elif ptype in ("integer", "float"):
        roles.append("measure")
    elif ptype in ("categorical", "boolean", "binary"):
        roles.append("category")
    elif ptype in ("date", "datetime"):
        roles.append("time")
    elif ptype == "free_text":
        roles.append("text")
    if name_has(name, rules["target_words"]):
        roles.append("target_candidate")
    if name_has(name, rules["split_words"]) and lower_set & {"train", "test", "validation", "valid", "val", "holdout"}:
        roles.append("split")
    if name_has(name, ["lat", "latitude", "lon", "lng", "longitude", "country", "city", "state", "region",
                       "zip", "postcode", "postal", "address"]):
        roles.append("geo")
    prof["roles"] = roles
    if "sensitive" in roles:
        prof["examples"] = [mask_value(v) for v in prof["examples"]]
        prof["mode"] = mask_value(prof.get("mode", "")) if prof.get("mode") else prof.get("mode")
        for tv in prof.get("top_values", []):
            tv["value"] = mask_value(tv["value"])
        prof["non_parsing_examples"] = [mask_value(v) for v in prof["non_parsing_examples"]]
    return prof


def numeric_stats(vals):
    vals = pd.Series(vals, dtype=float).dropna()
    if not len(vals):
        return {}
    q1, med, q3 = vals.quantile([0.25, 0.5, 0.75]).tolist()
    mode = vals.mode()
    out = {"count": int(len(vals)), "min": float(vals.min()), "max": float(vals.max()), "mean": float(vals.mean()),
           "median": float(med), "mode_numeric": float(mode.iloc[0]) if len(mode) else None,
           "std": float(vals.std(ddof=1)) if len(vals) > 1 else 0.0, "q1": float(q1), "q3": float(q3),
           "iqr": float(q3 - q1), "zeros": int((vals == 0).sum()), "negatives": int((vals < 0).sum())}
    if len(vals) > 2 and vals.std() > 0:
        out["skewness"] = float(vals.skew())
        out["kurtosis"] = float(vals.kurt())
    return out


def infer_domain(profiles):
    scores = []
    names = [p["column"] for p in profiles]
    for dom, words in DOMAINS.items():
        hits = sorted({w for n in names for w in [name_has(n, words)] if w})
        if hits:
            scores.append({"domain": dom, "matched_terms": hits, "score": len(hits)})
    scores.sort(key=lambda d: -d["score"])
    has_time = any("time" in p["roles"] for p in profiles)
    has_measure = any("measure" in p["roles"] for p in profiles)
    structure = []
    if has_time and has_measure:
        structure.append("time-indexed measures (time-series analysis applies)")
    if any("geo" in p["roles"] for p in profiles):
        structure.append("geographic fields")
    if any("target_candidate" in p["roles"] for p in profiles):
        structure.append("possible outcome variable")
    top = scores[0] if scores else None
    conf = "none"
    if top:
        conf = "high" if top["score"] >= 4 and (len(scores) == 1 or top["score"] >= 2 * scores[1]["score"]) else \
            "medium" if top["score"] >= 2 else "low"
    return {"candidates": scores[:4], "structure": structure, "confidence": conf,
            "basis": "column names and column types only, never the file name; confirm with the user's context"}


def profile_table(df, meta, cfg=None, rules=None):
    cfg = cfg or load_config()
    rules = rules or load_config("domain_rules.json")
    cols = [c for c in df.columns if c != ROW_ID]
    profiles = [infer_column(c, df[c], cfg, rules) for c in cols]
    body = df[cols].astype(str)
    stripped = body.apply(lambda s: s.str.strip())
    empty_rows = int((stripped == "").all(axis=1).sum())
    exact_dupes = int(body.duplicated().sum())
    norm = stripped.apply(lambda s: s.map(norm_label))
    norm_dupes = int(norm.duplicated().sum())
    roles = {r: [p["column"] for p in profiles if r in p["roles"]] for r in
             ("identifier", "measure", "category", "time", "text", "target_candidate", "split", "geo", "sensitive")}
    return {
        "dataset": {k: meta.get(k) for k in ("file", "format", "size_bytes", "sha256", "encoding", "delimiter",
                                              "sheet", "table", "header_detected", "sampled", "rows_before_sampling")},
        "rows": int(len(df)), "columns": len(cols), "empty_rows": empty_rows,
        "exact_duplicate_rows": exact_dupes, "duplicates_after_trim_and_case": norm_dupes,
        "unique_rows": int(len(df) - exact_dupes),
        "malformed_rows": len(meta.get("malformed_rows", [])),
        "constant_columns": [p["column"] for p in profiles if p["unique_count"] == 1],
        "empty_columns": [p["column"] for p in profiles if p["inferred_type"] == "empty"],
        "roles": roles, "domain": infer_domain(profiles), "columns_profile": profiles,
    }
