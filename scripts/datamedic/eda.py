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
from .common import ROW_ID, load_config, read_json, write_json
from .profile import name_has
from .validate import typed_view

ADDITIVE = ["revenue", "sales", "amount", "total", "quantity", "qty", "units", "count", "cost", "spend", "profit",
            "income", "orders", "clicks", "impressions", "conversions", "visits", "sessions", "volume"]


def _rel(out_dir, p):
    return os.path.relpath(p, out_dir).replace("\\", "/") if p else None


class EDA:
    def __init__(self, out_dir, target=None, time_col=None, cfg=None, charts_on=True):
        self.out = out_dir
        self.cfg = cfg or load_config()
        self.ec = self.cfg["eda"]
        self.seed = self.cfg.get("seed", 42)
        self.plan = read_json(os.path.join(out_dir, "plan.json"))
        self.schema = read_json(os.path.join(out_dir, "cleaned_data", "schema.json"))
        self.prof = read_json(os.path.join(out_dir, "profiling", "initial_profile.json"))
        raw = read_str_csv(os.path.join(out_dir, "cleaned_data", "cleaned_dataset.csv"))
        self.df = typed_view(raw, self.schema)
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
        self.measures = self.measures[: self.ec["max_numeric_columns"]]
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
        self.chart_count = 0
        self.tests = []
        self.res = {"rows": self.n, "columns": len(cols), "measures": self.measures, "categories": self.categories,
                    "time_column": self.time, "target": self.target, "identifiers": sorted(ids),
                    "sensitive_columns_excluded_from_value_reports": sorted(self.sensitive),
                    "domain": self.prof.get("domain"), "charts": []}

    # ------------------------------------------------------------ helpers
    def chart(self, kind, fn, *a, **k):
        if not self.charts_on or self.chart_count >= self.ec["max_charts"]:
            return None
        try:
            p = fn(*a, **k)
            self.chart_count += 1
            rp = _rel(self.out, p)
            self.res["charts"].append({"kind": kind, "path": rp})
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
            if len(num) <= 6:
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
            if len(cat) <= 4:
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
        self.res["multivariate"] = out

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
        gaps = d["_t"].sort_values().diff().dropna()
        regular = bool(len(gaps) and (gaps == gaps.mode().iloc[0]).mean() >= 0.8)
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
            ts_sorted = d["_t"].sort_values()
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
            if st.have_scipy():
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
            for m in self.measures[:1]:
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
        for s in self.res.get("segments", []):
            top, t20, t10 = (s.get("top_share_pct") or 0), (s.get("top20pct_levels_share_pct") or 0), \
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
        return self.res
