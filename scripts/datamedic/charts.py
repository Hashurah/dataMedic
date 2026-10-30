"""Charts: matplotlib (Agg backend) as the base, seaborn for statistical charts when installed.

Every chart is titled, labelled and shows its sample size. One theme (colours, type, grid) is shared
by all charts and matches the HTML report. PNG is always written; SVG and PDF copies are added when
config `charts.formats` asks for them. Without seaborn the same charts are drawn with matplotlib alone.
"""

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
_sns = optional("seaborn") if _mpl is not None else None

INK, MUTED, ACCENT, ACCENT2, GRID = "#0f172a", "#475569", "#0f766e", "#b45309", "#e2e8f0"
NAVY, RED, GREEN = "#1e3a5f", "#b91c1c", "#15803d"
PALETTE = [ACCENT, NAVY, ACCENT2, "#7c3aed", "#0369a1", "#be185d", "#4d7c0f", "#64748b"]
SETTINGS = {"formats": ["png"], "dpi": 150, "use_seaborn": "auto"}


def configure(cfg=None):
    """Apply config `charts` settings (formats, dpi, seaborn on/off) and the shared theme."""
    SETTINGS.update((cfg or {}).get("charts") or {})
    if plt is None:
        return
    plt.rcParams.update({
        "font.family": ["DejaVu Sans"], "font.size": 9, "axes.titlesize": 11,
        "axes.edgecolor": GRID, "axes.labelcolor": MUTED, "xtick.color": MUTED, "ytick.color": MUTED,
        "axes.prop_cycle": _mpl.cycler(color=PALETTE), "figure.facecolor": "white", "savefig.facecolor": "white",
    })


def available():
    return plt is not None


def seaborn_on():
    return _sns is not None and SETTINGS.get("use_seaborn", "auto") in ("auto", True, "true")


def library():
    return "seaborn + matplotlib" if seaborn_on() else "matplotlib"


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
    fig.savefig(path, dpi=SETTINGS.get("dpi", 150))
    base = os.path.splitext(path)[0]
    for fmt in SETTINGS.get("formats", ["png"]):
        if fmt in ("svg", "pdf"):
            fig.savefig(f"{base}.{fmt}")
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
    if seaborn_on():
        _sns.histplot(x=x, bins=nb, kde=len(x) >= 20 and x.nunique() > 5, color=ACCENT, edgecolor="white",
                      linewidth=0.4, alpha=0.8, ax=a1, log_scale=log)
    else:
        a1.hist(x, bins=bins, color=ACCENT, alpha=0.85, edgecolor="white", linewidth=0.4)
        if log:
            a1.set_xscale("log")
    a1.axvline(x.mean(), color=ACCENT2, linewidth=1.4, label=f"mean {x.mean():.4g}")
    a1.axvline(x.median(), color=INK, linewidth=1.2, linestyle="--", label=f"median {x.median():.4g}")
    a1.legend(frameon=False, fontsize=8)
    _style(a1, f"Distribution of {name} (n = {len(x):,})", "", "Count of rows")
    box = dict(widths=0.6, flierprops={"markersize": 2, "markerfacecolor": MUTED, "alpha": 0.5},
               medianprops={"color": INK}, boxprops={"color": MUTED}, whiskerprops={"color": MUTED},
               capprops={"color": MUTED})
    try:
        a2.boxplot(x, orientation="horizontal", **box)
    except TypeError:  # matplotlib before 3.10
        a2.boxplot(x, vert=False, **box)
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
    fig, ax = plt.subplots(figsize=(min(10, 1.6 + 0.6 * k), min(9, 1.2 + 0.5 * k)))
    labels = [_short(c, 16) for c in corr.columns]
    if seaborn_on():
        mask = np.triu(np.ones_like(corr.values, dtype=bool), k=1)
        _sns.heatmap(corr, mask=mask, cmap="RdBu_r", vmin=-1, vmax=1, center=0, annot=k <= 14, fmt=".2f",
                     annot_kws={"fontsize": 6}, linewidths=0.5, linecolor="white", square=True,
                     cbar_kws={"label": "correlation", "shrink": 0.8}, xticklabels=labels, yticklabels=labels, ax=ax)
        ax.tick_params(axis="x", rotation=45, labelsize=7)
        ax.tick_params(axis="y", rotation=0, labelsize=7)
    else:
        im = ax.imshow(corr.values, cmap="RdBu_r", vmin=-1, vmax=1)
        ax.set_xticks(range(k))
        ax.set_yticks(range(k))
        ax.set_xticklabels(labels, rotation=45, ha="right", fontsize=7)
        ax.set_yticklabels(labels, fontsize=7)
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
    order = list(d.groupby("g")["v"].median().sort_values(ascending=False).index[:max_groups])
    d = d[d["g"].isin(order)]
    sizes = d["g"].value_counts()
    fig, ax = plt.subplots(figsize=(7, 4.4))
    log = needs_log(d["v"])
    drawn = False
    if seaborn_on():
        # a violin shows the shape when groups are large enough; a box is clearer for small groups
        kind = _sns.violinplot if sizes.min() >= 30 else _sns.boxplot
        extra = {"inner": "quartile", "cut": 0, "density_norm": "width"} if kind is _sns.violinplot else \
            {"fliersize": 2, "width": 0.6}
        try:
            kind(data=d, x="g", y="v", order=order, color=ACCENT, ax=ax, log_scale=log, **extra)
            drawn = True
        except TypeError:  # seaborn older than 0.13: draw with matplotlib below
            ax.clear()
    if not drawn:
        ax.boxplot([d.loc[d["g"] == g, "v"].values for g in order], widths=0.6,
                   flierprops={"markersize": 2, "alpha": 0.4})
        if log:
            ax.set_yscale("log")
        ax.set_xticks(range(1, len(order) + 1))
    if drawn:
        ax.set_xticks(range(len(order)))
    ax.set_xticklabels([f"{_short(g, 14)}\n(n={sizes[g]:,})" for g in order], fontsize=7)
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


def missingness(before, after, path):
    """Share of missing values per column before and after cleaning (dict column -> pct)."""
    cols = sorted(set(before) | set(after), key=lambda c: -max(before.get(c, 0), after.get(c, 0)))[:25]
    if not cols:
        return None
    y = np.arange(len(cols))
    fig, ax = plt.subplots(figsize=(7, max(2.4, 0.3 * len(cols) + 1.2)))
    ax.barh(y + 0.2, [before.get(c, 0) for c in cols], height=0.4, color=MUTED, label="before cleaning")
    ax.barh(y - 0.2, [after.get(c, 0) for c in cols], height=0.4, color=ACCENT, label="after cleaning")
    ax.set_yticks(y)
    ax.set_yticklabels([_short(c, 26) for c in cols], fontsize=7)
    ax.invert_yaxis()
    ax.legend(frameon=False, fontsize=8, loc="lower right")
    _style(ax, "Missing values by column", "% of rows missing", "")
    ax.grid(axis="x", color=GRID)
    ax.grid(axis="y", visible=False)
    return _save(fig, path)


def missing_pattern(mask, path, max_rows=400, seed=42):
    """Which columns are missing together: rows (sampled, in original order) by columns."""
    m = mask.loc[:, mask.any()]
    if m.shape[1] == 0:
        return None
    n = len(m)
    if n > max_rows:
        m = m.sample(n=max_rows, random_state=seed).sort_index()
    fig, ax = plt.subplots(figsize=(min(9, 2 + 0.45 * m.shape[1]), 4))
    ax.imshow(m.values.T.astype(float), aspect="auto", cmap=_mpl.colors.ListedColormap(["#f1f5f9", ACCENT2]),
              interpolation="nearest")
    ax.set_yticks(range(m.shape[1]))
    ax.set_yticklabels([_short(c, 20) for c in m.columns], fontsize=7)
    ax.set_xlabel(f"rows ({len(m):,} of {n:,}" + (", random sample" if n > max_rows else "") + ", in file order)",
                  color=MUTED)
    ax.set_title("Missing-value pattern (amber = missing)", fontsize=11, color=INK, loc="left")
    ax.tick_params(colors=MUTED, labelsize=7)
    return _save(fig, path)


def anomaly_scatter(scores, flagged, path, title):
    """Two leading principal components of the scaled measures, anomalies highlighted."""
    fig, ax = plt.subplots(figsize=(6.4, 4.4))
    ax.scatter(scores[~flagged, 0], scores[~flagged, 1], s=7, alpha=0.35, color=MUTED, edgecolors="none",
               label=f"other rows (n = {int((~flagged).sum()):,})")
    ax.scatter(scores[flagged, 0], scores[flagged, 1], s=18, alpha=0.9, color=RED, edgecolors="none",
               label=f"flagged (n = {int(flagged.sum()):,})")
    ax.legend(frameon=False, fontsize=8)
    _style(ax, title, "principal component 1", "principal component 2")
    return _save(fig, path)


if plt is not None:
    configure()
