"""Statistical helpers: test selection with assumption checks, effect sizes, multiple-testing correction."""

import math

import numpy as np
import pandas as pd

from .common import optional

scipy_stats = None
_sp = optional("scipy")
if _sp is not None:
    from scipy import stats as scipy_stats  # noqa: F811


def have_scipy():
    return scipy_stats is not None


def bh_adjust(pvals):
    """Benjamini-Hochberg adjusted p-values (q-values), same order as input."""
    p = np.asarray([1.0 if v is None or not np.isfinite(v) else v for v in pvals], dtype=float)
    n = len(p)
    if n == 0:
        return []
    order = np.argsort(p)
    ranked = p[order] * n / (np.arange(n) + 1)
    q = np.minimum.accumulate(ranked[::-1])[::-1]
    out = np.empty(n)
    out[order] = np.minimum(q, 1.0)
    return out.tolist()


def describe_numeric(x):
    x = pd.Series(x, dtype=float).dropna()
    n = len(x)
    if not n:
        return {"n": 0}
    q1, med, q3 = x.quantile([0.25, 0.5, 0.75]).tolist()
    mode = x.mode()
    d = {"n": n, "mean": float(x.mean()), "median": float(med), "mode": float(mode.iloc[0]) if len(mode) else None,
         "mode_count": int((x == mode.iloc[0]).sum()) if len(mode) else 0,
         "min": float(x.min()), "max": float(x.max()), "range": float(x.max() - x.min()),
         "variance": float(x.var(ddof=1)) if n > 1 else 0.0, "std": float(x.std(ddof=1)) if n > 1 else 0.0,
         "q1": float(q1), "q3": float(q3), "iqr": float(q3 - q1)}
    if n > 2 and x.std() > 0:
        d["skewness"] = float(x.skew())
        d["kurtosis_excess"] = float(x.kurt())
    d["shape"] = shape_label(d)
    if n > 1:
        se = d["std"] / math.sqrt(n)
        t = scipy_stats.t.ppf(0.975, n - 1) if have_scipy() else 1.96
        d["mean_ci95"] = [d["mean"] - t * se, d["mean"] + t * se]
    return d


def shape_label(d):
    sk = d.get("skewness")
    if sk is None:
        return "constant" if d.get("std") == 0 else "too few values"
    if abs(sk) < 0.5:
        s = "roughly symmetric"
    elif abs(sk) < 1:
        s = "moderately " + ("right" if sk > 0 else "left") + "-skewed"
    else:
        s = "strongly " + ("right" if sk > 0 else "left") + "-skewed"
    k = d.get("kurtosis_excess", 0)
    if k > 3:
        s += ", heavy-tailed"
    elif k < -1:
        s += ", light-tailed or flat"
    return s


def normal_enough(x, sample=5000, seed=42):
    """Shapiro-Wilk on up to `sample` values, plus a skew check. Large samples lean on the CLT."""
    x = pd.Series(x, dtype=float).dropna()
    if len(x) < 3 or not have_scipy():
        return False, None
    xs = x.sample(n=min(len(x), sample), random_state=seed) if len(x) > sample else x
    if xs.std() == 0:
        return False, None
    p = float(scipy_stats.shapiro(xs).pvalue)
    skew = float(x.skew())
    ok = p > 0.05 or (len(x) >= 30 and abs(skew) < 1)
    return ok, p


def cramers_v(table):
    chi2, p, dof, expected = scipy_stats.chi2_contingency(table, correction=False)
    n = table.values.sum()
    r, k = table.shape
    v = math.sqrt(chi2 / (n * max(1, min(r - 1, k - 1)))) if n else 0.0
    return chi2, p, dof, expected, v


def association_label(v):
    return "negligible" if v < 0.1 else "weak" if v < 0.3 else "moderate" if v < 0.5 else "strong"


def corr_label(r):
    a = abs(r)
    return "negligible" if a < 0.1 else "weak" if a < 0.3 else "moderate" if a < 0.5 else "strong" if a < 0.8 else "very strong"


def pearson_ci(r, n):
    if n <= 3 or abs(r) >= 1:
        return None
    z = math.atanh(r)
    se = 1 / math.sqrt(n - 3)
    return [math.tanh(z - 1.96 * se), math.tanh(z + 1.96 * se)]


def correlation(x, y):
    """Pearson and Spearman on paired non-missing values."""
    d = pd.concat([pd.Series(x, dtype=float), pd.Series(y, dtype=float)], axis=1).dropna()
    n = len(d)
    if n < 5 or d.iloc[:, 0].std() == 0 or d.iloc[:, 1].std() == 0:
        return None
    pr = scipy_stats.pearsonr(d.iloc[:, 0], d.iloc[:, 1])
    sr = scipy_stats.spearmanr(d.iloc[:, 0], d.iloc[:, 1])
    return {"n": n, "pearson_r": float(pr.statistic), "pearson_p": float(pr.pvalue),
            "pearson_ci95": pearson_ci(float(pr.statistic), n), "spearman_rho": float(sr.statistic),
            "spearman_p": float(sr.pvalue)}


def compare_groups(values, groups, min_group=5, alpha=0.05, sample=5000, seed=42):
    """Choose and run a test comparing a numeric variable across groups. Returns a full test record."""
    d = pd.DataFrame({"v": pd.Series(values, dtype=float).values, "g": pd.Series(groups).astype(str).values}).dropna()
    d = d[d["g"] != ""]
    sizes = d["g"].value_counts()
    keep = sizes[sizes >= min_group].index
    d = d[d["g"].isin(keep)]
    k = d["g"].nunique()
    if k < 2:
        return None
    arrays = [g["v"].values for _, g in d.groupby("g")]
    names = [str(n) for n, _ in d.groupby("g")]
    normal = all(normal_enough(a, sample, seed)[0] for a in arrays)
    lev_p = float(scipy_stats.levene(*arrays, center="median").pvalue) if all(len(a) > 1 for a in arrays) else None
    equal_var = lev_p is not None and lev_p > alpha
    rec = {"groups": k, "group_sizes": {n: int(len(a)) for n, a in zip(names, arrays)},
           "group_medians": {n: float(np.median(a)) for n, a in zip(names, arrays)},
           "group_means": {n: float(np.mean(a)) for n, a in zip(names, arrays)},
           "assumptions": {"approximately_normal_in_every_group": normal, "levene_p": lev_p,
                           "equal_variances": equal_var}, "n": int(len(d))}
    if k == 2:
        a, b = arrays
        rec["null_hypothesis"] = f"The distributions of the value are the same in '{names[0]}' and '{names[1]}'."
        rec["alternative_hypothesis"] = "They differ (two-sided)."
        if normal:
            res = scipy_stats.ttest_ind(a, b, equal_var=False)
            sp = math.sqrt((np.var(a, ddof=1) + np.var(b, ddof=1)) / 2) or float("nan")
            dd = (np.mean(a) - np.mean(b)) / sp if sp else float("nan")
            se = math.sqrt(np.var(a, ddof=1) / len(a) + np.var(b, ddof=1) / len(b))
            dfw = se ** 4 / ((np.var(a, ddof=1) / len(a)) ** 2 / (len(a) - 1) + (np.var(b, ddof=1) / len(b)) ** 2 / (len(b) - 1)) if se else 1
            t = scipy_stats.t.ppf(0.975, dfw)
            diff = np.mean(a) - np.mean(b)
            rec.update(test="Welch's t-test", why="both groups are approximately normal (or large with mild skew); "
                       "Welch's version does not assume equal variances", statistic=float(res.statistic),
                       p_value=float(res.pvalue), effect_size={"cohens_d": float(dd), "magnitude": _d_label(dd)},
                       ci95_mean_difference=[float(diff - t * se), float(diff + t * se)],
                       null_hypothesis=f"The mean is the same in '{names[0]}' and '{names[1]}'.")
        else:
            res = scipy_stats.mannwhitneyu(a, b, alternative="two-sided")
            r = 1 - 2 * float(res.statistic) / (len(a) * len(b))
            rec.update(test="Mann-Whitney U", why="at least one group is clearly non-normal, so a rank-based test is used",
                       statistic=float(res.statistic), p_value=float(res.pvalue),
                       effect_size={"rank_biserial": -r, "magnitude": association_label(abs(r))})
    else:
        rec["null_hypothesis"] = f"The value has the same distribution in all {k} groups."
        rec["alternative_hypothesis"] = "At least one group differs."
        n = len(d)
        if normal and equal_var:
            res = scipy_stats.f_oneway(*arrays)
            grand = d["v"].mean()
            ssb = sum(len(a) * (np.mean(a) - grand) ** 2 for a in arrays)
            sst = ((d["v"] - grand) ** 2).sum()
            eta = float(ssb / sst) if sst else 0.0
            rec.update(test="one-way ANOVA", why="groups are approximately normal with similar variances",
                       statistic=float(res.statistic), p_value=float(res.pvalue),
                       effect_size={"eta_squared": eta, "magnitude": _eta_label(eta)},
                       null_hypothesis=f"The mean is the same in all {k} groups.")
        else:
            res = scipy_stats.kruskal(*arrays)
            eps = float((res.statistic - k + 1) / (n - k)) if n > k else 0.0
            rec.update(test="Kruskal-Wallis H", why="normality or equal variances do not hold, so a rank-based test is used",
                       statistic=float(res.statistic), p_value=float(res.pvalue),
                       effect_size={"epsilon_squared": max(0.0, eps), "magnitude": _eta_label(max(0.0, eps))})
    return rec


def chi_square(a, b, min_expected=5):
    t = pd.crosstab(pd.Series(a).astype(str), pd.Series(b).astype(str))
    if t.shape[0] < 2 or t.shape[1] < 2:
        return None
    chi2, p, dof, expected, v = cramers_v(t)
    low = float((expected < min_expected).mean())
    rec = {"test": "chi-square test of independence", "statistic": float(chi2), "p_value": float(p), "dof": int(dof),
           "n": int(t.values.sum()), "effect_size": {"cramers_v": v, "magnitude": association_label(v)},
           "assumptions": {"share_of_cells_expected_below_5": low},
           "null_hypothesis": "The two variables are independent.", "alternative_hypothesis": "They are associated.",
           "why": "both variables are categorical"}
    if t.shape == (2, 2) and low > 0:
        odds, pf = scipy_stats.fisher_exact(t.values)
        rec.update(test="Fisher's exact test", p_value=float(pf), statistic=float(odds),
                   why="2x2 table with small expected counts, so the exact test replaces chi-square")
    elif low > 0.2:
        rec["warning"] = "more than 20% of cells have expected counts below 5; the chi-square p-value is unreliable"
    return rec


def _d_label(d):
    a = abs(d)
    return "negligible" if a < 0.2 else "small" if a < 0.5 else "medium" if a < 0.8 else "large"


def _eta_label(e):
    return "negligible" if e < 0.01 else "small" if e < 0.06 else "medium" if e < 0.14 else "large"


def vif(frame):
    """Variance inflation factors via least squares (no statsmodels needed)."""
    X = frame.dropna()
    if len(X) < X.shape[1] + 5:
        return {}
    X = (X - X.mean()) / X.std().replace(0, np.nan)
    X = X.dropna(axis=1)
    out = {}
    for c in X.columns:
        y = X[c].values
        others = X.drop(columns=[c]).values
        if others.shape[1] == 0:
            continue
        A = np.column_stack([np.ones(len(y)), others])
        beta, *_ = np.linalg.lstsq(A, y, rcond=None)
        resid = y - A @ beta
        r2 = 1 - resid.var() / y.var() if y.var() else 0
        out[c] = float("inf") if r2 >= 1 else float(1 / (1 - r2))
    return out
