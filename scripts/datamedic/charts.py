"""Charts with matplotlib (Agg backend): titled, labelled, sample sizes shown, saved at 150 dpi."""

import os

import numpy as np
import pandas as pd

from .common import optional

_mpl = optional("matplotlib")
if _mpl is not None:
    _mpl.use("Agg")
    import matplotlib.pyplot as plt
else:  # pragma: no cover
    plt = None

INK, MUTED, ACCENT, ACCENT2, GRID = "#1f2933", "#616e7c", "#2563eb", "#d97706", "#e4e7eb"


def available():
    return plt is not None


def _style(ax, title, xlabel, ylabel):
    ax.set_title(title, fontsize=11, color=INK, loc="left")
    ax.set_xlabel(xlabel, color=MUTED)
    ax.set_ylabel(ylabel, color=MUTED)
    ax.grid(axis="y", color=GRID, linewidth=0.8)
    ax.set_axisbelow(True)
    for s in ("top", "right"):
        ax.spines[s].set_visible(False)
    ax.tick_params(colors=MUTED, labelsize=8)


def _save(fig, path):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fig.tight_layout()
    fig.savefig(path, dpi=150)
    plt.close(fig)
    return path


def _short(s, n=22):
    s = str(s)
    return s if len(s) <= n else s[: n - 1] + "…"


def needs_log(x):
    """Positive data where a few extreme values would squash the rest: show it on a log scale."""
    x = pd.Series(x, dtype=float).dropna()
    if len(x) < 5 or (x <= 0).any():
        return False
    q75 = x.quantile(0.75)
    return bool(q75 > 0 and x.max() / q75 > 20)


def distribution(x, name, path, unit=""):
    x = pd.Series(x, dtype=float).dropna()
    log = needs_log(x)
    fig, (a1, a2) = plt.subplots(2, 1, figsize=(7, 4.6), gridspec_kw={"height_ratios": [3, 1]}, sharex=True)
    nb = min(60, max(10, int(np.sqrt(len(x)))))
    bins = np.logspace(np.log10(x.min()), np.log10(x.max()), nb) if log else nb
    a1.hist(x, bins=bins, color=ACCENT, alpha=0.85, edgecolor="white", linewidth=0.4)
    if log:
        a1.set_xscale("log")
    a1.axvline(x.mean(), color=ACCENT2, linewidth=1.4, label=f"mean {x.mean():.4g}")
    a1.axvline(x.median(), color=INK, linewidth=1.2, linestyle="--", label=f"median {x.median():.4g}")
    a1.legend(frameon=False, fontsize=8)
    _style(a1, f"Distribution of {name} (n = {len(x):,})", "", "Count of rows")
    a2.boxplot(x, vert=False, widths=0.6, flierprops={"markersize": 2, "markerfacecolor": MUTED, "alpha": 0.5})
    a2.set_yticks([])
    _style(a2, "", name + (f" ({unit})" if unit else "") + (", log scale" if log else ""), "")
    return _save(fig, path)


def bar_counts(counts, name, path, total):
    counts = counts.head(15)
    fig, ax = plt.subplots(figsize=(7, max(2.6, 0.32 * len(counts) + 1)))
    labels = [_short(i) for i in counts.index[::-1]]
    ax.barh(labels, counts.values[::-1], color=ACCENT)
    for i, v in enumerate(counts.values[::-1]):
        ax.text(v, i, f" {v:,} ({100 * v / total:.1f}%)", va="center", fontsize=7, color=MUTED)
    _style(ax, f"{name}: most frequent values (n = {total:,})", "Count of rows", "")
    ax.grid(axis="x", color=GRID)
    ax.grid(axis="y", visible=False)
    return _save(fig, path)


def heatmap(corr, path, title):
    k = len(corr)
    fig, ax = plt.subplots(figsize=(min(10, 1.2 + 0.55 * k), min(9, 1 + 0.5 * k)))
    im = ax.imshow(corr.values, cmap="RdBu_r", vmin=-1, vmax=1)
    ax.set_xticks(range(k))
    ax.set_yticks(range(k))
    ax.set_xticklabels([_short(c, 16) for c in corr.columns], rotation=45, ha="right", fontsize=7)
    ax.set_yticklabels([_short(c, 16) for c in corr.index], fontsize=7)
    if k <= 14:
        for i in range(k):
            for j in range(k):
                v = corr.values[i, j]
                if np.isfinite(v):
                    ax.text(j, i, f"{v:.2f}", ha="center", va="center", fontsize=6,
                            color="white" if abs(v) > 0.6 else INK)
    fig.colorbar(im, ax=ax, fraction=0.046, pad=0.04, label="correlation")
    ax.set_title(title, fontsize=11, color=INK, loc="left")
    return _save(fig, path)


def scatter(x, y, xn, yn, path, stats_text, sample=5000, seed=42):
    d = pd.concat([pd.Series(x, dtype=float), pd.Series(y, dtype=float)], axis=1).dropna()
    n = len(d)
    if n > sample:
        d = d.sample(n=sample, random_state=seed)
    fig, ax = plt.subplots(figsize=(6.4, 4.6))
    ax.scatter(d.iloc[:, 0], d.iloc[:, 1], s=8, alpha=0.4, color=ACCENT, edgecolors="none")
    lx, ly = needs_log(d.iloc[:, 0]), needs_log(d.iloc[:, 1])
    if lx:
        ax.set_xscale("log")
    if ly:
        ax.set_yscale("log")
    if n >= 3 and d.iloc[:, 0].std() > 0 and not (lx or ly):
        b, a = np.polyfit(d.iloc[:, 0], d.iloc[:, 1], 1)
        xs = np.linspace(d.iloc[:, 0].min(), d.iloc[:, 0].max(), 50)
        ax.plot(xs, a + b * xs, color=ACCENT2, linewidth=1.4, label="least-squares line")
        ax.legend(frameon=False, fontsize=8)
    sub = f"n = {n:,}" + (f", {sample:,} points shown" if n > sample else "")
    _style(ax, f"{yn} against {xn} ({sub})\n{stats_text}", xn + (" (log scale)" if lx else ""),
           yn + (" (log scale)" if ly else ""))
    return _save(fig, path)


def box_by_group(values, groups, vn, gn, path, stats_text, max_groups=12):
    d = pd.DataFrame({"v": pd.Series(values, dtype=float).values, "g": pd.Series(groups).astype(str).values}).dropna()
    d = d[d["g"] != ""]
    order = d.groupby("g")["v"].median().sort_values(ascending=False).index[:max_groups]
    data = [d.loc[d["g"] == g, "v"].values for g in order]
    fig, ax = plt.subplots(figsize=(7, 4.4))
    ax.boxplot(data, widths=0.6, flierprops={"markersize": 2, "alpha": 0.4})
    log = needs_log(d["v"])
    if log:
        ax.set_yscale("log")
    ax.set_xticks(range(1, len(order) + 1))
    ax.set_xticklabels([f"{_short(g, 14)}\n(n={len(a):,})" for g, a in zip(order, data)], fontsize=7)
    _style(ax, f"{vn} by {gn}\n{stats_text}", gn, vn + (" (log scale)" if log else ""))
    return _save(fig, path)


def time_series(series, name, path, freq_label, ma=None):
    fig, ax = plt.subplots(figsize=(7.5, 3.8))
    ax.plot(series.index, series.values, color=ACCENT, linewidth=1.3, marker="o" if len(series) < 40 else None,
            markersize=3, label=name)
    if ma is not None:
        ax.plot(ma.index, ma.values, color=ACCENT2, linewidth=1.4, label="moving average")
        ax.legend(frameon=False, fontsize=8)
    _style(ax, f"{name} per {freq_label} ({len(series)} periods)", "Period", name)
    fig.autofmt_xdate()
    return _save(fig, path)


def before_after(before, after, name, path):
    b = pd.Series(before, dtype=float).dropna()
    a = pd.Series(after, dtype=float).dropna()
    fig, ax = plt.subplots(figsize=(7, 3.6))
    lo, hi = min(b.min(), a.min()), max(b.max(), a.max())
    bins = np.linspace(lo, hi, 40) if hi > lo else 10
    ax.hist(a, bins=bins, color=ACCENT2, alpha=0.55, label=f"after cleaning (n = {len(a):,})")
    ax.hist(b, bins=bins, color=ACCENT, alpha=0.6, label=f"before cleaning (n = {len(b):,})")
    ax.legend(frameon=False, fontsize=8)
    _style(ax, f"{name}: distribution before and after cleaning", name, "Count of rows")
    return _save(fig, path)
