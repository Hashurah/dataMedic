"""Diagnose data-quality issues and propose a cleaning plan.

The plan is a JSON file of operations. Each operation carries the issue it addresses, the
evidence, a rationale, and whether it is enabled. Safe, reversible fixes are enabled; anything
that depends on business meaning is proposed but disabled with needs_review = true, so the
agent (or user) decides with context. Nothing here changes data.
"""

import datetime as _dt
import difflib
import re

import numpy as np
import pandas as pd

from . import stats as st
from .clean import Engine, _is_missing, _num, op_risk, rule_mask
from .common import (ROW_ID, canon_frame, canon_series, fmt_num, load_config, merged_config, norm_label, optional, pct,
                     repair_mojibake)
from .impute_eval import better_than, candidates_for, compare_methods
from .missingness import diagnose
from .profile import name_has, name_tokens


class PlanBuilder:
    def __init__(self, raw, profile, meta, cfg=None, rules=None):
        self.raw, self.profile, self.meta = raw, profile, meta
        self.cfg = merged_config(cfg)
        self.rules = rules or load_config("domain_rules.json")
        self.ops, self.issues = [], []
        self.comparisons = {}
        self.extras = {}
        self.cols = {p["column"]: p for p in profile["columns_profile"]}

    # ------------------------------------------------------------ helpers
    def op(self, op, params, issue, rationale, enabled=True, needs_review=False, evidence=None, risk=None):
        o = {"op_id": f"OP-{len(self.ops) + 1:03d}", "op": op, "params": params, "issue": issue,
             "rationale": rationale, "enabled": enabled, "needs_review": needs_review,
             "evidence": evidence or {}, "decided_by": "auto"}
        o["risk"] = risk or op_risk(o)
        self.ops.append(o)
        return o

    def issue(self, kind, column, severity, detail, count=None, n=None, evidence=None, action=""):
        self.issues.append({"issue_id": f"ISSUE-{len(self.issues) + 1:03d}", "kind": kind, "column": column,
                            "severity": severity, "detail": detail, "count": count,
                            "pct": pct(count, n) if count is not None and n else None,
                            "evidence": evidence or {}, "planned_action": action})

    def role(self, c, r):
        p = self.cols.get(c)
        return bool(p and r in p["roles"])

    # ------------------------------------------------------------ stage 1: structure and types
    def stage1(self):
        n = self.profile["rows"]
        mapping = {}
        for c in self.cols:
            new = " ".join(str(c).split())
            if new != c and new not in self.cols and new not in mapping.values():
                mapping[c] = new
        if mapping:
            self.issue("column names", None, "low", "column names with extra spaces", len(mapping))
            self.op("rename_columns", {"mapping": mapping}, "column names with leading, trailing or repeated spaces",
                    "Extra spaces in headers cause lookup errors; the names are otherwise kept as given.")
        for orig, new in (self.meta.get("header_renames") or {}).items():
            self.issue("column names", new, "medium", f"header '{orig.split(':', 1)[1]}' was blank or duplicated; "
                       f"loaded as '{new}'")
        self.renamed = mapping
        if self.profile["empty_rows"]:
            self.issue("empty rows", None, "low", "rows with no values in any column", self.profile["empty_rows"], n)
            self.op("drop_rows", {"empty": True, "reason": "row has no values in any column"}, "completely empty rows",
                    "An empty row carries no information. Removed rows are kept in removed_records.csv.",
                    evidence={"rows": self.profile["empty_rows"]})
        if self.meta.get("malformed_rows"):
            k = len(self.meta["malformed_rows"])
            self.issue("corrupted records", None, "high", "lines with more fields than the header; kept out of the "
                       "table and saved for review", k, n + k)
        trim = [self.nc(c) for c, p in self.cols.items() if p["whitespace_values"]]
        collapse = [self.nc(c) for c, p in self.cols.items()
                    if p["repeated_internal_spaces"] and p["inferred_type"] in ("categorical", "identifier", "boolean")]
        if trim or collapse:
            for c, p in self.cols.items():
                if p["whitespace_values"]:
                    self.issue("whitespace", self.nc(c), "low", "values with leading or trailing spaces",
                               p["whitespace_values"], n)
            if trim:
                self.op("trim_whitespace", {"columns": trim}, "leading/trailing spaces",
                        "Surrounding spaces are formatting noise and split identical values into different ones.")
            if collapse:
                self.op("trim_whitespace", {"columns": collapse, "collapse_internal": True}, "repeated inner spaces",
                        "In category and code columns, repeated spaces split one label into variants.")
        self.collapse = set(collapse)
        for c, p in self.cols.items():
            col = self.nc(c)
            if p["mojibake_values"]:
                vals = self.raw[c].dropna().astype(str)
                fixable = vals[vals.map(repair_mojibake) != vals]
                self.issue("encoding", col, "medium", "values that look garbled by a wrong text encoding "
                           "(for example 'Ã©' for 'é')", p["mojibake_values"], n,
                           action=f"{len(fixable)} repaired exactly; re-export the source as UTF-8 if possible"
                           if len(fixable) else "flagged; re-export the source as UTF-8 if possible")
                if len(fixable):
                    self.op("fix_encoding", {"columns": [col]}, "text garbled by a wrong encoding",
                            "These values are UTF-8 text that was read as Windows-1252. Re-decoding them gives back the "
                            "original characters exactly; values that do not round-trip are left alone.",
                            evidence={"repairable": int(len(fixable)),
                                      "examples": [{"from": a, "to": repair_mojibake(a)} for a in fixable.unique()[:5]]})
            found = p["missing_tokens_found"]
            if found:
                self.op("standardize_missing", {"column": col, "tokens": sorted(found)}, "missing-value placeholders",
                        "These tokens are standard spellings of a missing value (NULL, NaN, #N/A ...).",
                        evidence={"counts": found})
            cands = {k: v for k, v in p["placeholder_candidates"].items() if k not in found}
            if cands:
                typed = p["inferred_type"] in ("integer", "float", "date", "datetime", "mixed", "boolean", "binary")
                if typed:
                    self.op("standardize_missing", {"column": col, "tokens": sorted(cands)}, "placeholder text in a "
                            f"{p['inferred_type']} column", "The column is otherwise "
                            f"{p['inferred_type']}; these words cannot be values of that type, so they mark "
                            "missing entries.", evidence={"counts": cands})
                else:
                    self.issue("possible placeholders", col, "medium",
                               "values that are sometimes used to mean 'missing' but can be real answers "
                               f"({', '.join(cands)})", sum(cands.values()), n, evidence=cands,
                               action="proposed as missing, disabled until reviewed")
                    self.op("standardize_missing", {"column": col, "tokens": sorted(cands)},
                            "possible missing-value placeholders", "In a text column words like 'Unknown', 'None' "
                            "or 'N/A' can be a legitimate answer (for example 'no allergies'). Enable only if they "
                            "mean the value was not recorded.", enabled=False, needs_review=True, evidence={"counts": cands})
            self.plan_type(col, p, n, c)
            if p["inferred_type"] == "categorical":
                self.plan_categories(col, c, p, n)

    def nc(self, c):
        return self.renamed.get(c, c) if hasattr(self, "renamed") else c

    def plan_type(self, col, p, n, orig):
        t = p["inferred_type"]
        cur = p.get("currencies") or {}
        if len(cur) > 1 and t in ("integer", "float", "mixed"):
            self.issue("units", col, "high", "amounts in several currencies (" + ", ".join(f"{k}: {v}" for k, v in cur.items())
                       + "); they cannot be compared or summed without exchange rates", sum(cur.values()), n,
                       evidence={"currencies": cur}, action="currency copied to its own column; number conversion and "
                       "conversion to one currency proposed, disabled until reviewed")
            cur_col = f"{col}_currency"
            ex = self.op("extract_currency", {"column": col, "to_column": cur_col},
                         "currency stored inside the amount text", "Copies each amount's currency marker into "
                         f"{cur_col} (values without one are labelled 'Unmarked'), so the amount can become a number "
                         "without losing it. '$' is read as USD; set params.symbols if the source uses another dollar.",
                         evidence={"currencies": cur})
            num = self.op("convert_type", {"column": col, "to": "number", "decimal": p.get("decimal_separator", ".")},
                    "numbers stored as text in several currencies", f"Safe to enable once {ex['op_id']} has kept the "
                    "currency. The column will still hold several currencies: analyse it per currency, or convert "
                    "with the convert_currency step.", enabled=False, needs_review=True,
                    evidence={"currencies": cur})
            num["depends_on"] = [ex["op_id"]]
            conv = self.op("convert_currency", {"column": col, "currency_column": cur_col, "rates_file": "rates.csv",
                                                "target": None, "date_column": None},
                           "amounts in several currencies", "Needs exchange rates from you: a CSV in the output folder "
                           "with currency,rate (and date, to use the rate of each row's date). Set target and, for "
                           "dated rates, date_column. Rows whose currency has no rate (such as 'Unmarked') stay "
                           "unconverted and are flagged.", enabled=False, needs_review=True,
                           evidence={"currencies": cur})
            conv["depends_on"] = [ex["op_id"], num["op_id"]]
            return
        pv = p.get("percent_sign_values", 0)
        if t in ("integer", "float") and 0 < pv < 0.5 * max(1, p["non_null_count"]):
            s = self.raw[orig].astype(str).str.strip()
            ids = self.raw.loc[s.str.endswith("%"), ROW_ID].astype(int).tolist()
            self.issue("units", col, "medium", f"{pv} values carry a % sign but most do not, so they may be on a "
                       "different scale (45% next to 0.45 or 45)", pv, n, action="% removed (45% becomes 45); rows flagged")
            self.op("flag_rows", {"column": col, "rule": {"row_ids": ids}, "reason": "value had a % sign; most values "
                    "in this column do not, so check its scale", "classification": "unit inconsistency"},
                    "mixed percent and plain numbers", "The % sign is removed during conversion, keeping the number. "
                    "Whether 45% means 45 or 0.45 here needs the column's definition, so the rows are flagged.")
        if t in ("integer", "float"):
            unit = "percent (% sign removed)" if pv >= 0.5 * max(1, p["non_null_count"]) else None
            fmts = p.get("number_formats", [])
            self.op("convert_type", {"column": col, "to": t if t == "integer" else "number",
                                     "decimal": p.get("decimal_separator", "."), **({"unit": unit} if unit else {})},
                    "numbers stored as text" + (f" ({', '.join(fmts)})" if fmts else ""),
                    f"{p['parse_shares']['number'] * 100:.1f}% of the non-missing values read as numbers.",
                    evidence={"non_parsing_examples": p["non_parsing_examples"], "decimal": p.get("decimal_separator")},
                    risk="moderate" if p.get("non_parsing_count") else "safe")
            if p.get("non_parsing_count"):
                self.issue("type", col, "medium", "values that are not numbers in a numeric column; they will be set "
                           "to missing and flagged", p["non_parsing_count"], n,
                           evidence={"examples": p["non_parsing_examples"]})
            if unit:
                self.issue("units", col, "low", "values carry a % sign; stored as percentage points (45% becomes 45)")
        elif t in ("date", "datetime"):
            ev = p.get("date_order_evidence", {})
            order = p.get("date_order")
            ambiguous = ev.get("ambiguous", 0)
            conflict = ev.get("day_first_only", 0) and ev.get("month_first_only", 0)
            allow_compact = bool(name_has(col, ["date", "dt", "day", "dob"]))
            params = {"column": col, "to": t, "date_order": order, "allow_compact": allow_compact}
            if conflict:
                self.issue("dates", col, "high", "the column mixes day-first and month-first dates "
                           f"({ev['day_first_only']} only readable day-first, {ev['month_first_only']} only month-first)",
                           evidence=ev, action="conversion proposed, disabled until reviewed")
                self.op("convert_type", params, "dates in conflicting formats",
                        "Day-first and month-first values both occur, which suggests merged sources. Decide the "
                        "order per source before converting.", enabled=False, needs_review=True, evidence=ev)
            elif ambiguous and not order:
                self.issue("dates", col, "high", f"{ambiguous} dates could be read day-first or month-first and "
                           "nothing in the column decides which", ambiguous, n, evidence=ev,
                           action="conversion proposed, disabled until the order is known")
                self.op("convert_type", params, "ambiguous day/month order",
                        "Values such as 03/04/2024 are ambiguous. Set params.date_order to 'dmy' or 'mdy' from the "
                        "data's origin, then enable.", enabled=False, needs_review=True, evidence=ev)
            else:
                why = "All values have one unambiguous reading." if not ambiguous else \
                    f"{ev.get('day_first_only' if order == 'dmy' else 'month_first_only')} values can only be read " \
                    f"{'day-first' if order == 'dmy' else 'month-first'}, so the {ambiguous} ambiguous values are read the same way."
                self.op("convert_type", params, "dates stored as text", why, evidence=ev,
                        risk="moderate" if p.get("non_parsing_count") or ambiguous else "safe")
                if p.get("non_parsing_count"):
                    self.issue("dates", col, "medium", "values that are not valid dates; set to missing and flagged",
                               p["non_parsing_count"], n, evidence={"examples": p["non_parsing_examples"]})
        elif t == "boolean":
            self.op("convert_type", {"column": col, "to": "boolean"}, "yes/no values stored as text",
                    "Every value is a recognised true/false spelling.")
        elif t == "mixed":
            self.issue("mixed types", col, "high", f"column is partly numeric/date "
                       f"({p['parse_shares']['number'] * 100:.0f}% numbers, {p['parse_shares']['date'] * 100:.0f}% dates) "
                       "and partly text", p.get("non_parsing_count"), n,
                       evidence={"examples": p["non_parsing_examples"]}, action="left as text; review")
        elif t == "identifier" and p.get("numeric_looking_identifier"):
            self.issue("identifiers", col, "info", "numeric-looking codes kept as text (leading zeros and codes "
                       "are not quantities)")

    def plan_categories(self, col, orig, p, n):
        s = self.raw[orig].astype(str).str.strip()
        if col in self.collapse:
            s = s.map(lambda v: " ".join(v.split()))
        s = s[s != ""]
        vc = s.value_counts()
        groups = {}
        for val, cnt in vc.items():
            groups.setdefault(norm_label(val), []).append((val, int(cnt)))
        mapping, examples = {}, []
        for key, vals in groups.items():
            if len(vals) > 1:
                canonical = sorted(vals, key=lambda t: (-t[1], t[0]))[0][0]
                for v, cnt in vals:
                    if v != canonical:
                        mapping[v] = canonical
                examples.append({"canonical": canonical, "variants": [v for v, _ in vals]})
        if mapping:
            self.issue("inconsistent labels", col, "medium", f"{len(examples)} labels written in several ways "
                       "(case, spacing or unicode differences)", sum(int(vc[v]) for v in mapping), n,
                       evidence={"groups": examples[:10]})
            self.op("standardize_categories", {"column": col, "mapping": mapping}, "inconsistent category labels",
                    "Variants differ only in case, spacing or unicode form; each is mapped to its most frequent spelling.",
                    evidence={"groups": examples[:20]})
        keys = list(groups)
        # misspellings are rare variants of a common label; codes, names and emails are not checked
        if 2 <= len(keys) <= 50 and "sensitive" not in p["roles"]:
            freq = {k: sum(c for _, c in groups[k]) for k in keys}
            sugg = {}
            for i, a in enumerate(keys):
                for b in keys[i + 1:]:
                    if min(len(a), len(b)) < 4:
                        continue
                    r = difflib.SequenceMatcher(None, a, b).ratio()
                    if r >= self.cfg["categories"]["fuzzy_similarity"]:
                        rare, common = (a, b) if freq[a] < freq[b] else (b, a)
                        if freq[rare] > 0.2 * freq[common]:
                            continue
                        rare_label = sorted(groups[rare], key=lambda t: -t[1])[0][0]
                        common_label = sorted(groups[common], key=lambda t: -t[1])[0][0]
                        sugg[rare_label] = common_label
            if sugg:
                self.issue("possible misspellings", col, "low", "labels that are very similar and may be the same "
                           "category: " + "; ".join(f"'{k}' ~ '{v}'" for k, v in list(sugg.items())[:5]),
                           len(sugg), action="merge proposed, disabled until reviewed")
                self.op("standardize_categories", {"column": col, "mapping": sugg}, "possible misspelled labels",
                        "Similar spelling alone does not prove two labels mean the same thing (for example "
                        "'Austria' and 'Australia'). Enable only after checking.", enabled=False, needs_review=True)

    # ------------------------------------------------------------ stage 2: values, duplicates, missing
    def stage2(self):
        eng = Engine(self.raw, {"operations": [o for o in self.ops], "settings": {}}).run()
        df = eng.df
        self.df1 = df
        n = len(df)
        rules = self.rules
        cfgo = self.cfg["outliers"]
        failed = [e for e in eng.log if e.get("status") == "failed"]
        for e in failed:
            self.issue("plan", e.get("column"), "high", f"{e['op_id']} could not run in the trial: {e.get('error')}")
        typed = {c: str(df[c].dtype) for c in df.columns}
        numeric = [c for c in df.columns if c != ROW_ID and (typed[c].startswith("float") or typed[c] == "Int64")]
        dates = [c for c in df.columns if typed[c].startswith("datetime")]
        self.numeric, self.dates = numeric, dates
        prof_of = {self.nc(k): v for k, v in self.cols.items()}
        self.prof_of = prof_of

        # empty, constant, near-constant, high cardinality
        empty_cols = [c for c in df.columns if c != ROW_ID and _is_missing(df[c]).all()]
        if empty_cols:
            for c in empty_cols:
                self.issue("empty column", c, "medium", "column has no values", n, n)
            self.op("drop_columns", {"columns": empty_cols, "reason": "column has no values"}, "completely empty columns",
                    "An empty column carries no information. Its original values (all blank) are kept in "
                    "removed_columns.csv.", evidence={"columns": empty_cols}, risk="safe")
        for c in df.columns:
            if c == ROW_ID or c in empty_cols:
                continue
            cs = canon_series(df[c])
            vals = cs[cs != ""]
            if not len(vals):
                continue
            top_share = vals.value_counts(normalize=True).iloc[0]
            nun = vals.nunique()
            if nun == 1:
                self.issue("constant column", c, "low", f"every value is '{vals.iloc[0] if c not in self.sens() else '***'}'",
                           action="kept; it adds nothing to analysis but may matter to the business")
            elif top_share >= self.cfg["categories"]["near_constant_share"]:
                self.issue("near-constant column", c, "info", f"{top_share * 100:.1f}% of values are the same")
            p = prof_of.get(c, {})
            if p.get("inferred_type") == "categorical" and nun > self.cfg["categories"]["high_cardinality_levels"]:
                self.issue("high cardinality", c, "info", f"{nun} distinct labels; group rare ones before charts or models")
            if p.get("inferred_type") == "categorical" and nun > 2:
                rare = vals.value_counts(normalize=True)
                rare = rare[rare < self.cfg["categories"]["rare_share"]]
                if len(rare):
                    self.issue("rare categories", c, "info", f"{len(rare)} labels each under "
                               f"{self.cfg['categories']['rare_share'] * 100:.0f}% of rows", int(len(rare)))

        # duplicates
        body = canon_frame(df.drop(columns=[ROW_ID]))
        exact = int(body.duplicated().sum())
        if exact:
            self.issue("duplicates", None, "high" if exact / max(1, n) > 0.01 else "medium",
                       "rows identical in every column (after the formatting fixes above)", exact, n,
                       action="removed; kept in removed_records.csv")
            self.op("drop_duplicates", {"reason": "exact duplicate of an earlier row", "keep": "first"},
                    "exact duplicate rows", "Identical in every column, so the copies add no information and would "
                    "double-count. The first occurrence is kept; each removed row records which row was kept.",
                    evidence={"duplicates": exact})
        ci = int(body.apply(lambda s: s.str.casefold()).duplicated().sum()) - exact
        if ci > 0:
            self.issue("possible duplicates", None, "low", "rows that differ only in letter case", ci, n,
                       action="flag proposed")
        ids = [c for c in df.columns if prof_of.get(c, {}).get("roles") and "identifier" in prof_of[c]["roles"]]
        nodup = body.drop_duplicates()
        # only near-unique identifiers are record keys; zip codes or customer ids repeat by design
        keys = [c for c in ids if "geo" not in prof_of[c]["roles"] and
                nodup[c][nodup[c] != ""].nunique() >= 0.9 * max(1, int((nodup[c] != "").sum()))]
        for c in keys:
            cs = canon_series(df[c])
            keyed = cs != ""
            dupk = cs[keyed].duplicated(keep=False)
            if dupk.any():
                k = int(cs[keyed][dupk].nunique())
                conflicting = int(nodup[c][nodup[c] != ""].duplicated().sum()) if c in nodup else 0
                sev = "high" if conflicting else "medium"
                self.issue("duplicate identifiers", c, sev, f"{k} identifier values appear on more than one row"
                           + (f"; {conflicting} of those rows have different contents" if conflicting else ""),
                           int(dupk.sum()), n, action="flagged for review; not removed")
                self.op("flag_rows", {"column": c, "rule": {"duplicate_key": [c]}, "reason": f"identifier '{c}' "
                        "appears on more than one row", "classification": "duplicate identifier"},
                        "duplicate identifiers", "The same ID with different details may be an update, a data entry "
                        "error or a one-to-many relationship. That needs business knowledge, so the rows are flagged.")
        others = [c for c in df.columns if c not in ids and c != ROW_ID and c not in empty_cols]
        detailed = [c for c in others if body[c][body[c] != ""].nunique() > 0.9 * n]
        if keys and len(others) >= 2 and detailed:
            # identical content only means something when it includes a detailed (high-cardinality) value
            near = max(0, int(df.loc[rule_mask(df, None, {"duplicate_key": others}), others].pipe(canon_frame)
                              .duplicated().sum()) - exact)
            if near > 0:
                self.issue("possible duplicates", None, "medium", "rows identical in every column except the "
                           f"identifier ({', '.join(ids)})", near, n, action="flagged")
                self.op("flag_rows", {"rule": {"duplicate_key": others}, "reason": "same content as another row "
                        "with a different identifier", "classification": "possible duplicate"},
                        "possible duplicate entries", "The same details under different IDs can be a double entry "
                        "or two genuine identical events; flagged, not removed.")

        # domain validity, outliers, sentinels
        year = _dt.date.today().year
        for c in numeric:
            x = _num(df[c]).dropna()
            if not len(x):
                continue
            rule = next((r for r in rules["rules"] if re.search(r["pattern"], name_tokens(c)[0])), None)
            imp = {}
            if rule:
                imp = dict(rule.get("impossible", {}))
                if rule.get("impossible_percent_scale"):
                    imp = {"min": 0, "max": 100 if x.max() > 1.0 else 1}
                for k, v in list(imp.items()):
                    if isinstance(v, str) and v.startswith("current_year"):
                        imp[k] = year + int(v.split("+")[1])
                if rule.get("only_if_integer") and (x % 1 != 0).any():
                    imp = {}
            lo, hi = imp.get("min", -np.inf), imp.get("max", np.inf)
            # missing-value codes first: a repeated -999 is a code, not a measurement error
            far = []
            sent = [v for v in self.cfg["sentinel_numbers"] if (x == v).sum() >= 2]
            if sent:
                rest = x[~x.isin(sent)]
                far = [v for v in sent if len(rest) > 5 and (v > rest.max() * 1.5 + 1 or v < rest.min() - abs(rest.min()) * 0.5 - 1)]
            if far:
                cnt = int(x.isin(far).sum())
                certain = all(v < lo or v > hi for v in far)
                self.issue("missing-value codes", c, "medium", f"repeated values {far} sit far outside the rest of the "
                           "data and are common codes for 'not recorded'" + (f"; they are also impossible for "
                           f"{rule['name']}" if certain else ""), cnt, n,
                           action="set to missing" if certain else "proposed as missing, disabled until reviewed")
                self.op("set_missing_where", {"column": c, "rule": {"values": far},
                        "reason": "numeric missing-value code", "classification": "missing-value code"},
                        "numeric missing-value codes",
                        ("These values are impossible for a " + rule["name"] + " and repeat exactly, the usual "
                         "pattern of a 'not recorded' code, so they are set to missing.") if certain else
                        "Values like -999 or 9999 are often codes for 'not recorded'. Confirm with the data "
                        "dictionary, then enable.", enabled=certain, needs_review=not certain,
                        evidence={"values": far, "count": cnt})
            xs = x[~x.isin(far)]
            hard = False
            if rule and imp:
                bad = int(((xs < lo) | (xs > hi)).sum())
                if bad:
                    hard = True
                    self.issue("invalid values", c, "high", f"values outside the possible range for {rule['name']} "
                               f"({lo} to {hi})", bad, n, evidence={"rule": rule["name"], "min": float(xs.min()),
                                                                   "max": float(xs.max())},
                               action="set to missing; originals in the change log; rows flagged")
                    self.op("set_missing_where", {"column": c, "rule": {"outside": [lo, hi]},
                            "reason": f"impossible value for {rule['name']} (allowed {lo} to {hi})",
                            "classification": "confirmed invalid"}, f"impossible {rule['name']} values",
                            f"A {rule['name']} outside {lo} to {hi} cannot be real, so it is treated as a recording "
                            "error. It is set to missing rather than guessed; the original stays in the change log.",
                            evidence={"count": bad})
            if rule:
                exp = dict(rule.get("expected", {}))
                for k, v in list(exp.items()):
                    if isinstance(v, str) and v.startswith("current_year"):
                        exp[k] = year + int(v.split("+")[1])
                if exp:
                    elo, ehi = exp.get("min", -np.inf), exp.get("max", np.inf)
                    cnt = int((((xs < elo) | (xs > ehi)) & (xs >= lo) & (xs <= hi)).sum())
                    if cnt > 0:
                        r2 = {}
                        if "min" in exp:
                            r2["lt"] = exp["min"]
                        if "max" in exp:
                            r2["gt"] = exp["max"]
                        self.issue("unusual values", c, "medium", f"values outside the usual range for {rule['name']}"
                                   + (f" ({rule['note']})" if rule.get("note") else ""), cnt, n,
                                   action="flagged; kept")
                        self.op("flag_rows", {"column": c, "rule": r2, "reason": f"unusual for {rule['name']}"
                                + (f"; {rule['note']}" if rule.get("note") else ""),
                                "classification": "potential anomaly"}, f"unusual {rule['name']} values",
                                "Possible but unusual values are kept and flagged, because they may be legitimate.")
            if len(xs) >= cfgo["min_n"] and xs.std() > 0 and not hard and not self.role_of(c, "identifier"):
                self.outliers(c, xs, n)

        # dates: future, very old, logical order
        today = pd.Timestamp.now().normalize()
        for c in dates:
            d = pd.to_datetime(df[c], errors="coerce")
            if getattr(d.dt, "tz", None) is not None:
                d = d.dt.tz_convert(None)
            fut = int((d > today + pd.Timedelta(days=1)).sum())
            future_ok = name_has(c, rules["future_ok_words"])
            is_birth = name_has(c, rules["birth_words"])
            if fut and not future_ok:
                if is_birth:
                    self.issue("invalid dates", c, "high", "birth dates in the future", fut, n,
                               action="set to missing; flagged")
                    self.op("set_missing_where", {"column": c, "rule": {"future": True},
                            "reason": "birth date in the future", "classification": "confirmed invalid"},
                            "future birth dates", "A birth date cannot be after today.")
                else:
                    self.issue("future dates", c, "medium", "dates after today", fut, n, action="flagged")
                    self.op("flag_rows", {"column": c, "rule": {"future": True}, "reason": "date is in the future",
                            "classification": "potential anomaly"}, "future dates",
                            "Future dates can be typos (wrong year) or legitimate scheduled events; flagged, not changed.")
            old = int((d < pd.Timestamp("1900-01-01")).sum())
            if old:
                self.issue("unusual dates", c, "medium", "dates before 1900", old, n, action="flagged")
                self.op("flag_rows", {"column": c, "rule": {"before": "1900-01-01"}, "reason": "date before 1900",
                        "classification": "potential anomaly"}, "very old dates",
                        "Often a default value (for example 1900-01-01) or a typo; flagged for review.")
        for a in dates:
            for b in dates:
                if a == b:
                    continue
                ta, tb = name_tokens(a)[0], name_tokens(b)[0]
                for x1, x2 in rules["date_pairs"]:
                    if re.search(rf"(^|_){x1}", ta) and re.search(rf"(^|_){x2}", tb):
                        cnt = int(rule_mask(df, None, {"date_order": [a, b]}).sum())
                        if cnt:
                            self.issue("logical inconsistency", f"{a} / {b}", "high", f"'{b}' is earlier than '{a}'",
                                       cnt, n, action="flagged")
                            self.op("flag_rows", {"column": b, "rule": {"date_order": [a, b]},
                                    "reason": f"'{b}' is before '{a}'", "classification": "logical inconsistency"},
                                    "dates out of order", "The order of these events is fixed, so at least one date is "
                                    "wrong; which one cannot be known from the data.")
                        break
        tc = rules["total_check"]
        q = next((c for c in numeric if name_has(c, tc["quantity"])), None)
        pr = next((c for c in numeric if name_has(c, tc["price"]) and c != q), None)
        tt = next((c for c in numeric if name_has(c, tc["total"]) and c not in (q, pr)), None)
        if q and pr and tt:
            cnt = int(rule_mask(df, None, {"total_mismatch": [q, pr, tt]}).sum())
            if cnt:
                self.issue("logical inconsistency", tt, "medium", f"'{tt}' does not equal '{q}' x '{pr}' "
                           "(beyond 1%; discounts, tax or shipping can explain this)", cnt, n, action="flagged")
                self.op("flag_rows", {"column": tt, "rule": {"total_mismatch": [q, pr, tt], "tolerance": 0.01},
                        "reason": f"{tt} differs from {q} x {pr}", "classification": "logical inconsistency"},
                        "totals that do not add up", "May be discounts or tax rather than errors; flagged for review.")

        self.missing(df, numeric)

    @staticmethod
    def discrete(s, max_levels=10):
        x = _num(s).dropna()
        return bool(len(x)) and bool((x % 1 == 0).all()) and x.nunique() <= max_levels

    def sens(self):
        return {self.nc(c) for c, p in self.cols.items() if "sensitive" in p["roles"]}

    def role_of(self, c, r):
        return r in self.prof_of.get(c, {}).get("roles", [])

    def outliers(self, c, x, n):
        cfgo = self.cfg["outliers"]
        q1, q3 = x.quantile(0.25), x.quantile(0.75)
        iqr = q3 - q1
        if iqr == 0:
            share = float((x == x.median()).mean())
            self.issue("outliers", c, "info", f"{share * 100:.0f}% of values equal the median, so IQR fences have zero "
                       "width; the values that differ are listed by the other checks rather than flagged as outliers")
            return
        lo, hi = q1 - cfgo["iqr_k"] * iqr, q3 + cfgo["iqr_k"] * iqr
        n_iqr = int(((x < lo) | (x > hi)).sum())
        n_ext = int(((x < q1 - cfgo["iqr_k_extreme"] * iqr) | (x > q3 + cfgo["iqr_k_extreme"] * iqr)).sum())
        z = (x - x.mean()) / x.std()
        n_z = int((z.abs() > cfgo["z_threshold"]).sum())
        med = x.median()
        mad = (x - med).abs().median()
        n_mad = int(((0.6745 * (x - med).abs() / mad) > cfgo["modified_z_threshold"]).sum()) if mad else 0
        skew = float(x.skew()) if len(x) > 2 else 0.0
        method = "iqr"
        n_log = None
        if skew > 1 and (x > 0).all():
            lx = np.log(x)
            lq1, lq3 = lx.quantile(0.25), lx.quantile(0.75)
            n_log = int(((lx < lq1 - 1.5 * (lq3 - lq1)) | (lx > lq3 + 1.5 * (lq3 - lq1))).sum())
            method = "iqr_log"
        ev = {"iqr_bounds": [float(lo), float(hi)], "iqr_count": n_iqr, "extreme_iqr_count": n_ext,
              "z_count": n_z, "modified_z_count": n_mad, "skewness": skew, "n": int(len(x)),
              "min": float(x.min()), "max": float(x.max()), "median": float(med)}
        if n_log is not None:
            ev["log_scale_iqr_count"] = n_log
        count = n_log if method == "iqr_log" else n_iqr
        if not count and not n_iqr:
            return
        note = ""
        if method == "iqr_log":
            note = (f" The data is strongly right-skewed (skewness {skew:.2f}). On a log scale "
                    + (f"only {n_log} values stand out" if n_log else "no value stands out")
                    + ", so most IQR outliers are the normal long tail of the distribution.")
        self.issue("outliers", c, "medium" if count / max(1, n) > 0.05 else "low",
                   f"{n_iqr} values outside the 1.5xIQR fences [{lo:.4g}, {hi:.4g}]; {n_mad} by modified z-score (MAD); "
                   f"{n_z} beyond 3 standard deviations." + note, count, n, evidence=ev,
                   action="flagged and kept; outliers are not removed automatically")
        if count:
            self.op("flag_rows", {"column": c, "rule": {"outlier": method, "k": 1.5},
                    "reason": "statistical outlier (" + ("1.5xIQR on log scale" if method == "iqr_log" else "1.5xIQR") + ")",
                    "classification": "unresolved outlier"}, "statistical outliers",
                    "Unusual is not the same as wrong. Outliers are kept and flagged; exclude or winsorise only with a "
                    "documented reason." + note, evidence=ev)

    def missing(self, df, numeric):
        cfgm = self.cfg["missing"]
        n = len(df)
        # every enabled fix so far (placeholders, types, invalid values, missing-value codes) applied, so the
        # comparison below measures errors on the values imputation will actually see
        trial = Engine(self.raw, {"operations": [o for o in self.ops if o["enabled"]], "settings": {}}).run().df
        self.df2 = trial
        sklearn = optional("sklearn") is not None
        dates = self.dates
        split = next((c for c in df.columns if self.role_of(c, "split")), None)
        fit_on = None
        if split:
            fit_on = {"column": split, "values": ["train", "Train", "TRAIN", "training"]}
            self.issue("modelling", split, "info", "train/test split column found; imputation statistics are fitted "
                       "on training rows only to avoid leakage")
        cats = [c for c in df.columns if c != ROW_ID and self.prof_of.get(c, {}).get("inferred_type") in
                ("categorical", "boolean", "binary") and 2 <= canon_series(df[c]).pipe(lambda s: s[s != ""]).nunique() <= 20]
        time_col = None
        for d in dates:
            dd = pd.to_datetime(df[d], errors="coerce").dropna()
            if len(dd) > 10 and dd.nunique() / len(dd) > 0.95:
                diffs = dd.sort_values().diff().dropna()
                if len(diffs) and (diffs == diffs.mode().iloc[0]).mean() >= 0.8:
                    time_col = d
                    break
        self.time_col = time_col
        for c in [c for c in df.columns if c != ROW_ID]:
            m = _is_missing(df[c])
            k = int(m.sum())
            if not k or k == n:
                continue
            share = k / n
            p = self.prof_of.get(c, {})
            assoc = self.missing_association(df, c, m, cats, numeric) if k >= 5 and st.have_scipy() else []
            mech = ("MAR plausible: missingness is related to " + ", ".join(a["column"] for a in assoc[:3])) if assoc else \
                "no relationship with observed columns found (consistent with MCAR; MNAR cannot be ruled out from the data alone)"
            if k >= 5 and not st.have_scipy():
                mech = "not tested (scipy not installed)"
            ev = {"missing": k, "missing_pct": round(100 * share, 2), "mechanism_assessment": mech,
                  "associations": assoc[:5]}
            sev = "high" if share > 0.3 else "medium" if share > 0.05 else "low"
            roles = p.get("roles", [])
            action, params, enabled, review, why = None, None, True, False, ""
            if "identifier" in roles:
                action = "left missing (identifiers are never imputed)"
            elif "sensitive" in roles:
                action = "left missing (personal data such as names or contact details is never invented)"
            elif "target_candidate" in roles:
                action = "left missing (possible outcome variable; imputing it would invent results)"
            elif "split" in roles:
                action = "left missing"
            elif c in numeric and self.discrete(df[c]):
                # ratings, scores, coded levels: a mean or median would invent an answer nobody gave
                vals = sorted(_num(df[c]).dropna().unique().tolist())
                if share <= cfgm["mode_impute_categorical_up_to_share"] and not assoc:
                    params = {"column": c, "method": "mode", "add_indicator": True}
                    why = (f"Whole-number scale with {len(vals)} levels and only {share * 100:.1f}% missing with no "
                           "detectable pattern, so the most frequent level is a low-impact fill.")
                    action = "mode imputation"
                else:
                    params = {"column": c, "method": "median", "add_indicator": True}
                    enabled, review = False, True
                    why = (f"The column holds {len(vals)} whole-number levels ({', '.join(fmt_num(v) for v in vals[:8])}), "
                           "so it is a scale, code or rating. Filling it would record answers or business values that "
                           "were never given. Left missing; enable only if the domain fixes the value (for example a "
                           "missing discount that means 0, via method 'constant').")
                    action = "left missing (discrete scale; fill proposed, disabled)"
            elif c in numeric:
                skew = float(_num(df[c]).skew()) if (~m).sum() > 2 else 0.0
                center = "median" if abs(skew) > cfgm["skew_threshold_for_median"] else "mean"
                group = assoc[0]["column"] if assoc and assoc[0]["column"] in cats else None
                if share > cfgm["leave_missing_above_share"]:
                    action = f"left missing ({share * 100:.0f}% missing is too much to fill credibly)"
                    params = {"column": c, "method": center, "add_indicator": True}
                    enabled, review = False, True
                    why = "Filling this many values would mostly report the fill value, not the data."
                else:
                    if time_col and c != time_col:
                        gap = self.cfg["time_series"]["max_gap_steps"]
                        params = {"column": c, "method": "interpolate", "order_by": time_col, "add_indicator": True,
                                  "max_gap": gap}
                        why = (f"Rows are evenly spaced in time by '{time_col}', so missing values are interpolated "
                               "between neighbours (time-weighted, no extrapolation past the ends). Runs of more than "
                               f"{gap} consecutive missing values are left missing: a straight line across a long gap "
                               "would hide whatever happened there.")
                    elif group:
                        params = {"column": c, "method": f"group_{center}", "group_by": group, "add_indicator": True}
                        why = (f"Missingness is related to '{group}', so a single overall {center} would bias groups; "
                               f"the {center} within each '{group}' group is used.")
                    else:
                        params = {"column": c, "method": center, "add_indicator": True}
                        why = (f"The distribution is {'skewed' if center == 'median' else 'roughly symmetric'} "
                               f"(skewness {skew:.2f}), so the {center} is a representative fill value.")
                    if share > cfgm["impute_numeric_up_to_share"]:
                        enabled, review = False, True
                        why += f" Disabled because {share * 100:.0f}% is missing; decide whether filling is defensible."
                    action = params["method"] + " imputation" + ("" if enabled else " proposed (disabled)")
            elif p.get("inferred_type") in ("categorical", "boolean", "binary"):
                if share <= cfgm["mode_impute_categorical_up_to_share"] and not assoc:
                    params = {"column": c, "method": "mode", "add_indicator": True}
                    why = (f"Only {share * 100:.1f}% missing with no detectable pattern, so the most frequent category "
                           "is a low-impact fill.")
                    action = "mode imputation"
                else:
                    params = {"column": c, "method": "constant", "value": "Missing", "add_indicator": False}
                    enabled, review = False, True
                    why = ("An explicit 'Missing' category keeps these rows visible in group comparisons without "
                           "guessing a label. Proposed, disabled: EDA already treats missing as its own level.")
                    action = "left missing (explicit category proposed)"
            else:
                action = "left missing"
            if params and fit_on and params.get("method") not in ("interpolate",):
                params["fit_on"] = fit_on
            if params and params.get("method") != "constant" and self.cfg["imputation_comparison"]["enabled"] \
                    and c in trial.columns:
                params, why = self.compare(trial, c, params, why, cats, numeric, time_col, fit_on, sklearn, ev,
                                           discrete=c in numeric and self.discrete(df[c]))
                if action and "imputation" in action:
                    action = params["method"] + " imputation" + ("" if enabled else " proposed (disabled)")
                sc = ev.get("strategy_comparison", {})
                best = (sc.get("methods") or {}).get(sc.get("chosen"), {})
                usable = self.cfg["imputation_evidence"]["usable_accuracy"]
                if sc.get("metric") == "accuracy" and best.get("accuracy") is not None and best["accuracy"] < usable:
                    # the check shows a fill would be wrong more often than right: do not enable it
                    why += (f" On hidden known values even the best method was right only {best['accuracy'] * 100:.0f}% "
                            "of the time, so a fill would mostly record wrong labels. Left missing; an explicit "
                            "'Missing' category is proposed instead (disabled).")
                    params = {"column": c, "method": "constant", "value": "Missing", "add_indicator": False}
                    enabled, review = False, True
                    action = "left missing (fills tested as unreliable; explicit category proposed)"
            self.issue("missing values", c, sev, f"{k} missing ({share * 100:.1f}%). {mech}.", k, n, evidence=ev,
                       action=action or "")
            if params:
                self.op("impute", params, "missing values", why, enabled=enabled, needs_review=review, evidence=ev)

    @staticmethod
    def spec_name(params):
        g = params.get("group_by")
        g = ", ".join(g) if isinstance(g, list) else g
        return f"{params['method']} by {g}" if g and params["method"].startswith("group_") else params["method"]

    def compare(self, df, c, params, why, cats, numeric, time_col, fit_on, sklearn, ev, discrete=False):
        """Masked-value comparison of plausible methods. Switches only to a clearly better method of a kind that
        suits the column (never to a continuous estimate on a discrete scale), and records the alternatives."""
        is_num = c in numeric
        group = params.get("group_by")
        specs = candidates_for(df, c, is_num, [g for g in cats if g != c], [x for x in numeric if x in df.columns],
                               time_col if time_col and time_col != c else None,
                               group_hint=group if isinstance(group, str) else None, sklearn=sklearn and not discrete)
        if discrete:
            specs = [s for s in specs if s["method"] in ("median", "group_median")] + [{"name": "mode", "method": "mode"}]
        name = self.spec_name(params)
        if name not in {sp.get("name") for sp in specs}:
            specs.append({"name": name, **{k: v for k, v in params.items()
                                           if k in ("method", "group_by", "order_by", "max_gap", "features")}})
        if is_num and "mean" not in {sp.get("name") for sp in specs}:
            specs.append({"name": "mean", "method": "mean"})  # the baseline every numeric comparison reports
        fit_rows = None
        if fit_on:
            fit_rows = canon_series(df[fit_on["column"]]).isin([str(v) for v in fit_on["values"]])
        comp = compare_methods(df, c, specs, self.cfg, self.cfg.get("seed", 42), fit_rows=fit_rows,
                               group_for_subgroups=group if isinstance(group, str) else (cats[0] if cats else None))
        self.comparisons[c] = comp
        if comp.get("status") != "ok":
            ev["strategy_comparison"] = {"status": comp.get("status")}
            return params, why
        key = "nmae" if comp["kind"] == "numeric" else "accuracy"
        summary = {n: {key: m.get(key), "coverage": m.get("coverage"),
                       **({"bias": m.get("bias"), "spread_ratio": m.get("spread_ratio")} if key == "nmae" else {})}
                   for n, m in comp["methods"].items() if m.get("status") == "ok"}
        alt = None if discrete and comp["kind"] == "numeric" and name == "mode" else better_than(comp, name, self.cfg)
        chosen = name
        if alt:
            spec = comp["methods"][alt]["spec"]
            new = {"column": c, **{k: v for k, v in spec.items() if k != "name"}, "add_indicator": True}
            if params.get("fit_on") and new["method"] != "interpolate":
                new["fit_on"] = params["fit_on"]
            if new["method"] == "interpolate":
                new["max_gap"] = self.cfg["time_series"]["max_gap_steps"]
            cur, best = comp["methods"][name], comp["methods"][alt]
            why += (f" Masked-value check ({comp['masked_per_repeat']} known values hidden, {comp['repeats']} times): "
                    + (f"{alt} had normalised MAE {best['nmae']:.3f} against {cur['nmae']:.3f} for {name}"
                       if key == "nmae" else f"{alt} had accuracy {best['accuracy']:.3f} against "
                       f"{cur['accuracy']:.3f} for {name}") + f", so {alt} is used instead.")
            params, chosen = new, alt
        elif name in summary and summary[name][key] is not None:
            v = summary[name][key]
            why += (f" Masked-value check: {name} had {'normalised MAE' if key == 'nmae' else 'accuracy'} {v:.3f}"
                    + (f"; no other method was clearly better (best: {comp['best']})." if comp["best"] != name
                       else ", the best of the methods compared."))
        ev["strategy_comparison"] = {"chosen": chosen, "metric": key, "methods": summary,
                                     "rejected": [n for n in summary if n != chosen],
                                     "masked_per_repeat": comp["masked_per_repeat"], "repeats": comp["repeats"],
                                     "limitations": comp["limitations"]}
        return params, why

    def missing_association(self, df, c, m, cats, numeric, alpha=0.01):
        out = []
        for g in cats:
            if g == c:
                continue
            gv = canon_series(df[g])
            ok = gv != ""
            if ok.sum() < 20:
                continue
            res = st.chi_square(m[ok].astype(int), gv[ok])
            if res:
                out.append({"column": g, "test": res["test"], "p_value": res["p_value"],
                            "effect": res["effect_size"].get("cramers_v", 0.0),
                            "missing_rate_by_group": {str(k): round(float(v), 3) for k, v in
                                                      m[ok].groupby(gv[ok]).mean().head(20).items()}})
        for x in numeric:
            if x == c:
                continue
            xv = _num(df[x])
            a, b = xv[m].dropna(), xv[~m].dropna()
            if len(a) < 5 or len(b) < 5:
                continue
            res = st.scipy_stats.mannwhitneyu(a, b, alternative="two-sided")
            r = 2 * float(res.statistic) / (len(a) * len(b)) - 1
            out.append({"column": x, "test": "Mann-Whitney U", "p_value": float(res.pvalue), "effect": abs(r),
                        "median_when_missing": float(a.median()), "median_when_present": float(b.median())})
        if not out:
            return []
        q = st.bh_adjust([o["p_value"] for o in out])
        for o, qq in zip(out, q):
            o["q_value"] = qq
        sig = [o for o in out if o["q_value"] < alpha and o["effect"] >= 0.1]
        return sorted(sig, key=lambda o: -o["effect"])

    # ------------------------------------------------------------ result
    def build(self):
        self.stage1()
        self.stage2()
        diag_df = getattr(self, "df2", None)
        self.extras["missingness"] = diagnose(
            diag_df if diag_df is not None else self.df1,
            time_col=getattr(self, "time_col", None) or (self.dates[0] if self.dates else None))
        base = diag_df if diag_df is not None else self.df1
        self.extras["_missing_mask"] = pd.DataFrame({c: _is_missing(base[c]) for c in base.columns if c != ROW_ID})
        self.extras["imputation_comparison"] = {"columns": self.comparisons,
                                                "note": "random masking of known values; see each column's limitations"}
        settings = {"seed": self.cfg.get("seed", 42), "config": self.cfg}
        roles = self.profile["roles"]
        constraints = []
        for c in roles.get("identifier", []):
            p = self.cols[c]
            if p["unique_count"] == p["non_null_count"] and p["non_null_count"]:
                constraints.append({"column": self.nc(c), "rule": "unique", "why": "identifier unique in the source"})
        for o in self.ops:
            if o["op"] == "set_missing_where" and o["enabled"] and "outside" in o["params"]["rule"]:
                lo, hi = o["params"]["rule"]["outside"]
                constraints.append({"column": o["params"]["column"], "rule": "range", "min": lo, "max": hi,
                                    "why": o["issue"]})
        sev_rank = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
        self.issues.sort(key=lambda i: sev_rank.get(i["severity"], 5))
        for i, iss in enumerate(self.issues, 1):
            iss["issue_id"] = f"ISSUE-{i:03d}"
        return {
            "plan_version": 1,
            "dataset": {k: self.meta.get(k) for k in ("file", "path", "sha256", "format", "sheet", "table")},
            "load_options": {k: self.meta.get(k) for k in ("sheet", "table", "block") if self.meta.get(k)},
            "settings": settings,
            "roles": {**{k: [self.nc(c) for c in v] for k, v in roles.items()}},
            "review_required": [o["op_id"] for o in self.ops if o["needs_review"]],
            "operations": self.ops,
            "constraints": constraints,
        }, self.issues


def build_plan(raw, profile, meta, cfg=None, extras=None):
    pb = PlanBuilder(raw, profile, meta, cfg)
    plan, issues = pb.build()
    if extras is not None:
        extras.update(pb.extras)
    return plan, issues
