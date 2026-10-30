"""Exploratory data analysis on the cleaned data: univariate, bivariate, multivariate, time series,
statistical tests with assumption checks and multiple-testing correction, and ranked finding candidates.

The numbers here are the evidence base for the written insights. The script ranks candidates;
the agent decides which matter and explains them.
"""

import math
import os

import numpy as np
import pandas as pd

from . import charts
from . import stats as st
from .clean import read_str_csv
from .common import ROW_ID, DataMedicError, load_config, merged_config, optional, read_json, write_json
from .profile import name_has
from .validate import typed_view

ADDITIVE = ["revenue", "sales", "amount", "total", "quantity", "qty", "units", "count", "cost", "spend", "profit",
            "income", "orders", "clicks", "impressions", "conversions", "visits", "sessions", "volume"]


def _rel(out_dir, p):
    return os.path.relpath(p, out_dir).replace("\\", "/") if p else None


def halting_rules(out_dir):
    """Contract rules with on_fail 'halt' that failed in the last validation."""
    p = os.path.join(out_dir, "audit", "validation_results.json")
    if not os.path.exists(p):
        return []
    rules = (read_json(p).get("contract") or {}).get("rules", [])
    return [r for r in rules if r.get("on_fail") == "halt" and not r.get("passed")]


class EDA:
    def __init__(self, out_dir, target=None, time_col=None, cfg=None, charts_on=True, ignore_halt=False):
        self.out = out_dir
        if not ignore_halt:
            halted = halting_rules(out_dir)
            if halted:
                raise DataMedicError("Analysis stopped: contract rules marked on_fail 'halt' failed in validation ("
                                     + "; ".join(f"{r['id']} {r['rule']} {r.get('column', '')}: {r['detail']}"
                                                 for r in halted[:5])
                                     + "). Fix the data or the contract and validate again, or pass --ignore-halt.")
        self.cfg = merged_config(cfg)
        self.ec = self.cfg["eda"]
        self.seed = self.cfg.get("seed", 42)
        self.plan = read_json(os.path.join(out_dir, "plan.json"))
        self.schema = read_json(os.path.join(out_dir, "cleaned_data", "schema.json"))
        self.prof = read_json(os.path.join(out_dir, "profiling", "initial_profile.json"))
        raw = read_str_csv(os.path.join(out_dir, "cleaned_data", "cleaned_dataset.csv"))
        self.df = typed_view(raw, self.schema)
        self.exclusions = self.apply_exclusions()
        self.n = len(self.df)
        roles = self.plan.get("roles", {})
        cols = self.schema["columns"]
        self.sensitive = {c for c, i in cols.items() if i.get("sensitive")} | set(roles.get("sensitive", []))
        added = {c for c, i in cols.items() if i.get("added_by_cleaning")}
        ids = set(roles.get("identifier", []))
        self.ids = ids
        self.split = (roles.get("split") or [None])[0]
        self.measures = [c for c, i in cols.items() if i["type"] in ("number", "integer") and c not in ids
                         and c not in added and self.df[c].notna().sum() >= 5 and self.df[c].nunique() > 2]
        self.measures.sort(key=lambda c: (not name_has(c, load_config("domain_rules.json")["measure_priority_words"]),
                                          list(cols).index(c)))
        self.categories = []
        for c, i in cols.items():
            if c in ids or c in added or c in self.sensitive or c == self.split:
                continue
            if i["type"] in ("string", "boolean") or (i["type"] in ("number", "integer") and self.df[c].nunique() == 2):
                k = self.df[c].nunique()
                if 2 <= k <= self.ec["max_group_levels"]:
                    self.categories.append(c)
        self.free = [c for c, i in cols.items() if i["type"] == "string" and c not in self.categories
                     and c not in ids and c not in added]
        dates = [c for c, i in cols.items() if i["type"] in ("date", "datetime")]
        self.time = time_col if time_col in dates else next((d for d in dates if self.df[d].notna().mean() >= 0.8), None)
        self.target = target or next((c for c in roles.get("target_candidate", []) if c in self.df.columns), None)
        self.charts_on = charts_on and charts.available()
        charts.configure(self.cfg)
        self.chart_count = 0
        self.tests = []
        self.selection = self.rank_variables()
        self.res = {"rows": self.n, "columns": len(cols), "measures": self.measures, "categories": self.categories,
                    "variable_selection": self.selection, "analytical_exclusions": self.exclusions,
                    "time_column": self.time, "target": self.target, "identifiers": sorted(ids),
                    "sensitive_columns_excluded_from_value_reports": sorted(self.sensitive),
                    "domain": self.prof.get("domain"), "charts": []}

    # ------------------------------------------------------------ exclusions and variable choice
    def apply_exclusions(self):
        """Rows flagged with exclude_from stay in the cleaned data but are left out of the named columns' analysis."""
        p = os.path.join(self.out, "audit", "flagged_records.csv")
        if not os.path.exists(p):
            return []
        fl = read_str_csv(p)
        if "exclude_from" not in fl.columns:
            return []
        fl = fl[fl["exclude_from"] != ""]
        out = []
        for (op, scope), g in fl.groupby(["op_id", "exclude_from"]):
            hit = self.df[ROW_ID].astype(int).isin(set(g[ROW_ID].astype(int)))
            cols = [c.strip() for c in scope.split(",")]
            if "all" in cols:
                self.df = self.df.loc[~hit]
            else:
                for c in cols:
                    if c in self.df.columns:
                        self.df.loc[hit, c] = np.nan if self.df[c].dtype.kind in "fiM" else None
            out.append({"op_id": op, "columns": cols, "rows": int(hit.sum()), "reason": g["reason"].iloc[0]})
        return out

    def rank_variables(self):
        """Order measures and grouping columns by how much they can tell, not by their position in the file.

        A measure scores for completeness, spread, a link to the outcome column and a domain priority name;
        a grouping column scores for completeness and balance (normalised entropy). Charts and the capped
        analyses follow this order. The scores are recorded so the choice can be checked."""
        prio = load_config("domain_rules.json")["measure_priority_words"]
        out = {"measures": [], "categories": []}
        tgt = self.target
        for c in self.measures:
            x = self.df[c]
            comp = float(x.notna().mean())
            nun = int(x.nunique())
            spread = min(1.0, math.log10(max(nun, 1)) / 2)
            rel = 0.0
            if tgt and tgt in self.df.columns and tgt != c and st.have_scipy() and self.df[tgt].dtype.kind in "fi":
                r = st.correlation(x, self.df[tgt])
                rel = abs(r["spearman_rho"]) if r else 0.0
            pr = 0.3 if name_has(c, prio) else 0.0
            score = round(comp * (0.5 + 0.5 * spread) + rel + pr, 4)
            out["measures"].append({"column": c, "score": score, "completeness": round(comp, 4), "distinct": nun,
                                    "outcome_link": round(rel, 4), "priority_name": bool(pr)})
        for c in self.categories:
            vc = self.df[c].astype("string").value_counts(dropna=True)
            comp = float(self.df[c].notna().mean())
            sh = vc / vc.sum() if vc.sum() else vc
            ent = float(-(sh * np.log(sh)).sum() / math.log(len(sh))) if len(sh) > 1 else 0.0
            out["categories"].append({"column": c, "score": round(comp * ent, 4), "completeness": round(comp, 4),
                                      "levels": int(len(vc)), "balance": round(ent, 4)})
        out["measures"].sort(key=lambda r: -r["score"])
        out["categories"].sort(key=lambda r: -r["score"])
        out["measures"] = out["measures"][: self.ec["max_numeric_columns"]]
        self.measures = [r["column"] for r in out["measures"]]
        self.categories = [r["column"] for r in out["categories"]]
        out["note"] = ("measures ranked by completeness, spread, link to the outcome column and domain priority; grouping "
                       "columns by completeness and balance. Charts and capped analyses follow this order.")
        return out

    # ------------------------------------------------------------ helpers
    def chart(self, kind, fn, *a, **k):
        if not self.charts_on or self.chart_count >= self.ec["max_charts"]:
            return None
        try:
            p = fn(*a, **k)
            self.chart_count += 1
            rp = _rel(self.out, p)
            self.res["charts"].append({"kind": kind, "path": rp, "library": charts.library(),
                                       "formats": charts.SETTINGS.get("formats", ["png"])})
            return rp
        except Exception as exc:  # a chart failure must not stop the analysis
            self.res.setdefault("chart_errors", []).append(f"{kind}: {type(exc).__name__}: {exc}")
            return None

    def vpath(self, sub, name):
        safe = "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in name)[:60]
        return os.path.join(self.out, "visualisations", sub, safe + ".png")

    def add_test(self, family, rec, **meta):
        if rec is None:
            return None
        rec = {**rec, "family": family, **meta}
        self.tests.append(rec)
        return rec

    # ------------------------------------------------------------ sections
    def univariate(self):
        num = {}
        for c in self.measures:
            d = st.describe_numeric(self.df[c])
            d["missing"] = int(self.df[c].isna().sum())
            num[c] = d
            if len(num) <= self.ec.get("max_distribution_charts", 6):
                d["chart"] = self.chart("distribution", charts.distribution, self.df[c], c,
                                        self.vpath("distributions", c), self.schema["columns"][c].get("unit", ""))
        cat = {}
        for c in self.categories:
            s = self.df[c].astype("string")
            vc = s.value_counts(dropna=True)
            total = int(vc.sum())
            shares = vc / total if total else vc
            rare = shares[shares < self.cfg["categories"]["rare_share"]]
            ent = float(-(shares * np.log(shares)).sum() / math.log(len(shares))) if len(shares) > 1 else 0.0
            cat[c] = {"n": total, "missing": int(s.isna().sum()), "levels": int(len(vc)),
                      "top": [{"value": str(k), "count": int(v), "pct": round(100 * v / total, 2)} for k, v in vc.head(10).items()],
                      "least_frequent": [{"value": str(k), "count": int(v)} for k, v in vc.tail(3).items()],
                      "rare_levels": int(len(rare)), "imbalance_ratio": float(vc.iloc[0] / vc.iloc[-1]) if len(vc) else None,
                      "normalised_entropy": ent}
            if len(cat) <= self.ec.get("max_bar_charts", 4):
                cat[c]["chart"] = self.chart("bar", charts.bar_counts, vc, c, self.vpath("distributions", c), total)
        self.res["univariate"] = {"numeric": num, "categorical": cat}

    def bivariate(self):
        pairs = []
        ms = self.measures
        for i, a in enumerate(ms):
            for b in ms[i + 1:]:
                r = st.correlation(self.df[a], self.df[b]) if st.have_scipy() else None
                if r:
                    r.update(a=a, b=b, strength=st.corr_label(r["spearman_rho"]))
                    pairs.append(r)
                    self.add_test("correlation", {"test": "Spearman rank correlation", "p_value": r["spearman_p"],
                                                  "statistic": r["spearman_rho"], "n": r["n"],
                                                  "effect_size": {"spearman_rho": r["spearman_rho"],
                                                                  "magnitude": r["strength"]},
                                                  "null_hypothesis": f"No monotonic association between {a} and {b}.",
                                                  "alternative_hypothesis": "There is a monotonic association.",
                                                  "why": "Spearman does not assume normality or linearity; Pearson is "
                                                         "reported alongside for linear association",
                                                  "pearson_r": r["pearson_r"], "pearson_ci95": r["pearson_ci95"]},
                                  variables=[a, b])
        self.res["correlations"] = pairs
        cats = self.categories[:10]
        assoc = []
        for i, a in enumerate(cats):
            for b in cats[i + 1:]:
                d = self.df[[a, b]].dropna()
                if len(d) < 20 or not st.have_scipy():
                    continue
                rec = st.chi_square(d[a], d[b])
                if rec:
                    assoc.append({"a": a, "b": b, **rec})
                    self.add_test("categorical association", rec, variables=[a, b])
        self.res["categorical_associations"] = assoc
        groups = []
        for m in ms[:8]:
            for g in cats[:8]:
                if not st.have_scipy():
                    break
                rec = st.compare_groups(self.df[m], self.df[g].astype("string").fillna(""), sample=self.ec["normality_sample"],
                                        seed=self.seed)
                if rec:
                    rec.update(measure=m, group=g)
                    groups.append(rec)
                    self.add_test("group comparison", rec, variables=[m, g])
        self.res["group_comparisons"] = groups

    def multivariate(self):
        ms = self.measures
        out = {}
        if len(ms) >= 2:
            corr = self.df[ms].corr(method="spearman")
            out["spearman_matrix"] = {a: {b: (None if not np.isfinite(corr.at[a, b]) else float(corr.at[a, b]))
                                          for b in ms} for a in ms}
            out["heatmap"] = self.chart("heatmap", charts.heatmap, corr, self.vpath("correlations", "spearman_matrix"),
                                        f"Spearman correlations between {len(ms)} numeric columns")
            hi = []
            for i, a in enumerate(ms):
                for b in ms[i + 1:]:
                    v = corr.at[a, b]
                    if np.isfinite(v) and abs(v) >= self.ec["collinearity_correlation"]:
                        hi.append({"a": a, "b": b, "spearman_rho": float(v)})
            out["highly_correlated_pairs"] = hi
        if len(ms) >= 3:
            out["vif"] = {k: (None if not np.isfinite(v) else v) for k, v in st.vif(self.df[ms]).items()}
            X = self.df[ms].dropna()
            if len(X) >= 30:
                Z = (X - X.mean()) / X.std().replace(0, np.nan)
                Z = Z.dropna(axis=1)
                if Z.shape[1] >= 3:
                    U, S, Vt = np.linalg.svd(Z.values, full_matrices=False)
                    ev = S ** 2 / (S ** 2).sum()
                    load = sorted(zip(Z.columns, Vt[0]), key=lambda t: -abs(t[1]))
                    out["pca"] = {"rows_used": int(len(X)), "explained_variance_ratio": [float(v) for v in ev[:5]],
                                  "pc1_top_loadings": [{"column": c, "loading": float(v)} for c, v in load[:5]],
                                  "note": "on standardised complete rows; descriptive only"}
        # Simpson's paradox check
        simpson = []
        for r in sorted(self.res.get("correlations", []), key=lambda r: -abs(r["spearman_rho"]))[:5]:
            if abs(r["spearman_rho"]) < 0.3:
                continue
            for g in self.categories[:8]:
                d = self.df[[r["a"], r["b"], g]].dropna()
                within = []
                for lvl, sub in d.groupby(g):
                    if len(sub) >= 20 and sub[r["a"]].std() > 0 and sub[r["b"]].std() > 0:
                        within.append((str(lvl), float(sub[r["a"]].corr(sub[r["b"]], method="spearman")), len(sub)))
                if len(within) >= 2:
                    opp = [w for w in within if np.sign(w[1]) != np.sign(r["spearman_rho"]) and abs(w[1]) >= 0.1]
                    if len(opp) > len(within) / 2:
                        simpson.append({"a": r["a"], "b": r["b"], "overall_rho": r["spearman_rho"], "group": g,
                                        "within_groups": [{"level": l, "rho": v, "n": n} for l, v, n in within]})
        out["simpsons_paradox_checks"] = simpson
        out["anomalies"] = self.anomalies()
        self.res["multivariate"] = out

    def anomalies(self):
        """Rows unusual in several measures at once. Flagged for investigation only; nothing is changed.

        Isolation Forest and a robust (minimum covariance determinant) distance when scikit-learn is installed;
        otherwise a robust distance on median/MAD-scaled values. An anomaly is not an error: a large order from
        a large customer is unusual and real."""
        ac = self.cfg["anomalies"]
        # distance-based methods need continuous values; on a 1-5 rating almost every value is far from the median
        ms = [m for m in self.measures if self.df[m].nunique() >= ac["min_distinct"]][: ac["max_features"]]
        if len(ms) < 2:
            return {"status": f"not run: needs at least two continuous measures (with {ac['min_distinct']} or more "
                              "distinct values)"}
        X = self.df[ms].astype(float)
        X = X[X.notna().mean(axis=1) >= 0.5]
        if len(X) < ac["min_rows"]:
            return {"status": f"not run: {len(X)} usable rows (needs {ac['min_rows']})"}
        med = X.median()
        mad = (X - med).abs().median().replace(0, np.nan) * 1.4826
        Z = ((X - med) / mad).dropna(axis=1, how="all").fillna(0.0)
        if Z.shape[1] < 2:
            return {"status": "not run: measures have no spread"}
        res = {"features": list(Z.columns), "rows_scored": int(len(Z)), "methods": [],
               "scaling": "median and MAD (robust); missing values set to the median for scoring only"}
        scores = pd.DataFrame(index=Z.index)
        sk = optional("sklearn")
        cut = float(st.scipy_stats.chi2.ppf(ac["robust_distance_quantile"], Z.shape[1])) if st.have_scipy() else None
        if sk is not None:
            from sklearn.covariance import MinCovDet
            from sklearn.ensemble import IsolationForest
            iso = IsolationForest(random_state=self.seed, contamination=ac["contamination"], n_estimators=200)
            f_iso = pd.Series(iso.fit_predict(Z.values) == -1, index=Z.index)
            scores["isolation_forest_score"] = -iso.score_samples(Z.values)
            res["methods"].append({"method": "Isolation Forest", "flagged": int(f_iso.sum()),
                                   "contamination": ac["contamination"], "seed": self.seed})
            flagged = f_iso
            res["rule"] = "listed when Isolation Forest flags the row"
            if len(Z) > 5 * Z.shape[1] and cut:
                try:
                    d2 = MinCovDet(random_state=self.seed).fit(Z.values).mahalanobis(Z.values)
                    scores["robust_distance_sq"] = d2
                    f_d = scores["robust_distance_sq"] > cut
                    res["methods"].append({"method": "robust covariance distance (MCD)", "flagged": int(f_d.sum()),
                                           "cutoff_chi2_quantile": ac["robust_distance_quantile"]})
                    # both methods must agree, which keeps the list short and less dependent on one model's settings
                    flagged = f_iso & f_d
                    res["rule"] = "listed when Isolation Forest and the robust distance both flag the row"
                except ValueError as exc:
                    res["methods"].append({"method": "robust covariance distance (MCD)", "status": f"not run: {exc}"})
        elif cut:
            scores["robust_distance_sq"] = (Z ** 2).sum(axis=1)
            flagged = scores["robust_distance_sq"] > cut
            res["methods"].append({"method": "robust distance on median/MAD-scaled values (no scikit-learn)",
                                   "flagged": int(flagged.sum()), "cutoff_chi2_quantile": ac["robust_distance_quantile"],
                                   "note": "ignores correlation between measures; install scikit-learn for Isolation "
                                           "Forest and a robust covariance distance"})
            res["rule"] = "listed when the robust distance exceeds the cutoff"
        else:
            return {"status": "not run: needs scipy or scikit-learn"}
        if ac.get("write_row_scores", True):
            res["scores_file"] = self.write_row_scores(scores, flagged, cut)
        top = scores[flagged].sort_values(scores.columns[0], ascending=False).head(ac["max_rows_listed"])
        rows = []
        for idx, r in top.iterrows():
            z = Z.loc[idx]
            drivers = z.abs().sort_values(ascending=False).head(3).index
            rows.append({ROW_ID: int(self.df.at[idx, ROW_ID]), **{k: float(v) for k, v in r.items()},
                         "main_drivers": ", ".join(f"{c} ({float(z[c]):+.1f} robust sd)" for c in drivers)})
        if flagged.any() and Z.shape[1] >= 2:
            U, S_, _ = np.linalg.svd(Z.values - Z.values.mean(axis=0), full_matrices=False)
            res["chart"] = self.chart("anomalies", charts.anomaly_scatter, U[:, :2] * S_[:2], flagged.to_numpy(),
                                      self.vpath("relationships", "multivariate_anomalies"),
                                      f"Rows unusual across {Z.shape[1]} measures (two main components)")
        res["flagged"] = int(flagged.sum())
        res["share_pct"] = round(100 * float(flagged.mean()), 2)
        res["note"] = ("flagged for investigation, not removed: an anomaly can be an error, a rare real event or a "
                       "different kind of record")
        p = os.path.join(self.out, "analysis", "multivariate_anomalies.csv")
        if rows:
            os.makedirs(os.path.dirname(p), exist_ok=True)
            pd.DataFrame(rows).to_csv(p, index=False, encoding="utf-8", lineterminator="\n")
            res["file"] = "analysis/multivariate_anomalies.csv"
            res["rows"] = rows[:20]
        elif os.path.exists(p):
            os.remove(p)
        return res

    # ------------------------------------------------------------ missing dates
    def missing_dates(self):
        """Why dates are missing, and whether the missing ones cluster. Analysis only: nothing is filled.

        Reasons come from the audit trail (blank in the source, unreadable, ambiguous day/month order, set missing
        by a rule). Clustering is tested along every order the rows have (file order, numeric identifiers) with a
        runs test: an ingestion outage leaves long runs of missing dates, chance leaves short ones. Missing dates are
        placed in time (period, weekday) only when an order key tracks the date closely; otherwise the date of a
        missing value is unknown and the report says so."""
        cfg = self.cfg.get("missing_dates", {})
        out = []
        for c, info in self.schema["columns"].items():
            if info["type"] not in ("date", "datetime") or info.get("added_by_cleaning"):
                continue
            t = pd.to_datetime(self.df[c], errors="coerce")
            if getattr(t.dt, "tz", None) is not None:
                t = t.dt.tz_convert(None)
            miss = t.isna()
            rec = {"column": c, "rows": int(len(t)), "missing": int(miss.sum()),
                   "missing_pct": round(100 * float(miss.mean()), 2) if len(t) else 0.0}
            if not miss.any() or miss.all():
                rec["status"] = "no missing dates" if not miss.any() else "every date is missing"
                out.append(rec)
                continue
            rec["reasons"] = self._date_missing_reasons(c, miss)
            rec["orders"] = []
            for name, key in self._order_keys():
                o = self._missing_order_test(name, key, t, miss)
                if o:
                    rec["orders"].append(o)
                    self.add_test("missing dates", {"test": "Wald-Wolfowitz runs test",
                                                    "p_value": o["runs_test"]["p_value"], "statistic": o["runs_test"]["z"]},
                                  variables=[c, name], question=f"are missing {c} values clustered along {name}?")
            best = max(rec["orders"], key=lambda o: abs(o.get("date_rank_correlation") or 0), default=None)
            thr = cfg.get("chronological_rho", 0.9)
            if best and abs(best.get("date_rank_correlation") or 0) >= thr:
                rec["placement"] = self._place_missing_dates(c, best["order"], t, miss)
            else:
                rho = abs(best.get("date_rank_correlation") or 0) if best else None
                rec["placement"] = {"status": "not possible", "why": "no column orders the rows in time "
                                    f"(best rank correlation with the date {rho if rho is None else round(rho, 3)}, "
                                    f"needs {thr}), so a missing date cannot be placed in a period and filling it from "
                                    "neighbouring rows would invent it"}
            out.append(rec)
        self.res["missing_dates"] = out

    def _order_keys(self):
        keys = [("file order", self.df[ROW_ID].astype(float))]
        for k in sorted(self.ids):
            if k not in self.df.columns:
                continue
            v = pd.to_numeric(self.df[k], errors="coerce")
            if v.notna().mean() < 0.95:  # codes such as TXN-00042: use the trailing number
                v = pd.to_numeric(self.df[k].astype("string").str.extract(r"(\d+)\s*$")[0], errors="coerce")
            if v.notna().mean() >= 0.95 and v.nunique() >= 0.9 * v.notna().sum():
                ok = v.notna()
                if v[ok].rank().corr(keys[0][1][ok].rank()) > 0.999:
                    continue  # same order as the file: testing it again adds nothing
                keys.append((k, v.astype(float)))
        return keys

    def _date_missing_reasons(self, c, miss):
        from .common import date_candidates
        ids = set(self.df.loc[miss, ROW_ID].astype(int))
        reasons = {}
        last = {}
        p = os.path.join(self.out, "audit", "cell_changes.csv")
        if os.path.exists(p):
            ch = read_str_csv(p)
            ch = ch[(ch["column"] == c) & (ch["new_value"] == "")]
            ch = ch[ch[ROW_ID].astype(int).isin(ids)]
            last = {int(r): (op, old) for r, op, old in zip(ch[ROW_ID], ch["op_id"], ch["old_value"])}
        log = read_json(os.path.join(self.out, "audit", "transformation_log.json"))["operations"]
        log = {e["op_id"]: e for e in log}
        for rid in ids:
            if rid not in last:
                why = "blank in the source"
            else:
                op_id, old = last[rid]
                e = log.get(op_id, {})
                if e.get("op") == "convert_type":
                    cand = date_candidates(old, (e.get("params") or {}).get("allow_compact", False))
                    why = ("ambiguous day/month order" if "dmy" in cand and "mdy" in cand and cand["dmy"] != cand["mdy"]
                           else "not readable as a date")
                elif e.get("op") == "standardize_missing":
                    why = "placeholder text"
                else:
                    why = (e.get("params") or {}).get("reason") or e.get("op", "other step")
                why = f"{why} ({op_id})"
            reasons[why] = reasons.get(why, 0) + 1
        n = int(miss.sum())
        return [{"reason": k, "rows": v, "pct_of_missing": round(100 * v / n, 1)}
                for k, v in sorted(reasons.items(), key=lambda kv: -kv[1])]

    def _missing_order_test(self, name, key, t, miss):
        ok = key.notna()
        if ok.sum() < 20:
            return None
        order = key[ok].sort_values(kind="stable").index
        x = miss.loc[order].to_numpy().astype(int)
        n, n1 = len(x), int(x.sum())
        n0 = n - n1
        if n1 == 0 or n0 == 0:
            return None
        runs = 1 + int((x[1:] != x[:-1]).sum())
        mu = 2 * n0 * n1 / n + 1
        var = 2 * n0 * n1 * (2 * n0 * n1 - n) / (n ** 2 * (n - 1))
        z = (runs - mu) / math.sqrt(var) if var > 0 else 0.0
        p = math.erfc(abs(z) / math.sqrt(2))
        longest, cur = 0, 0
        for v in x:
            cur = cur + 1 if v else 0
            longest = max(longest, cur)
        # longest run expected by chance: about log(n0 * share) / log(1 / share)
        share = n1 / n
        exp_long = math.log(n * (1 - share)) / math.log(1 / share) if 0 < share < 1 else None
        dated = ok & ~miss
        rho = float(key[dated].rank().corr(t[dated].rank())) if dated.sum() >= 10 else None
        dec = pd.qcut(key[ok].rank(method="first"), 10, labels=False)
        by_dec = miss[ok].groupby(dec).mean()
        o = {"order": name, "date_rank_correlation": None if rho is None or np.isnan(rho) else round(rho, 4),
             "runs_test": {"runs": runs, "expected_runs": round(mu, 1), "z": round(z, 3), "p_value": p,
                           "reading": "clustered (fewer, longer runs than chance)" if z < 0 and p < self.ec["alpha"]
                           else "spread out more evenly than chance" if z > 0 and p < self.ec["alpha"]
                           else "consistent with missing at random positions"},
             "longest_missing_run": longest,
             "longest_run_expected_by_chance": None if exp_long is None else round(exp_long, 1),
             "missing_pct_by_tenth": [round(100 * float(v), 1) for v in by_dec.tolist()]}
        if st.have_scipy():
            tab = pd.crosstab(dec, miss[ok])
            if tab.shape[1] == 2:
                chi2, pp, dof, _, v = st.cramers_v(tab)
                o["tenths_chi_square"] = {"chi2": round(float(chi2), 3), "dof": int(dof), "p_value": float(pp),
                                          "cramers_v": round(float(v), 4)}
        return o

    def _place_missing_dates(self, c, order_name, t, miss):
        key = dict(self._order_keys())[order_name]
        ok = key.notna()
        idx = key[ok].sort_values(kind="stable").index
        # a missing date sits between its dated neighbours in this order; the earlier neighbour's date stands in
        placed = t.loc[idx].ffill()
        known = placed.notna()
        frame = pd.DataFrame({"t": placed[known], "missing": miss.loc[idx][known]})
        span = (frame["t"].max() - frame["t"].min()).days
        per = "M" if span > 730 else "W" if span > 90 else "D"
        frame["period"] = frame["t"].dt.to_period(per).astype(str)
        frame["weekday"] = frame["t"].dt.day_name()
        by_p = frame.groupby("period")["missing"].agg(["mean", "size"])
        top = by_p.sort_values("mean", ascending=False).head(5)
        res = {"status": "placed by neighbouring rows", "order": order_name,
               "method": "each missing date takes the date of the previous dated row in this order (approximate)",
               "periods": int(len(by_p)),
               "highest_missing_periods": [{"period": k, "missing_pct": round(100 * float(r["mean"]), 1),
                                            "rows": int(r["size"])} for k, r in top.iterrows()],
               "missing_pct_by_weekday": {k: round(100 * float(v), 1)
                                          for k, v in frame.groupby("weekday")["missing"].mean().items()}}
        if st.have_scipy():
            tab = pd.crosstab(frame["weekday"], frame["missing"])
            if tab.shape == (7, 2) or (tab.shape[0] >= 2 and tab.shape[1] == 2):
                chi2, pp, dof, _, v = st.cramers_v(tab)
                res["weekday_chi_square"] = {"chi2": round(float(chi2), 3), "dof": int(dof), "p_value": float(pp),
                                             "cramers_v": round(float(v), 4)}
                self.add_test("missing dates", {"test": "chi-square", "p_value": float(pp), "statistic": float(chi2)},
                              variables=[c, "weekday"], question=f"does the missing share of {c} differ by weekday?")
        return res

    def write_row_scores(self, scores, listed, cut):
        """One row per scored record, in each method's own units plus its percentile within this dataset. The
        scores rank rows from typical to unusual; they are not probabilities of error and are not comparable
        between datasets. Rows with fewer than half their measures recorded are not scored."""
        out = pd.DataFrame({ROW_ID: self.df.loc[scores.index, ROW_ID].astype(int).values})
        for c in scores.columns:
            v = scores[c].to_numpy()
            out[c] = np.round(v, 6)
            out[f"{c}_percentile"] = np.round(pd.Series(v).rank(pct=True).to_numpy() * 100, 2)
        if "robust_distance_sq" in scores.columns and cut:
            out["beyond_robust_cutoff"] = (scores["robust_distance_sq"] > cut).to_numpy()
        out["listed_as_anomaly"] = listed.reindex(scores.index).fillna(False).to_numpy()
        p = os.path.join(self.out, "analysis", "anomaly_scores.csv")
        os.makedirs(os.path.dirname(p), exist_ok=True)
        out.sort_values(out.columns[1], ascending=False).to_csv(p, index=False, encoding="utf-8", lineterminator="\n")
        return {"file": "analysis/anomaly_scores.csv", "rows": int(len(out)), "columns": list(out.columns),
                "note": "relative scores within this dataset (higher is more unusual), not probabilities of error; "
                        "use the percentile columns to compare methods"}

    def time_series(self):
        if not self.time:
            self.res["time_series"] = {"applicable": False, "reason": "no date column with at least 80% values"}
            return
        t = pd.to_datetime(self.df[self.time], errors="coerce")
        if getattr(t.dt, "tz", None) is not None:
            t = t.dt.tz_convert(None)
        d = self.df.assign(_t=t).dropna(subset=["_t"])
        if d["_t"].nunique() < 6:
            self.res["time_series"] = {"applicable": False, "reason": f"only {d['_t'].nunique()} distinct dates"}
            return
        span = (d["_t"].max() - d["_t"].min()).days
        per, label, ma_w, season = ("M", "month", 3, 12) if span > 730 else ("W", "week", 4, 52) if span > 90             else ("D", "day", 7, 7)
        # the step is measured between distinct timestamps: many rows on one date is not a zero-length step
        uniq = d["_t"].drop_duplicates().sort_values()
        gaps = uniq.diff().dropna()
        regular = bool(len(gaps) and gaps.mode().iloc[0] > pd.Timedelta(0) and (gaps == gaps.mode().iloc[0]).mean() >= 0.8)
        pidx = d["_t"].dt.to_period(per)
        rng = pd.period_range(pidx.min(), pidx.max(), freq=per)
        partial_first = bool(d["_t"].min() > rng[0].start_time)
        partial_last = bool(d["_t"].max() < rng[-1].end_time.normalize())
        out = {"applicable": True, "column": self.time, "start": str(d["_t"].min()), "end": str(d["_t"].max()),
               "span_days": int(span), "frequency": label,
               "sampling": "regular" if regular else "irregular (rows are not evenly spaced in time)",
               "rows_with_valid_date": int(len(d)), "series": {},
               "partial_periods": {"first": partial_first, "last": partial_last,
                                   "note": "partial first/last periods are shown but left out of trend and change figures"}}
        if regular:
            step = gaps.mode().iloc[0]
            big = gaps[gaps > step]
            ts_sorted = uniq
            out["native_step"] = str(step)
            out["missing_steps"] = int(sum(round(g / step) - 1 for g in big))
            out["gaps"] = [{"after": str(ts_sorted.iloc[i - 1])[:19], "before": str(ts_sorted.iloc[i])[:19],
                            "missing_steps": int(round(g / step) - 1)}
                           for i, g in zip(range(1, len(ts_sorted)), ts_sorted.diff().iloc[1:]) if g > step][:20]
        count = d.groupby(pidx)[ROW_ID].count().reindex(rng, fill_value=0)
        empty = count[count == 0]
        out["periods"] = int(len(count))
        out["empty_periods"] = [str(i) for i in empty.index[:50]]
        out["empty_period_count"] = int(len(empty))
        series = {"rows": count}
        aggs = {}
        for m in self.measures[:3]:
            grp = d.groupby(pidx)[m]
            # additive measures are summed; others are averaged, by the median when a few values dominate the mean
            sk = d[m].skew() if d[m].notna().sum() > 2 else 0
            aggs[m] = "sum" if name_has(m, ADDITIVE) else ("median" if abs(sk) > 1 else "mean")
            series[m] = getattr(grp, aggs[m])(**({"min_count": 1} if aggs[m] == "sum" else {})).reindex(rng)
        for name, s_all in series.items():
            s_all = s_all.astype(float)
            s = s_all.iloc[(1 if partial_first else 0): (len(s_all) - 1 if partial_last else len(s_all))].dropna()
            if len(s) < 4:
                continue
            agg = "count" if name == "rows" else aggs[name]
            rec = {"aggregation": agg, "periods": int(len(s)), "first_period": str(s.index[0]),
                   "last_period": str(s.index[-1]), "first": float(s.iloc[0]), "last": float(s.iloc[-1]),
                   "max": {"period": str(s.idxmax()), "value": float(s.max())},
                   "min": {"period": str(s.idxmin()), "value": float(s.min())}}
            if st.have_scipy() and s.std() > 0:
                tr = st.scipy_stats.spearmanr(np.arange(len(s)), s.values)
                slope = np.polyfit(np.arange(len(s)), s.values, 1)[0]
                rec["trend"] = {"spearman_rho": float(tr.statistic), "p_value": float(tr.pvalue),
                                "linear_slope_per_period": float(slope),
                                "direction": "upward" if tr.statistic > 0 else "downward"}
                self.add_test("trend", {"test": "Spearman correlation with time", "p_value": float(tr.pvalue),
                                        "statistic": float(tr.statistic), "n": int(len(s)),
                                        "effect_size": {"spearman_rho": float(tr.statistic),
                                                        "magnitude": st.corr_label(float(tr.statistic))},
                                        "null_hypothesis": f"No monotonic trend in {name} per {label}.",
                                        "alternative_hypothesis": "A monotonic trend exists.",
                                        "why": "rank-based trend test; does not assume linear change"},
                              variables=[name, self.time])
            pop = s.pct_change().replace([np.inf, -np.inf], np.nan)
            rec["last_period_change_pct"] = None if pd.isna(pop.iloc[-1]) else float(100 * pop.iloc[-1])
            rec["first_to_last_change_pct"] = float(100 * (s.iloc[-1] / s.iloc[0] - 1)) if s.iloc[0] else None
            if len(s) >= 2 * season:
                prev = s.iloc[-2 * season:-season].sum()
                rec["last_cycle_vs_previous_pct"] = float(100 * (s.iloc[-season:].sum() / prev - 1)) if prev else None
            if len(s) >= 3 * season and s.std() > 0:
                rec["autocorrelation_at_season_lag"] = {"lag": season, "acf": float(s.autocorr(lag=season))}
            resid = s - s.rolling(5, center=True, min_periods=3).median()
            mad = (resid - resid.median()).abs().median()
            if mad and np.isfinite(mad):
                z = 0.6745 * (resid - resid.median()) / mad
                an = z[z.abs() > 3.5]
                rec["anomalous_periods"] = [{"period": str(i), "value": float(s[i]), "modified_z": float(v)}
                                            for i, v in an.head(10).items()]
            ts_all = s_all.copy()
            ts_all.index = ts_all.index.to_timestamp()
            ma = ts_all.rolling(ma_w, min_periods=1).mean()
            rec["chart"] = self.chart("time series", charts.time_series, ts_all, name if name != "rows" else "Rows",
                                      self.vpath("time_series", name), label, ma)
            out["series"][name] = rec
        if label == "day" and self.measures and st.have_scipy():
            m = self.measures[0]
            wd = d["_t"].dt.day_name()
            rec = st.compare_groups(d[m], wd, sample=self.ec["normality_sample"], seed=self.seed)
            if rec:
                rec.update(measure=m, group="day of week")
                out["day_of_week"] = rec
                self.add_test("seasonality", rec, variables=[m, "day of week"])
        # distribution shift between first and second half of the period
        shifts = []
        if st.have_scipy() and len(d) >= 40:
            mid = d["_t"].quantile(0.5)
            early, late = d[d["_t"] <= mid], d[d["_t"] > mid]
            for m in self.measures[:8]:
                a, b = early[m].dropna(), late[m].dropna()
                if len(a) >= 10 and len(b) >= 10:
                    ks = st.scipy_stats.ks_2samp(a, b)
                    rec = {"test": "two-sample Kolmogorov-Smirnov", "statistic": float(ks.statistic),
                           "p_value": float(ks.pvalue), "n": int(len(a) + len(b)),
                           "effect_size": {"ks_d": float(ks.statistic),
                                           "magnitude": "small" if ks.statistic < 0.1 else "moderate" if ks.statistic < 0.3 else "large"},
                           "null_hypothesis": f"{m} has the same distribution in the first and second half of the period.",
                           "alternative_hypothesis": "The distributions differ.",
                           "why": "compares whole distributions without assuming a shape",
                           "median_first_half": float(a.median()), "median_second_half": float(b.median())}
                    shifts.append({"column": m, **rec})
                    self.add_test("distribution shift", rec, variables=[m, self.time])
        out["distribution_shift"] = shifts
        self.res["time_series"] = out

    def segments(self):
        out = []
        for m in self.measures[:3]:
            if not name_has(m, ADDITIVE) or (self.df[m] < 0).mean() > 0.05:
                continue
            total = float(self.df[m].sum())
            if not total:
                continue
            for g in self.categories[:6]:
                s = self.df.groupby(self.df[g].astype("string"), dropna=True)[m].sum().sort_values(ascending=False)
                if len(s) < 2:
                    continue
                k20 = max(1, int(math.ceil(0.2 * len(s))))
                out.append({"measure": m, "group": g, "levels": int(len(s)),
                            "top_level": str(s.index[0]), "top_share_pct": float(100 * s.iloc[0] / total),
                            "top3_share_pct": float(100 * s.iloc[:3].sum() / total) if len(s) > 3 else None,
                            "top20pct_levels_share_pct": float(100 * s.iloc[:k20].sum() / total) if len(s) >= 5 else None,
                            "shares": [{"level": str(k), "sum": float(v), "share_pct": float(100 * v / total)}
                                       for k, v in s.head(10).items()]})
        ent = [c for c in self.ids if name_has(c, ["customer", "client", "user", "account", "member", "patient", "student"])]
        for e in ent[:1]:
            for m in [m for m in self.measures if name_has(m, ADDITIVE)][:1]:
                s = self.df.groupby(e)[m].sum().sort_values(ascending=False)
                if len(s) >= 10 and s.sum() > 0:
                    k = max(1, int(math.ceil(0.1 * len(s))))
                    out.append({"measure": m, "group": e, "levels": int(len(s)),
                                "top10pct_entities_share_pct": float(100 * s.iloc[:k].sum() / s.sum()),
                                "note": "identifier values are not listed"})
        self.res["segments"] = out

    def target_checks(self):
        if not self.target or self.target not in self.df.columns:
            return
        out = {"target": self.target, "possible_leakage": []}
        t = self.df[self.target]
        if t.dtype.kind in "fi" and t.nunique() > 2:
            for m in self.measures:
                if m == self.target:
                    continue
                r = st.correlation(t, self.df[m]) if st.have_scipy() else None
                if r and abs(r["spearman_rho"]) >= 0.95:
                    out["possible_leakage"].append({"column": m, "spearman_rho": r["spearman_rho"]})
        else:
            for c in self.categories + self.measures:
                if c == self.target:
                    continue
                d = self.df[[c, self.target]].dropna()
                if len(d) >= 20 and d.groupby(c)[self.target].nunique().max() == 1 and d[c].nunique() > 1 \
                        and d[c].nunique() < len(d) * 0.5:
                    out["possible_leakage"].append({"column": c, "reason": "each value of this column maps to exactly one "
                                                    "target value (perfect separation)"})
            out["class_balance"] = {str(k): int(v) for k, v in t.value_counts().items()}
        self.res["target_analysis"] = out

    def correct_and_rank(self):
        q = st.bh_adjust([t.get("p_value") for t in self.tests])
        for t, qq in zip(self.tests, q):
            t["q_value_bh"] = qq
            t["significant_after_correction"] = qq < self.ec["alpha"]
        self.res["tests"] = {"count": len(self.tests), "alpha": self.ec["alpha"],
                             "correction": "Benjamini-Hochberg false discovery rate across all tests in this run",
                             "results": self.tests}
        cands = []
        for c in self.res.get("correlations", []):
            q_ = next((t["q_value_bh"] for t in self.tests if t.get("variables") == [c["a"], c["b"]]), 1)
            if abs(c["spearman_rho"]) >= self.ec["strong_correlation"] and q_ < self.ec["alpha"]:
                cands.append({"type": "correlation", "score": abs(c["spearman_rho"]), "variables": [c["a"], c["b"]],
                              "title": f"{c['a']} and {c['b']} move together" if c["spearman_rho"] > 0 else
                              f"{c['b']} falls as {c['a']} rises",
                              "evidence": {"spearman_rho": c["spearman_rho"], "pearson_r": c["pearson_r"],
                                           "n": c["n"], "q_value": q_},
                              "caveat": "association, not causation"})
        for g in self.res.get("group_comparisons", []):
            t = next((t for t in self.tests if t.get("variables") == [g["measure"], g["group"]]), None)
            if not t or not t["significant_after_correction"]:
                continue
            es = t["effect_size"]
            mag = es.get("magnitude")
            if mag in ("negligible",):
                continue
            val = next(iter(v for k, v in es.items() if k != "magnitude"))
            med = g["group_medians"]
            hi_, lo_ = max(med, key=med.get), min(med, key=med.get)
            cands.append({"type": "group difference", "variables": [g["measure"], g["group"]],
                          "score": min(1.0, abs(val) * (2 if "eta" in str(es) or "epsilon" in str(es) else 1)),
                          "title": f"{g['measure']} differs by {g['group']}",
                          "evidence": {"test": t["test"], "p_value": t["p_value"], "q_value": t["q_value_bh"],
                                       "effect_size": es, "highest_median": [hi_, med[hi_]],
                                       "lowest_median": [lo_, med[lo_]], "group_sizes": g["group_sizes"]},
                          "caveat": "groups may differ in other ways too (confounding)"})
        for md in self.res.get("missing_dates", []):
            for o in md.get("orders", []):
                t = next((t for t in self.tests if t.get("family") == "missing dates"
                          and t.get("variables") == [md["column"], o["order"]]), None)
                if t and t["significant_after_correction"] and o["runs_test"]["z"] < 0:
                    cands.append({"type": "missing data pattern", "score": min(1.0, abs(o["runs_test"]["z"]) / 10),
                                  "variables": [md["column"], o["order"]],
                                  "title": f"Missing {md['column']} values come in runs along {o['order']}",
                                  "evidence": {"runs": o["runs_test"]["runs"],
                                               "expected_runs": o["runs_test"]["expected_runs"],
                                               "q_value": t["q_value_bh"],
                                               "longest_missing_run": o["longest_missing_run"],
                                               "longest_run_expected_by_chance": o["longest_run_expected_by_chance"]},
                                  "caveat": "runs can come from batch loading or a source system outage; check the "
                                            "rows in the longest run before treating them as one cause"})
            wk = (md.get("placement") or {}).get("weekday_chi_square")
            t = next((t for t in self.tests if t.get("variables") == [md["column"], "weekday"]), None)
            if wk and t and t["significant_after_correction"] and wk["cramers_v"] >= 0.1:
                cands.append({"type": "missing data pattern", "score": wk["cramers_v"],
                              "variables": [md["column"], "weekday"],
                              "title": f"The share of missing {md['column']} values differs by weekday",
                              "evidence": {**wk, "q_value": t["q_value_bh"],
                                           "missing_pct_by_weekday": md["placement"]["missing_pct_by_weekday"]},
                              "caveat": "missing dates are placed by their neighbours' dates, so this is approximate"})
        for s in self.res.get("segments", []):
            top, t20, t10 =(s.get("top_share_pct") or 0), (s.get("top20pct_levels_share_pct") or 0), \
                (s.get("top10pct_entities_share_pct") or 0)
            even = 100 / s["levels"] if s.get("levels") else 0
            if (top >= 40 and top >= 1.5 * even) or t20 >= 70 or t10 >= 50:
                cands.append({"type": "concentration", "score": max(top, t20, t10) / 100,
                              "title": f"{s['measure']} is concentrated in a few {s['group']} values",
                              "evidence": {k: v for k, v in s.items() if k not in ("shares",)},
                              "caveat": "describes this dataset's period and coverage only"})
        ts = self.res.get("time_series", {})
        for name, rec in ts.get("series", {}).items():
            tr = rec.get("trend")
            if tr and tr["p_value"] < self.ec["alpha"] and abs(tr["spearman_rho"]) >= 0.5:
                cands.append({"type": "trend", "score": abs(tr["spearman_rho"]),
                              "title": f"{name} shows a {tr['direction']} trend per {ts['frequency']}",
                              "evidence": {"spearman_rho": tr["spearman_rho"], "p_value": tr["p_value"],
                                           "first": rec["first"], "last": rec["last"], "periods": rec["periods"]},
                              "caveat": "a trend over the observed window does not guarantee it continues"})
            acf = rec.get("autocorrelation_at_season_lag")
            if acf and acf["acf"] >= 0.5:
                cands.append({"type": "seasonality", "score": acf["acf"],
                              "title": f"{name} repeats a seasonal pattern (lag {acf['lag']} {ts['frequency']}s)",
                              "evidence": acf, "caveat": "needs at least three full cycles to be reliable"})
        for s in ts.get("distribution_shift", []):
            if s["p_value"] < self.ec["alpha"] and s["statistic"] >= 0.2:
                cands.append({"type": "distribution shift", "score": s["statistic"],
                              "title": f"{s['column']} changed between the first and second half of the period",
                              "evidence": {k: s[k] for k in ("statistic", "p_value", "median_first_half", "median_second_half")},
                              "caveat": "check for changes in definitions, units or collection before comparing periods"})
        for sp in self.res.get("multivariate", {}).get("simpsons_paradox_checks", []):
            cands.append({"type": "simpson's paradox", "score": 0.9,
                          "title": f"The {sp['a']}/{sp['b']} relationship reverses within {sp['group']} groups",
                          "evidence": sp, "caveat": "the overall correlation is misleading; analyse by group"})
        for hp in self.res.get("multivariate", {}).get("highly_correlated_pairs", []):
            cands.append({"type": "collinearity", "score": 0.3,
                          "title": f"{hp['a']} and {hp['b']} carry nearly the same information",
                          "evidence": hp, "caveat": "use one of them in regression models, or combine them"})
        cands.sort(key=lambda c: -c["score"])
        for i, c in enumerate(cands, 1):
            c["candidate_id"] = f"C{i:02d}"
        self.res["finding_candidates"] = cands

    def charts_for_top(self):
        for c in self.res.get("finding_candidates", [])[:6]:
            if c["type"] == "correlation":
                a, b = c["variables"]
                ev = c["evidence"]
                c["chart"] = self.chart("scatter", charts.scatter, self.df[a], self.df[b], a, b,
                                        self.vpath("relationships", f"{a}_vs_{b}"),
                                        f"Spearman rho = {ev['spearman_rho']:.2f}, n = {ev['n']:,}",
                                        self.ec["scatter_sample"], self.seed)
            elif c["type"] == "group difference":
                m, g = c["variables"]
                c["chart"] = self.chart("box", charts.box_by_group, self.df[m], self.df[g].astype("string").fillna(""), m, g,
                                        self.vpath("relationships", f"{m}_by_{g}"),
                                        f"{c['evidence']['test']}, q = {c['evidence']['q_value']:.3g}")

    def run(self):
        self.univariate()
        self.bivariate()
        self.multivariate()
        self.time_series()
        self.missing_dates()
        self.segments()
        self.target_checks()
        self.correct_and_rank()
        self.charts_for_top()
        if self.n < 30:
            self.res.setdefault("limitations", []).append(f"only {self.n} rows; tests have little power and estimates are unstable")
        man = read_json(os.path.join(self.out, "metadata", "analysis_manifest.json"))
        if man.get("analysis_scope", {}).get("sampled"):
            self.res.setdefault("limitations", []).append("analysis ran on a random sample, not the full file")
        write_json(os.path.join(self.out, "analysis", "eda_results.json"), self.res)
        write_json(os.path.join(self.out, "visualisations", "chart_index.json"),
                   {"library": charts.library() if self.charts_on else "none (charts off or matplotlib missing)",
                    "charts": self.res["charts"],
                    "note": "each chart's numbers are in analysis/eda_results.json under the section named by its kind"})
        return self.res
