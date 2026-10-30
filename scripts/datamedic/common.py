"""Shared helpers: errors, canonical string form, hashing, JSON output, value parsing."""

import datetime as _dt
import hashlib
import json
import math
import os
import re
import unicodedata

import numpy as np
import pandas as pd

ROW_ID = "dm_row_id"
CONFIG_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))), "config")


class DataMedicError(Exception):
    """An error the user should see as a plain message (bad file, missing library, bad plan)."""


def load_config(name="default.json"):
    with open(os.path.join(CONFIG_DIR, name), encoding="utf-8") as fh:
        return json.load(fh)


def require(module, purpose):
    """Import an optional library or raise a readable error."""
    try:
        return __import__(module)
    except ImportError as exc:
        raise DataMedicError(f"{purpose} needs the Python package '{module}', which is not installed. "
                             f"Install it with: pip install {module}") from exc


def optional(module):
    try:
        return __import__(module)
    except ImportError:
        return None


# ---------------------------------------------------------------- hashing and JSON

def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def now_iso():
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0).isoformat()


def _json_default(obj):
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating,)):
        return None if not np.isfinite(obj) else float(obj)
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (pd.Timestamp, _dt.datetime, _dt.date)):
        return obj.isoformat()
    if isinstance(obj, np.ndarray):
        return obj.tolist()
    if obj is pd.NA or obj is pd.NaT:
        return None
    if isinstance(obj, set):
        return sorted(obj)
    return str(obj)


def clean_json(obj):
    """Replace NaN/inf floats with None so the output is valid JSON."""
    if isinstance(obj, dict):
        return {str(k): clean_json(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [clean_json(v) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    if isinstance(obj, np.floating):
        return None if not np.isfinite(obj) else float(obj)
    return obj


def write_json(path, data):
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(clean_json(data), fh, indent=2, ensure_ascii=False, default=_json_default)
        fh.write("\n")


def read_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


# ---------------------------------------------------------------- canonical strings
# Every table is compared and stored as canonical strings, so a change is "the canonical
# string of a cell changed". Missing is "". This makes the change log exact and reversible.

def canon_value(v):
    if v is None or v is pd.NA or v is pd.NaT:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, (bool, np.bool_)):
        return "true" if v else "false"
    if isinstance(v, (int, np.integer)):
        return str(int(v))
    if isinstance(v, (float, np.floating)):
        f = float(v)
        if math.isnan(f):
            return ""
        if math.isinf(f):
            return "inf" if f > 0 else "-inf"
        if f.is_integer() and abs(f) < 1e15:
            return str(int(f))
        return repr(f)
    if isinstance(v, (pd.Timestamp, _dt.datetime)):
        ts = pd.Timestamp(v)
        if ts.tzinfo is None and ts.hour == 0 and ts.minute == 0 and ts.second == 0 and ts.microsecond == 0:
            return ts.strftime("%Y-%m-%d")
        return ts.isoformat(sep=" ")
    if isinstance(v, _dt.date):
        return v.isoformat()
    if isinstance(v, _dt.time):
        return v.isoformat()
    return str(v)


def canon_series(s):
    """Canonical strings for a Series, keeping the index."""
    if pd.api.types.is_datetime64_any_dtype(s):
        has_time = False
        nn = s.dropna()
        if len(nn):
            tz = getattr(s.dt, "tz", None)
            has_time = tz is not None or bool(((nn.dt.hour != 0) | (nn.dt.minute != 0) | (nn.dt.second != 0)
                                                | (nn.dt.microsecond != 0)).any())
        if has_time:
            out = s.map(lambda x: "" if pd.isna(x) else pd.Timestamp(x).isoformat(sep=" "))
        else:
            out = s.dt.strftime("%Y-%m-%d").fillna("")
        return out.astype(object)
    return s.map(canon_value).astype(object)


def canon_frame(df):
    return pd.DataFrame({c: canon_series(df[c]) for c in df.columns}, index=df.index)


# ---------------------------------------------------------------- text helpers

def norm_label(s):
    """Normalisation key for category variants: unicode NFKC, casefold, trim, collapse spaces."""
    s = unicodedata.normalize("NFKC", str(s))
    s = re.sub(r"\s+", " ", s).strip().casefold()
    return s


def mask_value(v, keep=2):
    """Mask a sensitive value for reports: keep the first characters and the shape."""
    s = str(v)
    if not s:
        return s
    if "@" in s:
        user, _, domain = s.partition("@")
        return (user[:1] + "***@" + domain[:1] + "***") if user else "***"
    if len(s) <= keep:
        return "*" * len(s)
    return s[:keep] + "*" * min(6, len(s) - keep)


# ---------------------------------------------------------------- number parsing

CURRENCY_RE = re.compile(r"^(?:[$€£¥₦₹₩₽¢]|USD|EUR|GBP|NGN|JPY|CAD|AUD|INR|CHF)\s*|\s*(?:[$€£¥₦₹₩₽¢]|USD|EUR|GBP|NGN|JPY|CAD|AUD|INR|CHF)$", re.I)
_DOT_DEC = re.compile(r"^[+-]?(\d{1,3}(,\d{3})+|\d+)(\.\d+)?$")
_COMMA_DEC = re.compile(r"^[+-]?(\d{1,3}(\.\d{3})+|\d+)(,\d+)?$")
_PLAIN = re.compile(r"^[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?$")


def split_number_text(raw):
    """Strip whitespace, currency and percent markers. Returns (core, flags) or (None, None)."""
    s = str(raw).strip()
    if not s:
        return None, None
    flags = set()
    neg = False
    if s.startswith("(") and s.endswith(")"):
        neg, s = True, s[1:-1].strip()
    if s.endswith("%"):
        flags.add("percent")
        s = s[:-1].strip()
    stripped = CURRENCY_RE.sub("", s)
    if stripped != s:
        flags.add("currency")
        s = stripped.strip()
    if s.startswith(("-", "+")) and len(s) > 1 and not s[1].isdigit() and s[1] != ".":
        return None, None
    s = s.replace(" ", "").replace(" ", "").replace("'", "") if re.search(r"\d[\s ']\d{3}", s) else s
    if neg:
        flags.add("parentheses")
        s = "-" + s
    return s, flags


def parse_number(raw, decimal="."):
    """Parse one value. decimal is '.' or ','. Returns float or None."""
    s, flags = split_number_text(raw)
    if s is None:
        return None
    if _PLAIN.match(s) and not (decimal == "," and "." in s and not re.search(r"[eE]", s)
                                 and re.match(r"^[+-]?\d{1,3}(\.\d{3})+$", s)):
        try:
            return float(s)
        except ValueError:
            return None
    if decimal == "." and _DOT_DEC.match(s):
        return float(s.replace(",", ""))
    if decimal == "," and _COMMA_DEC.match(s):
        return float(s.replace(".", "").replace(",", "."))
    return None


def decimal_evidence(values):
    """Count values that are only readable with a decimal comma vs a decimal point."""
    comma = dot = 0
    for v in values:
        s, _ = split_number_text(v)
        if s is None:
            continue
        if re.match(r"^[+-]?\d+,\d{1,2}$", s) or re.match(r"^[+-]?\d{1,3}(\.\d{3})+,\d+$", s) \
                or re.match(r"^[+-]?\d{1,3}(\.\d{3}){2,}$", s):
            comma += 1
        elif re.match(r"^[+-]?\d+\.\d{1,2}$", s) or re.match(r"^[+-]?\d{1,3}(,\d{3})+\.\d+$", s) \
                or re.match(r"^[+-]?\d{1,3}(,\d{3}){2,}$", s):
            dot += 1
    return comma, dot


BOOL_TRUE = {"true", "t", "yes", "y"}
BOOL_FALSE = {"false", "f", "no", "n"}


def parse_bool(raw):
    s = str(raw).strip().casefold()
    if s in BOOL_TRUE or s == "1":
        return True
    if s in BOOL_FALSE or s == "0":
        return False
    return None


# ---------------------------------------------------------------- date parsing

DATE_FORMATS_DMY = ["%d/%m/%Y", "%d-%m-%Y", "%d.%m.%Y", "%d/%m/%y", "%d-%m-%y"]
DATE_FORMATS_MDY = ["%m/%d/%Y", "%m-%d-%Y", "%m/%d/%y", "%m-%d-%y"]
DATE_FORMATS_OTHER = ["%Y/%m/%d", "%Y.%m.%d", "%d %b %Y", "%d %B %Y", "%b %d %Y", "%B %d %Y", "%b %d, %Y",
                      "%B %d, %Y", "%d-%b-%Y", "%d-%b-%y", "%Y%m%d", "%b %Y", "%B %Y"]
TIME_SUFFIXES = ["", " %H:%M", " %H:%M:%S", " %I:%M %p", " %I:%M:%S %p", "T%H:%M", "T%H:%M:%S"]
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2}(\.\d+)?)?)?(Z|[+-]\d{2}:?\d{2})?$")
_DATEISH = re.compile(r"^\s*(\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}|\d{1,2}[ -][A-Za-z]{3,9}[ -,]+\d{2,4}|[A-Za-z]{3,9}\.? \d{1,2},? \d{4}|\d{8}|[A-Za-z]{3,9} \d{4})")


def _strptime(s, fmt):
    try:
        return _dt.datetime.strptime(s, fmt)
    except ValueError:
        return None


def date_candidates(raw, allow_compact=False):
    """Return {'iso'|'dmy'|'mdy'|'other': datetime} for every reading of one value."""
    s = re.sub(r"\s+", " ", str(raw).strip())
    if not s or not _DATEISH.match(s):
        return {}
    if ISO_RE.match(s):
        try:
            return {"iso": pd.Timestamp(s.replace("Z", "+00:00"))}
        except (ValueError, OverflowError):
            return {}
    out = {}
    for group, fmts in (("dmy", DATE_FORMATS_DMY), ("mdy", DATE_FORMATS_MDY), ("other", DATE_FORMATS_OTHER)):
        for f in fmts:
            if f == "%Y%m%d" and not allow_compact:
                continue
            for suf in TIME_SUFFIXES:
                d = _strptime(s.replace(",", ", ").replace(",  ", ", ") if "," in f else s, f + suf)
                if d is not None:
                    out.setdefault(group, d)
                    break
            if group in out:
                break
    return out


def parse_date(raw, order=None, allow_compact=False):
    """Parse with a column-level day/month decision. order: 'dmy', 'mdy' or None (unambiguous only)."""
    c = date_candidates(raw, allow_compact)
    if not c:
        return None
    for key in ("iso", "other"):
        if key in c:
            return c[key]
    if "dmy" in c and "mdy" in c:
        if c["dmy"] == c["mdy"]:
            return c["dmy"]
        return c.get(order) if order in ("dmy", "mdy") else None
    return c.get("dmy") or c.get("mdy")


def date_order_evidence(values, allow_compact=False):
    dmy = mdy = ambiguous = 0
    for v in values:
        c = date_candidates(v, allow_compact)
        if "dmy" in c and "mdy" in c and c["dmy"] != c["mdy"]:
            ambiguous += 1
        elif "dmy" in c and "mdy" not in c:
            dmy += 1
        elif "mdy" in c and "dmy" not in c:
            mdy += 1
    return dmy, mdy, ambiguous


def fmt_num(x, digits=4):
    """Short human formatting for reports."""
    if x is None or (isinstance(x, float) and not math.isfinite(x)):
        return "n/a"
    if isinstance(x, (int, np.integer)) or (isinstance(x, float) and x.is_integer() and abs(x) < 1e12):
        return f"{int(x):,}"
    ax = abs(x)
    if ax >= 1000:
        return f"{x:,.2f}"
    if ax >= 1:
        return f"{x:.{max(2, digits - 2)}f}".rstrip("0").rstrip(".")
    return f"{x:.{digits}g}"


def pct(n, d):
    return 0.0 if not d else round(100.0 * n / d, 2)
