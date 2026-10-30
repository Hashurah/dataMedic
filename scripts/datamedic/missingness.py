"""Missingness diagnostics: how much, where, together with what, over time, and Little's MCAR test.

None of these establish why values are missing. A relationship with an observed column makes
"missing at random given that column" (MAR) plausible; no relationship is consistent with
"missing completely at random" (MCAR); "missing because of the value itself" (MNAR) cannot be
detected from the observed data at all. Every result carries that caveat.
"""

import numpy as np
import pandas as pd

from . import stats as st
from .clean import _is_missing, _num
from .common import ROW_ID

CAVEAT = ("Observational data cannot establish why values are missing. These results can contradict MCAR or make "
          "MAR plausible; MNAR (missing because of the unseen value itself) cannot be tested from the data.")


def _em_mvn(X, max_iter=200, tol=1e-6):
    """Maximum-likelihood mean and covariance of a multivariate normal with missing values (EM)."""
    n, p = X.shape
    mu = np.nanmean(X, axis=0)
    S = np.diag(np.nanvar(X, axis=0) + 1e-9)
    miss = np.isnan(X)
    patterns = {}
    for i, row in enumerate(miss):
        patterns.setdefault(row.tobytes(), []).append(i)
    pats = [(np.frombuffer(k, dtype=bool), np.array(v)) for k, v in patterns.items()]
    for _ in range(max_iter):
        sx = np.zeros(p)
        sxx = np.zeros((p, p))
        for m, rows in pats:
            o = ~m
            Xo = X[np.ix_(rows, np.flatnonzero(o))]
            full = np.empty((len(rows), p))
            full[:, o] = Xo
            C = np.zeros((p, p))
            if m.any():
                Soo_inv = np.linalg.pinv(S[np.ix_(o, o)])
                B = S[np.ix_(m, o)] @ Soo_inv
                full[:, m] = mu[m] + (Xo - mu[o]) @ B.T
                C[np.ix_(m, m)] = S[np.ix_(m, m)] - B @ S[np.ix_(o, m)]
            sx += full.sum(axis=0)
            sxx += full.T @ full + len(rows) * C
        mu_new = sx / n
        S_new = sxx / n - np.outer(mu_new, mu_new)
        done = np.max(np.abs(mu_new - mu)) < tol and np.max(np.abs(S_new - S)) < tol
        mu, S = mu_new, S_new
        if done:
            break
    return mu, S, pats


def littles_mcar(frame, max_columns=10):
    """Little's (1988) chi-square test of MCAR for numeric columns. Assumes multivariate normality."""
    cols = [c for c in frame.columns][:max_columns]
    X = frame[cols].to_numpy(dtype=float)
    X = X[~np.isnan(X).all(axis=1)]
    if X.shape[0] < 30 or not np.isnan(X).any():
        return None
    sd = np.nanstd(X, axis=0)
    keep = sd > 0
    if keep.sum() < 2:
        return None
    X = (X[:, keep] - np.nanmean(X[:, keep], axis=0)) / sd[keep]
    cols = [c for c, k in zip(cols, keep) if k]
    mu, S, pats = _em_mvn(X)
    d2, dof = 0.0, 0
    for m, rows in pats:
        o = ~m
        if not o.any():
            continue
        ybar = np.nanmean(X[np.ix_(rows, np.flatnonzero(o))], axis=0)
        diff = ybar - mu[o]
        d2 += len(rows) * float(diff @ np.linalg.pinv(S[np.ix_(o, o)]) @ diff)
        dof += int(o.sum())
    dof -= X.shape[1]
    if dof <= 0:
        return None
    p = float(st.scipy_stats.chi2.sf(d2, dof))
    return {"test": "Little's MCAR test", "columns": cols, "statistic": d2, "df": dof, "p_value": p,
            "patterns": len(pats), "rows": int(X.shape[0]),
            "reading": ("evidence against MCAR: missingness in these columns is related to observed values"
                        if p < 0.05 else "no evidence against MCAR (this does not prove MCAR)"),
            "assumptions": "multivariate normal data; low power with few rows or many patterns"}


def diagnose(df, time_col=None, max_patterns=10):
    """Missingness summary for a typed table (with ROW_ID)."""
    cols = [c for c in df.columns if c != ROW_ID]
    M = pd.DataFrame({c: _is_missing(df[c]) for c in cols})
    n = len(df)
    per = {c: {"missing": int(M[c].sum()), "pct": round(100 * float(M[c].mean()), 2)} for c in cols if M[c].any()}
    out = {"rows": n, "columns_with_missing": len(per), "per_column": per, "caveat": CAVEAT}
    if not per:
        out["note"] = "no missing values"
        return out
    mc = [c for c in cols if M[c].any()]
    pat = M[mc].apply(lambda r: tuple(c for c, v in zip(mc, r) if v), axis=1)
    vc = pat[pat.map(len) > 0].value_counts()
    out["row_patterns"] = [{"missing_columns": list(k), "rows": int(v), "pct": round(100 * v / n, 2)}
                           for k, v in vc.head(max_patterns).items()]
    out["rows_with_any_missing"] = int((pat.map(len) > 0).sum())
    together, structural = [], []
    for i, a in enumerate(mc):
        for b in mc[i + 1:]:
            both = int((M[a] & M[b]).sum())
            union = int((M[a] | M[b]).sum())
            if both >= 5 and union and both / union >= 0.5:
                together.append({"a": a, "b": b, "both_missing": both, "jaccard": round(both / union, 3)})
        for b in mc:
            if b != a and M[a].sum() >= 5 and bool((M[b][M[a]]).all()) and M[b].sum() > M[a].sum():
                structural.append({"when_missing": a, "always_missing": b, "rows": int(M[a].sum()),
                                   "note": f"every row missing {a} is also missing {b}; may be structural "
                                           "(a question skipped by design) rather than lost data"})
    out["missing_together"] = sorted(together, key=lambda r: -r["jaccard"])[:20]
    out["structural_candidates"] = structural[:20]
    if time_col and time_col in df.columns and st.have_scipy():
        t = pd.to_datetime(df[time_col], errors="coerce")
        if getattr(t.dt, "tz", None) is not None:
            t = t.dt.tz_convert(None)
        if t.notna().sum() >= 30:
            span = (t.max() - t.min()).days
            per_code = "M" if span > 730 else "W" if span > 90 else "D"
            period = t.dt.to_period(per_code)
            over = {}
            for c in mc:
                if c == time_col:
                    continue
                ok = period.notna()
                rate = M.loc[ok, c].groupby(period[ok]).mean()
                if len(rate) < 3:
                    continue
                res = st.chi_square(M.loc[ok, c].astype(int), period[ok].astype(str))
                over[c] = {"periods": int(len(rate)), "highest_rate_period": str(rate.idxmax()),
                           "highest_rate_pct": round(100 * float(rate.max()), 2),
                           "lowest_rate_pct": round(100 * float(rate.min()), 2),
                           **({"test": res["test"], "p_value": res["p_value"],
                               "cramers_v": res["effect_size"].get("cramers_v")} if res else {})}
            out["over_time"] = {"time_column": time_col, "period": {"M": "month", "W": "week", "D": "day"}[per_code],
                                "columns": over}
    num = [c for c in mc if pd.api.types.is_numeric_dtype(df[c]) and not pd.api.types.is_bool_dtype(df[c])]
    others = [c for c in cols if c not in num and pd.api.types.is_numeric_dtype(df[c])
              and not pd.api.types.is_bool_dtype(df[c])]
    if num and st.have_scipy():
        frame = pd.DataFrame({c: _num(df[c]) for c in (num + others)[:10]})
        try:
            out["littles_mcar"] = littles_mcar(frame) or {"status": "not run: too few rows, columns or patterns"}
        except np.linalg.LinAlgError as exc:
            out["littles_mcar"] = {"status": f"not run: {exc}"}
    return out
