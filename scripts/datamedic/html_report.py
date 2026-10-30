"""The self-contained HTML audit report.

One file, no network: styles, scripts, charts (base64 PNG) and data are inline. Every figure comes from
the run's JSON and CSV outputs. Data shown in the page is escaped when the page is built and inserted with
textContent by the scripts, so values from the dataset are never interpreted as HTML. Sensitive columns
are masked in the preview and the inspectors.
"""

import base64
import html
import json
import os

import pandas as pd

from . import charts
from .clean import STAGE
from .common import ROW_ID, fmt_num, mask_value

SECTIONS = ["Overview", "Dataset profile", "Data quality", "Cleaning operations", "Missing values",
            "Duplicate records", "Outlier analysis", "Type corrections", "Removed and quarantined records",
            "Cleaned data preview", "Exploratory analysis", "Statistical insights", "Data lineage", "Validation",
            "Audit and recovery"]

CSS = """
:root{--bg:#f6f7f9;--fg:#0f172a;--muted:#526071;--line:#dfe3ea;--card:#ffffff;--navy:#1e3a5f;--accent:#0f766e;
--accent-soft:#e6f3f1;--ok:#15803d;--warn:#b45309;--bad:#b91c1c;--chip:#eef1f5;--imp:#fff4d6;--mod:#e6f0ff;--side:#14243b;
--side-fg:#cfd8e3;--side-active:#ffffff}
@media (prefers-color-scheme: dark){:root:not([data-theme="light"]){--bg:#0d1117;--fg:#e6e9ee;--muted:#9aa6b4;
--line:#273140;--card:#141a22;--navy:#9cc3f5;--accent:#2dd4bf;--accent-soft:#11302d;--ok:#4ade80;--warn:#fbbf24;
--bad:#f87171;--chip:#1c2430;--imp:#3a2f10;--mod:#14284a;--side:#0a0f16;--side-fg:#9aa6b4;--side-active:#ffffff}}
:root[data-theme="dark"]{--bg:#0d1117;--fg:#e6e9ee;--muted:#9aa6b4;--line:#273140;--card:#141a22;--navy:#9cc3f5;
--accent:#2dd4bf;--accent-soft:#11302d;--ok:#4ade80;--warn:#fbbf24;--bad:#f87171;--chip:#1c2430;--imp:#3a2f10;
--mod:#14284a;--side:#0a0f16;--side-fg:#9aa6b4;--side-active:#ffffff}
*{box-sizing:border-box}html{scroll-behavior:smooth}
body{margin:0;background:var(--bg);color:var(--fg);font:14.5px/1.55 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif}
.layout{display:grid;grid-template-columns:240px minmax(0,1fr);min-height:100vh}
aside{background:var(--side);color:var(--side-fg);position:sticky;top:0;height:100vh;overflow-y:auto;padding:20px 0}
aside .brand{padding:0 20px 14px;color:var(--side-active);font-weight:650;letter-spacing:.2px}
aside .brand small{display:block;color:var(--side-fg);font-weight:400;font-size:12px;margin-top:2px}
aside a{display:block;padding:6px 20px;color:var(--side-fg);text-decoration:none;font-size:13.5px;border-left:3px solid transparent}
aside a:hover,aside a.active{color:var(--side-active);border-left-color:var(--accent);background:rgba(255,255,255,.04)}
aside .tools{padding:14px 20px}button,.btn{font:inherit;font-size:12.5px;border:1px solid var(--line);background:var(--card);
color:var(--fg);border-radius:6px;padding:4px 10px;cursor:pointer}aside button{background:transparent;color:var(--side-fg);border-color:#33465f}
main{padding:28px 32px 80px;max-width:1240px}
h1{font-size:24px;margin:0 0 4px;color:var(--navy)}h2{font-size:18px;margin:44px 0 10px;padding-top:10px;color:var(--navy);
border-top:1px solid var(--line)}h3{font-size:14.5px;margin:22px 0 6px}
.muted{color:var(--muted)}.small{font-size:12.5px}
.grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(160px,1fr));gap:10px;margin:14px 0}
.stat{background:var(--card);border:1px solid var(--line);border-radius:8px;padding:12px 14px}
.stat b{display:block;font-size:21px;font-variant-numeric:tabular-nums}.stat span{color:var(--muted);font-size:12.5px}
.table-wrap{overflow-x:auto;border:1px solid var(--line);border-radius:8px;background:var(--card);margin:8px 0 14px}
table{border-collapse:collapse;width:100%;font-size:12.8px}th,td{text-align:left;padding:6px 9px;border-bottom:1px solid var(--line);
vertical-align:top}th{background:var(--chip);font-weight:600;white-space:nowrap;position:sticky;top:0}
tr:last-child td{border-bottom:none}td.num{text-align:right;font-variant-numeric:tabular-nums}
.pill{display:inline-block;padding:1px 8px;border-radius:99px;font-size:11.5px;font-weight:600;background:var(--chip);white-space:nowrap}
.ok{color:var(--ok)}.warn{color:var(--warn)}.bad{color:var(--bad)}
.pill.ok{background:color-mix(in srgb,var(--ok) 14%,transparent)}.pill.warn{background:color-mix(in srgb,var(--warn) 16%,transparent)}
.pill.bad{background:color-mix(in srgb,var(--bad) 14%,transparent)}
img{max-width:100%;border:1px solid var(--line);border-radius:8px;background:#fff}
.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(360px,1fr));gap:12px}
.charts figure{margin:0}.charts figcaption{font-size:12px;color:var(--muted);margin-top:4px}
code{background:var(--chip);padding:1px 5px;border-radius:4px;font-size:12px}pre{background:var(--chip);padding:10px;
border-radius:6px;overflow-x:auto;font-size:12px;max-height:340px}
details{border:1px solid var(--line);border-radius:6px;background:var(--card);margin:6px 0;padding:6px 10px}
summary{cursor:pointer;font-weight:550}
.controls{display:flex;flex-wrap:wrap;gap:8px;align-items:center;margin:8px 0}
input[type=search],input[type=text],select{font:inherit;font-size:13px;padding:5px 8px;border:1px solid var(--line);
border-radius:6px;background:var(--card);color:var(--fg)}
td.imp{background:var(--imp)}td.mod{background:var(--mod)}.legend span{display:inline-block;padding:0 8px;border-radius:4px;margin-right:6px}
.callout{border-left:3px solid var(--accent);background:var(--accent-soft);padding:8px 12px;border-radius:0 6px 6px 0;margin:10px 0}
.callout.warn{border-left-color:var(--warn);background:color-mix(in srgb,var(--warn) 10%,transparent)}
svg text{font-family:inherit}
.menu{display:none}
@media (max-width:860px){.layout{grid-template-columns:minmax(0,1fr)}aside{position:static;height:auto;max-height:none}
aside nav{display:none}aside.open nav{display:block}.menu{display:inline-block}main{padding:20px 16px 60px}
.charts{grid-template-columns:1fr}}
@media print{aside{display:none}.layout{display:block}main{max-width:none;padding:0}details{break-inside:avoid}
details:not([open]) > *:not(summary){display:block}body{background:#fff;color:#000}.controls{display:none}
h2{break-before:auto}.table-wrap{overflow:visible}}
"""

JS = r"""
(function(){
  const root=document.documentElement;
  try{const t=localStorage.getItem('dm-theme');if(t)root.setAttribute('data-theme',t);}catch(e){}
  const tb=document.getElementById('theme');
  if(tb)tb.addEventListener('click',()=>{const cur=root.getAttribute('data-theme')||
    (matchMedia('(prefers-color-scheme: dark)').matches?'dark':'light');const nx=cur==='dark'?'light':'dark';
    root.setAttribute('data-theme',nx);try{localStorage.setItem('dm-theme',nx);}catch(e){}});
  const mb=document.getElementById('menu');if(mb)mb.addEventListener('click',()=>document.querySelector('aside').classList.toggle('open'));
  const links=[...document.querySelectorAll('aside nav a')];
  const obs=new IntersectionObserver(es=>{es.forEach(e=>{if(e.isIntersecting){links.forEach(a=>a.classList.toggle('active',
    a.getAttribute('href')==='#'+e.target.id));}});},{rootMargin:'0px 0px -75% 0px'});
  document.querySelectorAll('main h2[id]').forEach(h=>obs.observe(h));
  function data(id){const el=document.getElementById(id);return el?JSON.parse(el.textContent):null;}
  // filterable tables: <input data-filter="tableId">
  document.querySelectorAll('input[data-filter]').forEach(inp=>{const tb=document.getElementById(inp.dataset.filter);
    inp.addEventListener('input',()=>{const q=inp.value.toLowerCase();tb.querySelectorAll('tbody tr').forEach(tr=>{
      tr.style.display=tr.textContent.toLowerCase().includes(q)?'':'none';});});});
  document.querySelectorAll('select[data-filter-col]').forEach(sel=>{const tb=document.getElementById(sel.dataset.table);
    const ci=+sel.dataset.filterCol;sel.addEventListener('change',()=>{tb.querySelectorAll('tbody tr').forEach(tr=>{
      const v=tr.children[ci]?tr.children[ci].textContent:'';tr.style.display=!sel.value||v===sel.value?'':'none';});});});
  // cleaned data preview
  const P=data('preview-data');
  if(P){
    const host=document.getElementById('preview'),q=document.getElementById('pv-q'),ps=document.getElementById('pv-size'),
      info=document.getElementById('pv-info'),cols=document.getElementById('pv-cols');
    let rows=P.rows.map((r,i)=>({r,i})),sortC=-1,asc=true,page=0,shown=P.columns.map(()=>true);
    P.columns.forEach((c,j)=>{const l=document.createElement('label');l.className='small';const cb=document.createElement('input');
      cb.type='checkbox';cb.checked=true;cb.addEventListener('change',()=>{shown[j]=cb.checked;draw();});l.append(cb,' '+c+' ');cols.append(l);});
    function filt(){const s=q.value.toLowerCase();return rows.filter(o=>!s||o.r.some(v=>String(v).toLowerCase().includes(s)));}
    function draw(){let f=filt();if(sortC>=0){f=f.slice().sort((a,b)=>{const x=a.r[sortC],y=b.r[sortC];const nx=parseFloat(x),ny=parseFloat(y);
        const c=(!isNaN(nx)&&!isNaN(ny))?nx-ny:String(x).localeCompare(String(y));return asc?c:-c;});}
      const size=+ps.value,pages=Math.max(1,Math.ceil(f.length/size));page=Math.min(page,pages-1);
      const t=document.createElement('table'),th=document.createElement('thead'),hr=document.createElement('tr');
      P.columns.forEach((c,j)=>{if(!shown[j])return;const h=document.createElement('th');h.textContent=c+(sortC===j?(asc?' ▲':' ▼'):'');
        h.style.cursor='pointer';h.addEventListener('click',()=>{asc=sortC===j?!asc:true;sortC=j;draw();});hr.append(h);});
      th.append(hr);t.append(th);const tb=document.createElement('tbody');
      f.slice(page*size,(page+1)*size).forEach(o=>{const tr=document.createElement('tr');o.r.forEach((v,j)=>{if(!shown[j])return;
        const td=document.createElement('td');td.textContent=v;const m=P.marks[o.i]&&P.marks[o.i][j];
        if(m){td.className=m[0]==='i'?'imp':'mod';td.title=(m[0]==='i'?'imputed; ':'changed; ')+'original: '+(m.slice(2)||'(empty)');}
        tr.append(td);});tb.append(tr);});
      t.append(tb);host.replaceChildren(t);
      info.textContent=f.length.toLocaleString()+' matching rows, page '+(page+1)+' of '+pages;}
    q.addEventListener('input',()=>{page=0;draw();});ps.addEventListener('change',()=>{page=0;draw();});
    document.getElementById('pv-prev').addEventListener('click',()=>{page=Math.max(0,page-1);draw();});
    document.getElementById('pv-next').addEventListener('click',()=>{page=page+1;draw();});
    draw();
  }
  // record inspector
  const R=data('record-data');
  if(R){const inp=document.getElementById('rec-id'),out=document.getElementById('rec-out');
    function show(){const id=inp.value.trim();out.replaceChildren();if(!id)return;
      const st=R.status[id];const h=document.createElement('p');
      if(!st){h.textContent=R.status_complete?'No row with id '+id+'.':'Row '+id+' is not in the embedded sample; see audit/row_status.csv.';out.append(h);return;}
      h.textContent='Status: '+st[0]+(st[1]?'; flags: '+st[1]:'')+(st[2]?'; reason: '+st[2]:'');out.append(h);
      const ch=R.changes[id]||[];if(!ch.length){const p=document.createElement('p');p.className='muted';p.textContent='No values changed in this row.';out.append(p);return;}
      const t=document.createElement('table');const hr=document.createElement('tr');['Step','Column','Before','After'].forEach(x=>{const th=document.createElement('th');th.textContent=x;hr.append(th);});
      const hd=document.createElement('thead');hd.append(hr);t.append(hd);const tb=document.createElement('tbody');
      ch.forEach(c=>{const tr=document.createElement('tr');c.forEach(v=>{const td=document.createElement('td');td.textContent=v===''?'(empty)':v;tr.append(td);});tb.append(tr);});
      t.append(tb);const w=document.createElement('div');w.className='table-wrap';w.append(t);out.append(w);}
    inp.addEventListener('input',show);}
})();
"""


class Raw(str):
    """Trusted HTML built by this module (never data from the dataset)."""


def _h(x):
    return html.escape("" if x is None else str(x))


def _json_script(id_, obj):
    s = json.dumps(obj, ensure_ascii=False, default=str).replace("</", "<\\/").replace("<!--", "<\\!--")
    return f"<script type='application/json' id='{id_}'>{s}</script>"


def _table(headers, rows, limit=None, tid=None, num_cols=()):
    rows = list(rows)
    more = ""
    if limit and len(rows) > limit:
        more = f"<p class='muted small'>Showing {limit:,} of {len(rows):,}. The full list is in the file named above.</p>"
        rows = rows[:limit]
    if not rows:
        return "<p class='muted'>None.</p>"
    head = "".join(f"<th>{_h(h)}</th>" for h in headers)
    body = "".join("<tr>" + "".join(
        f"<td{' class=num' if j in num_cols else ''}>{c if isinstance(c, Raw) else _h(c)}</td>" for j, c in enumerate(r))
        + "</tr>" for r in rows)
    idattr = f" id='{tid}'" if tid else ""
    return f"<div class='table-wrap'><table{idattr}><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table></div>{more}"


def _img(path, caption=""):
    if not path or not os.path.exists(path):
        return ""
    with open(path, "rb") as fh:
        b64 = base64.b64encode(fh.read()).decode()
    cap = f"<figcaption>{_h(caption)}</figcaption>" if caption else ""
    return f"<figure><img alt='{_h(caption)}' src='data:image/png;base64,{b64}'>{cap}</figure>"


def _pill(text, kind):
    return Raw(f"<span class='pill {kind}'>{_h(text)}</span>")


def _status_kind(status):
    return "ok" if status == "PASSED" else "warn" if "WARN" in str(status) else "bad"


def _sev(sev):
    return _pill(sev, {"critical": "bad", "high": "bad", "medium": "warn", "low": "", "info": ""}.get(sev, ""))


def _pre(obj):
    return Raw(f"<pre>{_h(json.dumps(obj, indent=2, ensure_ascii=False, default=str))}</pre>")


def _op_columns(e, renames):
    p = e.get("params", {}) or {}
    cols = []
    if p.get("column"):
        cols.append(p["column"])
    cols += list(p.get("columns") or [])
    if isinstance(p.get("set"), dict):
        cols += list(p["set"])
    if e.get("op") == "rename_columns":
        cols += list((p.get("mapping") or {}).values())
    return cols


# ---------------------------------------------------------------- lineage

def lineage_svg(stages, start, end):
    """Vertical flow: original -> each stage that ran (in order) -> final. Counts come from the log."""
    nodes = [("Original dataset", f"{start['rows']:,} rows · {start['columns']} columns", "start")]
    for name, info in stages:
        bits = []
        if info["changed"]:
            bits.append(f"{info['changed']:,} values changed")
        if info["removed"]:
            bits.append(f"{info['removed']:,} rows removed")
        if info["quarantined"]:
            bits.append(f"{info['quarantined']:,} rows quarantined")
        if info["flagged"]:
            bits.append(f"{info['flagged']:,} flags")
        if info["columns_removed"]:
            bits.append(f"{info['columns_removed']} columns removed")
        if info["columns_added"]:
            bits.append(f"{info['columns_added']} columns added")
        nodes.append((f"{name.capitalize()} ({', '.join(info['ops'])})", "; ".join(bits) or "no values changed", "step"))
    nodes.append(("Final dataset", f"{end['rows']:,} rows · {end['columns']} columns · validation {end['status']}", "end"))
    w, bh, gap = 640, 50, 22
    h = len(nodes) * (bh + gap)
    parts = [f"<svg viewBox='0 0 {w} {h}' width='100%' style='max-width:{w}px' role='img' "
             f"aria-label='Transformation lineage from the original to the final dataset'>",
             "<defs><marker id='arr' viewBox='0 0 10 10' refX='5' refY='5' markerWidth='6' markerHeight='6' "
             "orient='auto-start-reverse'><path d='M0 0L10 5L0 10z' fill='var(--muted)'/></marker></defs>"]
    for i, (title, sub, kind) in enumerate(nodes):
        y = i * (bh + gap)
        fill = "var(--accent-soft)" if kind != "step" else "var(--card)"
        stroke = "var(--accent)" if kind != "step" else "var(--line)"
        parts.append(f"<rect x='1' y='{y + 1}' width='{w - 2}' height='{bh}' rx='8' fill='{fill}' stroke='{stroke}'/>")
        parts.append(f"<text x='14' y='{y + 21}' font-size='13' font-weight='600' fill='var(--fg)'>{_h(title[:95])}</text>")
        parts.append(f"<text x='14' y='{y + 39}' font-size='12' fill='var(--muted)'>{_h(sub[:110])}</text>")
        if i < len(nodes) - 1:
            parts.append(f"<line x1='{w / 2}' y1='{y + bh + 2}' x2='{w / 2}' y2='{y + bh + gap - 3}' stroke='var(--muted)' "
                         "stroke-width='1.5' marker-end='url(#arr)'/>")
    parts.append("</svg>")
    return "".join(parts)


def stage_summary(log, removed):
    out = []
    by_op_removed = removed.groupby(["op_id", "dm_disposition"]).size().to_dict() if len(removed) and \
        "dm_disposition" in removed.columns else {}
    for e in log:
        if not e.get("op") or not str(e.get("status", "")).startswith(("applied", "flagged")):
            continue
        name = STAGE.get(e["op"], "other")
        if not out or out[-1][0] != name:
            out.append((name, {"ops": [], "changed": 0, "removed": 0, "quarantined": 0, "flagged": 0,
                               "columns_removed": 0, "columns_added": 0}))
        info = out[-1][1]
        info["ops"].append(e["op_id"])
        if e["op"] in ("drop_rows", "drop_duplicates", "quarantine_rows"):
            info["removed"] += int(by_op_removed.get((e["op_id"], "removed"), 0))
            info["quarantined"] += int(by_op_removed.get((e["op_id"], "quarantined"), 0))
        elif e["op"] == "drop_columns":
            info["columns_removed"] += int(e.get("affected", 0))
        elif e["op"] == "flag_rows":
            info["flagged"] += int(e.get("flagged", 0) or 0)
        else:
            info["changed"] += int(e.get("affected", 0) or 0)
        if e["op"] == "impute" and (e.get("statistical_basis") or {}).get("indicator_column"):
            info["columns_added"] += 1
        info["columns_added"] += len(e.get("added_columns") or [])
    return out


# ---------------------------------------------------------------- page

def build(d, out, interactive=False):
    man, p0, p1, issues, log, val = d["man"], d["p0"], d["p1"], d["issues"], d["log"], d["val"]
    cfg = d.get("cfg", {})
    rcfg = cfg.get("report", {})
    sens = d["sensitive"] if rcfg.get("redact_sensitive", True) else set()
    r = man.get("result", {})
    inp = man["input"]
    status = val["status"] if val else "NOT VALIDATED"
    renames = d["schema"].get("renamed_columns", {})
    ch, removed, flags, cleaned = d["changes"], d["removed"], d["flags"], d["cleaned"]
    if "dm_disposition" not in removed.columns:
        removed = removed.assign(dm_disposition="removed")
    applied = [e for e in log if e.get("op") and str(e.get("status", "")).startswith("applied")]
    disabled = [e for e in log if "disabled" in str(e.get("status", "")) and e.get("needs_review")]
    failed = [e for e in log if e.get("status") == "failed"]
    imputed = sum(int(e.get("affected", 0)) for e in applied if e["op"] in ("impute", "apply_rule"))
    dups = int((removed["op_id"].isin([e["op_id"] for e in applied if e["op"] == "drop_duplicates"])).sum()) \
        if len(removed) else 0
    n_removed = int((removed["dm_disposition"] == "removed").sum()) if len(removed) else 0
    n_quar = int((removed["dm_disposition"] == "quarantined").sum()) if len(removed) else 0
    missing0 = sum(c["missing_count"] for c in p0["columns_profile"])
    checks = val["checks"] if val else []
    passed = sum(c["passed"] for c in checks)
    outstanding = len(disabled) + sum(1 for c in checks if not c["passed"]) + len(failed)
    S = []
    S.append(f"<h1>Data audit report</h1><p class='muted'>{_h(inp['file'])} · dataMedic {_h(man.get('version'))} · "
             f"validation {_pill(status, _status_kind(status))}</p>")
    scope = man.get("analysis_scope", {})
    if scope.get("sampled"):
        S.append(f"<div class='callout warn'>This run analysed a random sample of {scope['rows_analysed']:,} of "
                 f"{scope['rows_in_file']:,} rows. Every figure describes the sample, not the whole file.</div>")

    # 1 overview
    S.append(_sec(1))
    tiles = [("rows before → after", f"{p0['rows']:,} → {r.get('rows_after', 'n/a'):,}" if isinstance(r.get('rows_after'), int)
              else f"{p0['rows']:,} → n/a"),
             ("columns before → after", f"{p0['columns']} → {r.get('columns_after', 'n/a')}"),
             ("missing values found", f"{missing0:,}"), ("values imputed or rule-filled", f"{imputed:,}"),
             ("duplicate rows removed", f"{dups:,}"), ("rows removed (all reasons)", f"{n_removed:,}"),
             ("rows quarantined", f"{n_quar:,}"), ("values changed", f"{r.get('cells_changed', 0):,}"),
             ("steps applied", f"{len(applied)}"), ("validation checks passed", f"{passed} of {len(checks)}"),
             ("outstanding items", f"{outstanding}")]
    S.append("<div class='grid'>" + "".join(f"<div class='stat'><b>{_h(v)}</b><span>{_h(k)}</span></div>"
                                            for k, v in tiles) + "</div>")
    qd = (val or {}).get("quality_dimensions")
    if qd:
        S.append("<h3>Quality by dimension</h3><p class='muted small'>Each dimension is measured separately; there is "
                 "no overall score, because one number can hide a serious problem in a single dimension.</p>")
        c_, u_, v_, k_, i_ = qd["completeness"], qd["uniqueness"], qd["validity"], qd["consistency"], qd["integrity"]
        rows = [["Completeness", f"{c_['missing_cells_before']:,} of {c_['cells_before']:,} cells missing",
                 f"{c_['missing_cells_after']:,} of {c_['cells_after']:,} cells missing", c_.get("note", "")],
                ["Uniqueness", f"{u_['exact_duplicate_rows_before']:,} exact duplicate rows",
                 f"{u_['exact_duplicate_rows_after']:,}", ""],
                ["Validity", (f"{v_['contract_violations_before']:,} contract violations" if v_["contract_rules"] else "n/a"),
                 f"{v_['passed']} of {v_['checks']} validity checks passed" + (
                     f"; {v_['contract_violations_after']:,} contract violations" if v_["contract_rules"] else ""), ""],
                ["Consistency", "", f"{k_['rows_flagged_logically_inconsistent']:,} rows flagged as logically inconsistent",
                 k_["note"]],
                ["Integrity", "", f"{i_['passed']} of {i_['checks']} checks passed", "original unchanged, revert, replay"],
                ["Accuracy", "", _pill(qd["accuracy"]["status"], "warn"), qd["accuracy"]["note"]]]
        S.append(_table(["Dimension", "Before cleaning", "After cleaning", "Note"], rows))

    # 2 profile
    S.append(_sec(2))
    S.append(_table(["Property", "Value"], [
        ["File", inp["file"]], ["Format", inp["format"]], ["Encoding", inp.get("encoding") or "n/a"],
        ["Delimiter", repr(inp.get("delimiter")) if inp.get("delimiter") else "n/a"],
        ["Size (bytes)", f"{inp['size_bytes']:,}"], ["SHA-256", Raw(f"<code>{_h(inp['sha256'])}</code>")],
        ["Rows", f"{p0['rows']:,}"], ["Columns", p0["columns"]]]))
    S.append("<div class='controls'><input type='search' placeholder='Filter columns' data-filter='t-profile'></div>")
    S.append(_table(["Column", "Inferred type", "Roles", "Non-null", "Missing", "Missing %", "Unique", "Min", "Max",
                     "Mean", "Median"],
                    [[c["column"], c["inferred_type"], ", ".join(c["roles"]), f"{c['non_null_count']:,}",
                      f"{c['missing_count']:,}", c["missing_pct"], f"{c['unique_count']:,}",
                      "" if c["column"] in sens or "sensitive" in c["roles"] else _fmt(c.get("min")),
                      "" if c["column"] in sens or "sensitive" in c["roles"] else _fmt(c.get("max")),
                      _fmt(c.get("mean")), _fmt(c.get("median"))] for c in p0["columns_profile"]],
                    tid="t-profile", num_cols=(3, 4, 5, 6, 9, 10)))

    # 3 quality
    S.append(_sec(3))
    sevs = sorted({i["severity"] for i in issues}, key=["critical", "high", "medium", "low", "info"].index)
    S.append("<div class='controls'><input type='search' placeholder='Filter issues' data-filter='t-issues'>"
             "<select data-table='t-issues' data-filter-col='1'><option value=''>All severities</option>"
             + "".join(f"<option>{_h(s)}</option>" for s in sevs) + "</select></div>")
    S.append(_table(["ID", "Severity", "Kind", "Column", "Count", "Detail", "Action"],
                    [[i["issue_id"], _sev(i["severity"]), i["kind"], i["column"] or "(table)",
                      f"{i['count']:,}" if isinstance(i.get("count"), int) else "", i["detail"], i["planned_action"]]
                     for i in issues], tid="t-issues", num_cols=(4,)))

    # 4 operations
    S.append(_sec(4))
    S.append("<p>In the order they ran. Risk: <b>safe</b> steps cannot lose information; <b>moderate</b> steps change "
             "values in a documented, reversible way; <b>high</b> steps remove rows or columns or reshape a distribution. "
             "Open a step for its parameters and evidence.</p>")
    for e in log:
        if not e.get("op"):
            continue
        st = str(e.get("status", ""))
        kind = "bad" if st == "failed" else "warn" if "not applied" in st else "ok"
        head = (f"<summary>{_h(e['op_id'])} · {_h(e['op'])} · {_h(e.get('column') or '(table)')} · "
                f"{_pill(st, kind)} {_pill('risk ' + str(e.get('risk', '')), '')}</summary>")
        body = [f"<p>{_h(e.get('issue', ''))}. {_h(e.get('rationale', ''))}</p>",
                f"<p class='small'>Changed {e.get('affected', 0):,} values" +
                (f", flagged {e['flagged']:,} rows" if e.get("flagged") else "") +
                f". Undo: {_h(e.get('reversal', ''))}" + (f". Error: {_h(e.get('error'))}" if e.get("error") else "") + "</p>"]
        body.append("<p class='small'><b>Parameters</b></p>" + _pre(e.get("params", {})))
        if e.get("statistical_basis"):
            body.append("<p class='small'><b>Statistical basis</b></p>" + _pre(e["statistical_basis"]))
        if e.get("evidence"):
            body.append("<p class='small'><b>Evidence at planning time</b></p>" + _pre(e["evidence"]))
        S.append(f"<details>{head}{''.join(body)}</details>")

    # 5 missing values
    S.append(_sec(5))
    miss = d.get("missingness") or {}
    pct0 = {c["column"]: c["missing_pct"] for c in p0["columns_profile"] if c["missing_count"]}
    pct1 = {c["column"]: c["missing_pct"] for c in (p1 or {}).get("columns_profile", []) if c["missing_count"]
            and not c["column"].endswith("_was_missing")}
    imgs = []
    if charts.available() and (pct0 or pct1):
        pth = os.path.join(out, "visualisations", "missingness", "missing_before_after.png")
        try:
            charts.missingness(pct0, pct1, pth)
            imgs.append(_img(pth, "Share of rows missing each column, before and after cleaning (from the profiles)"))
        except Exception:
            pass
    if miss.get("chart"):
        imgs.append(_img(os.path.join(out, miss["chart"]), "Missing-value pattern after placeholders were normalised, "
                                                          "before imputation"))
    if imgs:
        S.append("<div class='charts'>" + "".join(imgs) + "</div>")
    S.append(f"<div class='callout'>{_h(miss.get('caveat', ''))}</div>" if miss.get("caveat") else "")
    lm = miss.get("littles_mcar") or {}
    if lm.get("p_value") is not None:
        S.append(f"<p>Little's MCAR test on {_h(', '.join(lm['columns']))}: chi-square {lm['statistic']:.3g} on "
                 f"{lm['df']} degrees of freedom, p = {lm['p_value']:.3g}. Reading: {_h(lm['reading'])}. "
                 f"Assumes {_h(lm['assumptions'])}.</p>")
    if miss.get("missing_together"):
        S.append("<h3>Columns missing together</h3>" + _table(["Column", "Column", "Both missing", "Jaccard"], [
            [m["a"], m["b"], f"{m['both_missing']:,}", m["jaccard"]] for m in miss["missing_together"]], num_cols=(2, 3)))
    if miss.get("structural_candidates"):
        S.append("<h3>Possibly structural</h3>" + _table(["When missing", "Always missing", "Rows", "Note"], [
            [m["when_missing"], m["always_missing"], f"{m['rows']:,}", m["note"]] for m in miss["structural_candidates"]]))
    ot = (miss.get("over_time") or {}).get("columns") or {}
    if ot:
        S.append(f"<h3>Missingness over time (per {_h(miss['over_time']['period'])})</h3>" + _table(
            ["Column", "Periods", "Highest rate", "In period", "Lowest rate", "p", "Cramer's V"],
            [[c, v["periods"], f"{v['highest_rate_pct']}%", v["highest_rate_period"], f"{v['lowest_rate_pct']}%",
              _fmt(v.get("p_value")), _fmt(v.get("cramers_v"))] for c, v in ot.items()]))
    comp = (d.get("comparison") or {}).get("columns") or {}
    if comp:
        S.append("<h3>Imputation methods compared on hidden known values</h3><p class='muted small'>Known values were "
                 "hidden at random and filled by each method using only the rest of the data. Normalised MAE is the "
                 "mean absolute error divided by the column's standard deviation (lower is better; filling with the mean "
                 "is the reference). Random masking imitates values missing completely at random, so a good score here "
                 "does not prove a method is unbiased on the real gaps.</p>")
        rows = []
        for col, cr in comp.items():
            if cr.get("status") != "ok":
                rows.append([col, cr.get("status", ""), "", "", "", "", ""])
                continue
            for name, m in cr["methods"].items():
                if m.get("status") != "ok":
                    rows.append([col, name, "could not run", "", "", "", m.get("error", "")])
                    continue
                metric = f"{m['nmae']:.3f}" if m.get("nmae") is not None else (
                    f"{m['accuracy']:.3f} acc." if m.get("accuracy") is not None else "")
                rows.append([col, Raw(f"<b>{_h(name)}</b>") if name == cr.get("best") else name, metric,
                             f"{m['coverage'] * 100:.0f}%", _fmt(m.get("bias")), _fmt(m.get("spread_ratio")),
                             m.get("note", "")])
        S.append(_table(["Column", "Method", "Normalised MAE / accuracy", "Filled", "Bias", "Spread ratio", "Note"], rows))
    ev = d.get("evidence")
    if ev is not None and len(ev):
        summ = ev.groupby(["column", "method", "evidence_level"]).size().reset_index(name="cells")
        S.append("<h3>Evidence behind each imputed value</h3><p class='muted small'>Levels summarise the supporting data "
                 "and the masked-value check; they are not probabilities. Every cell's record is in "
                 "<code>audit/imputation_evidence.csv</code>.</p>")
        S.append(_table(["Column", "Method", "Evidence level", "Cells"], summ.values.tolist(), num_cols=(3,)))

    # 6 duplicates
    S.append(_sec(6))
    dd = [e for e in applied if e["op"] == "drop_duplicates"]
    for e in dd:
        det = e.get("details", {})
        S.append(f"<p>{_h(e['op_id'])}: removed {e.get('affected', 0):,} rows ({_h(e.get('method'))}); compared columns: "
                 f"{_h(det.get('columns_compared'))}. Removed rows that differ from the row kept: "
                 f"{det.get('removed_rows_that_differ_from_the_kept_row', 0):,}.</p>")
    if not dd:
        S.append("<p class='muted'>No duplicate rows were removed.</p>")
    dup_flags = flags[flags["classification"].isin(["duplicate identifier", "possible duplicate"])] if len(flags) else flags
    if len(dup_flags):
        S.append("<h3>Flagged, not removed</h3>" + _table(["Classification", "Column", "Rows"], dup_flags.groupby(
            ["classification", "column"]).size().reset_index().values.tolist(), num_cols=(2,)))

    # 7 outliers
    S.append(_sec(7))
    outl = flags[flags["classification"].isin(["unresolved outlier", "potential anomaly"])] if len(flags) else flags
    S.append("<p>Outliers are flagged and kept unless a step above changed them with a stated reason. An unusual value "
             "is not the same as a wrong one.</p>")
    if len(outl):
        S.append(_table(["Classification", "Column", "Reason", "Rows"], outl.groupby(
            ["classification", "column", "reason"]).size().reset_index().values.tolist(), num_cols=(3,)))
    an = ((d.get("eda") or {}).get("multivariate") or {}).get("anomalies") or {}
    if an.get("flagged") is not None:
        S.append(f"<h3>Unusual across several measures</h3><p>{an['flagged']:,} rows ({an['share_pct']}%) on "
                 f"{_h(', '.join(an['features']))}. Rule: {_h(an.get('rule', ''))}. {_h(an.get('note', ''))}</p>")
        if an.get("chart"):
            S.append("<div class='charts'>" + _img(os.path.join(out, an["chart"]), "Flagged rows on the two main "
                                                                                "components of the scaled measures") + "</div>")
        if an.get("rows"):
            S.append(_table(list(an["rows"][0].keys()), [[_fmt(v) for v in x.values()] for x in an["rows"]]))
    elif an.get("status"):
        S.append(f"<p class='muted'>Multivariate check {_h(an['status'])}.</p>")

    # 8 types
    S.append(_sec(8))
    conv = [e for e in applied if e["op"] == "convert_type"]
    S.append(_table(["Step", "Column", "Converted to", "Values rewritten", "Format only", "Failed (set missing, flagged)"],
                    [[e["op_id"], e["column"], e["params"]["to"], f"{e.get('affected', 0):,}",
                      f"{e.get('details', {}).get('format_only_changes', 0):,}",
                      f"{e.get('details', {}).get('failed_conversions', 0):,}"] for e in conv], num_cols=(3, 4, 5))
             if conv else "<p class='muted'>No type conversions.</p>")
    cf = flags[flags["classification"] == "conversion failure"] if len(flags) else flags
    if len(cf):
        S.append("<h3>Values that could not be converted</h3><p class='muted small'>Set to missing and flagged; the original "
                 "text is in the change log, so nothing is lost.</p>" + _table(
                     ["Row", "Column", "Original value"], [[x[ROW_ID], x["column"], x["value"]] for _, x in cf.iterrows()],
                     limit=100))

    # 9 removed / quarantined
    S.append(_sec(9))
    for disp, title, fname in (("removed", "Removed", "removed_records.csv"),
                               ("quarantined", "Quarantined", "quarantined_records.csv")):
        sub = removed[removed["dm_disposition"] == disp] if len(removed) else removed
        S.append(f"<h3>{title} ({len(sub):,})</h3><p class='muted small'>Full original rows: <code>audit/{fname}</code>.</p>")
        if len(sub):
            sub = _mask(sub, sens)
            cols = [c for c in sub.columns if c != "dm_disposition"][:14]
            S.append(_table(cols, sub[cols].values.tolist(), limit=50))

    # 10 preview
    S.append(_sec(10))
    limit = int(rcfg.get("preview_rows", 500))
    pv = cleaned.head(limit)
    pvm = _mask(pv, sens)
    kinds = {e["op_id"]: e.get("op") for e in log}
    marks = {}
    if len(ch):
        cset = ch[ch[ROW_ID].astype(int).isin(set(pv[ROW_ID].astype(int)))]
        pos = {int(rid): i for i, rid in enumerate(pv[ROW_ID].astype(int))}
        colpos = {c: j for j, c in enumerate(pv.columns)}
        first_old = cset.drop_duplicates([ROW_ID, "column"], keep="first").set_index([ROW_ID, "column"])["old_value"]
        last_kind = cset.drop_duplicates([ROW_ID, "column"], keep="last").set_index([ROW_ID, "column"])["op_id"].map(kinds)
        for (rid, col), k in last_kind.items():
            if col in colpos:
                old = first_old[(rid, col)]
                old = mask_value(old) if col in sens and old else old
                marks.setdefault(pos[int(rid)], {})[colpos[col]] = ("i:" if k in ("impute", "apply_rule") else "m:") + old
    marks_list = [marks.get(i, {}) for i in range(len(pvm))]
    marks_list = [{int(k): v for k, v in m.items()} for m in marks_list]
    marks_arr = [[m.get(j) for j in range(len(pvm.columns))] if m else None for m in marks_list]
    S.append(f"<p>{'The first ' + format(len(pv), ',') + ' of ' + format(len(cleaned), ',') + ' rows (a preview, not the whole dataset).' if len(cleaned) > len(pv) else 'All ' + format(len(cleaned), ',') + ' rows.'}"
             f" Sensitive columns are masked. Hover a highlighted cell for its original value.</p>"
             "<p class='legend small'><span style='background:var(--imp)'>imputed or filled by a rule</span>"
             "<span style='background:var(--mod)'>changed (format, correction, standardisation)</span></p>")
    S.append("<div class='controls'><input id='pv-q' type='search' placeholder='Search rows'>"
             "<select id='pv-size'><option>25</option><option selected>50</option><option>100</option></select>"
             "<button id='pv-prev' type='button'>Previous</button><button id='pv-next' type='button'>Next</button>"
             "<span id='pv-info' class='muted small'></span></div><details><summary>Columns</summary>"
             "<div id='pv-cols' class='controls'></div></details><div id='preview' class='table-wrap'></div>")
    S.append(_json_script("preview-data", {"columns": list(pvm.columns), "rows": pvm.values.tolist(),
                                           "marks": marks_arr}))
    rs = d.get("row_status")
    S.append("<h3>Inspect a record</h3><div class='controls'><label for='rec-id'>Row id (<code>"
             f"{ROW_ID}</code>)</label><input id='rec-id' type='text' inputmode='numeric' placeholder='e.g. 12'></div>"
             "<div id='rec-out'></div>")
    if rs is not None:
        cap = 20000
        rs_small = rs.head(cap)
        status_map = {str(x[ROW_ID]): [x["record_status"], x["quality_flags"],
                                       x.get("exclusion_reason") or x.get("quarantine_reason") or ""]
                      for _, x in rs_small.iterrows()}
        chs = ch[ch[ROW_ID].astype(int).isin(set(rs_small[ROW_ID].astype(int)))].head(50000) if len(ch) else ch
        changes_map = {}
        for x in chs.itertuples(index=False):
            col = x.column
            o, nw = x.old_value, x.new_value
            if col in sens:
                o, nw = (mask_value(o) if o else o), (mask_value(nw) if nw else nw)
            changes_map.setdefault(str(getattr(x, ROW_ID)), []).append([x.op_id, col, o, nw])
        S.append(_json_script("record-data", {"status": status_map, "changes": changes_map,
                                              "status_complete": len(rs) <= cap}))

    # 11 EDA
    S.append(_sec(11))
    e = d.get("eda")
    if e:
        S.append(f"<p>{e['rows']:,} rows analysed. Measures (ranked by how informative they are): "
                 f"{_h(', '.join(e['measures']) or 'none')}. Grouping columns: {_h(', '.join(e['categories']) or 'none')}. "
                 f"Time column: {_h(e['time_column'] or 'none')}. Charts drawn with {_h(charts.library())}.</p>")
        for x in e.get("analytical_exclusions") or []:
            S.append(f"<div class='callout'>{x['rows']:,} rows excluded from the analysis of "
                     f"{_h(', '.join(x['columns']))} ({_h(x['reason'])}, {_h(x['op_id'])}). They remain in the cleaned data.</div>")
        figs = []
        for c in e.get("charts", []):
            figs.append(_img(os.path.join(out, c["path"]), f"{c['kind']}: {os.path.basename(c['path'])}"))
        if figs:
            S.append("<div class='charts'>" + "".join(figs[:24]) + "</div>")
        if interactive:
            S.append(_interactive(d, e))
    else:
        S.append("<p class='muted'>The exploratory analysis has not been run (<code>dm eda</code>).</p>")

    # 12 insights
    S.append(_sec(12))
    if e:
        t = e.get("tests", {})
        sig = [x for x in t.get("results", []) if x.get("significant_after_correction")]
        S.append(f"<p>{t.get('count', 0)} tests ran; {len(sig)} are significant at {t.get('alpha', 0.05)} after "
                 f"{_h(t.get('correction', 'correction'))}. Significance is not importance: read the effect sizes.</p>")
        S.append(_table(["ID", "Type", "Lead", "Caveat"], [[c["candidate_id"], c["type"], c["title"], c["caveat"]]
                                                         for c in e.get("finding_candidates", [])[:20]]))
        if sig:
            S.append("<h3>Significant tests</h3>" + _table(
                ["Family", "Variables", "Test", "Statistic", "p", "q (BH)", "Effect size"],
                [[x.get("family"), " / ".join(map(str, x.get("variables", []))), x.get("test"), _fmt(x.get("statistic")),
                  _fmt(x.get("p_value")), _fmt(x.get("q_value_bh")),
                  ", ".join(f"{k} {_fmt(v)}" for k, v in (x.get("effect_size") or {}).items())] for x in sig], limit=60))

    # 13 lineage
    S.append(_sec(13))
    stages = stage_summary(log, removed)
    S.append(lineage_svg(stages, {"rows": p0["rows"], "columns": p0["columns"]},
                         {"rows": r.get("rows_after", 0) or 0, "columns": r.get("columns_after", 0) or 0,
                          "status": status}))
    S.append("<h3>Column lineage</h3><div class='controls'><input type='search' placeholder='Filter columns' "
             "data-filter='t-lineage'></div>")
    f1 = {c["column"]: c for c in (p1 or {}).get("columns_profile", [])}
    rows = []
    for c in p0["columns_profile"]:
        name = renames.get(c["column"], c["column"])
        steps = []
        for le in log:
            if le.get("op") and str(le.get("status", "")).startswith(("applied", "flagged")) and \
                    (name in _op_columns(le, renames) or c["column"] in _op_columns(le, renames)):
                n_ch = int((ch["op_id"].eq(le["op_id"]) & ch["column"].isin([name, c["column"]])).sum()) if len(ch) else 0
                steps.append(f"{le['op_id']} {le['op']}" + (f" ({n_ch:,} changed)" if n_ch else
                                                           f" ({le.get('flagged', 0):,} flagged)" if le.get("flagged") else ""))
        a = f1.get(name)
        vchecks = [x["check"] for x in checks if name in x["check"]]
        rows.append([c["column"], c["inferred_type"], " → ".join(steps) or "unchanged",
                     name if a else "(removed)", d["schema"]["columns"].get(name, {}).get("type", "") if a else "",
                     f"{c['missing_count']:,} → {a['missing_count']:,}" if a else "", "; ".join(vchecks)])
    S.append(_table(["Original column", "Type found", "Steps", "Final column", "Final type", "Missing before → after",
                     "Validation checks"], rows, tid="t-lineage"))

    # 14 validation
    S.append(_sec(14))
    if val:
        S.append(_table(["Check", "Result", "Severity", "Detail"], [[c["check"], _pill("passed", "ok") if c["passed"] else
                                                                    _pill("FAILED", "bad" if c["severity"] == "critical" else "warn"),
                                                                    c["severity"], c["detail"]] for c in checks]))
        con = val.get("contract")
        if con:
            S.append(f"<h3>Data contract</h3><p class='small muted'>{_h(con['file'])}. Provenance: confirmed (verified "
                     "rule), user (stated expectation), inferred (suggested by the data), provisional (assumption).</p>")
            S.append(_table(["Rule", "Kind", "Provenance", "Severity", "Before cleaning", "After cleaning", "Detail"], [
                [x["id"], x["rule"], x["provenance"], x["severity"], _fmt(x.get("violations_before_cleaning")),
                 _fmt(x.get("violations")), x["detail"]] for x in con["rules"]]))
            if con.get("conflicts"):
                S.append("<div class='callout warn'>Rules that contradict each other were not resolved: " + "; ".join(
                    f"{_h(' and '.join(c['rules']))} on {_h(c['column'])} ({_h(c['problem'])})" for c in con["conflicts"])
                         + ".</div>")
    else:
        S.append("<p class='bad'>Validation has not been run. Run <code>dm validate</code>.</p>")
    unres = [[x["op_id"], x["column"], x["issue"], x["rationale"]] for x in disabled]
    unres += [["validation", "", c["check"], c["detail"]] for c in checks if not c["passed"]]
    unres += [[x["op_id"], x["column"], "step failed", x.get("error", "")] for x in failed]
    S.append("<h3>Outstanding</h3>" + (_table(["Item", "Column", "Issue", "Detail"], unres) if unres else "<p>None.</p>"))

    # 15 recovery
    S.append(_sec(15))
    S.append("<p>Every change can be undone. <code>dm revert OUT</code> rebuilds the whole original table from the cleaned "
             "data and the audit files and checks it cell by cell; validation ran that check"
             + (f" ({_check_result(checks, 'revert')})" if checks else "") + ". <code>dm restore OUT --op OP-xxx</code> "
             "writes a copy of the cleaned data with one step undone, to <code>restored/</code>; the cleaned data itself "
             "is never overwritten.</p>")
    later = _later_changes(ch, log)
    rec_rows = []
    for x in applied:
        special = []
        if later.get(x["op_id"]):
            special.append(f"{later[x['op_id']]:,} of its cells were changed again by later steps; restoring this step "
                           "alone gives back the value before it and drops those later changes")
        if x["op"] in ("drop_rows", "drop_duplicates", "quarantine_rows"):
            special.append("restored rows come back with their original values; no other step is applied to them")
        if x["op"] == "impute" and (x.get("statistical_basis") or {}).get("indicator_column"):
            special.append(f"its indicator column {x['statistical_basis']['indicator_column']} is dropped on restore")
        if x.get("added_columns"):
            special.append(f"the columns it added ({', '.join(x['added_columns'])}) are dropped on restore")
        rec_rows.append([x["op_id"], x["op"], x.get("column", ""), f"{x.get('affected', 0):,}", x.get("reversal", ""),
                         "; ".join(special) or "none", Raw(f"<code>dm restore OUT --op {_h(x['op_id'])}</code>")])
    S.append(_table(["Step", "Operation", "Column", "Changed", "Where the originals are", "Needs care", "Undo this step"],
                    rec_rows))
    sw = man.get("software", {})
    S.append("<h3>Reproducibility</h3>" + _table(["Item", "Value"], [
        ["Original file", Raw(f"<code>{_h(inp['path'])}</code> (never modified; SHA-256 checked during validation)")],
        ["Reference copy", Raw(f"<code>{_h(inp.get('reference_copy') or 'none (large file)')}</code>")],
        ["Replay", _check_result(checks, "replaying") if checks else "not checked"],
        ["Row identity", Raw(f"<code>{ROW_ID}</code> is the row's position in the original table (1 = first data row)")],
        ["Plan SHA-256", Raw(f"<code>{_h(man.get('plan_sha256', ''))}</code>")], ["Random seed", man.get("random_seed")],
        ["Software", ", ".join(f"{k} {v}" for k, v in sw.items() if k != "platform")]]))

    nav = "".join(f"<a href='#s{i}'>{i}. {_h(t)}</a>" for i, t in enumerate(SECTIONS, 1))
    doc = ("<!doctype html><html lang='en'><head><meta charset='utf-8'>"
           "<meta name='viewport' content='width=device-width,initial-scale=1'>"
           f"<title>Data audit report</title><style>{CSS}</style></head><body><div class='layout'>"
           f"<aside><div class='brand'>dataMedic<small>{_h(inp['file'])}</small></div>"
           "<div class='tools'><button id='theme' type='button'>Light / dark</button> "
           "<button id='menu' class='menu' type='button'>Sections</button></div>"
           f"<nav aria-label='Report sections'>{nav}</nav></aside><main>{''.join(S)}</main></div>"
           f"<script>{JS}</script></body></html>")
    p = os.path.join(out, "reports", "data_audit_report.html")
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as fh:
        fh.write(doc)
    return p


def _sec(i):
    return f"<h2 id='s{i}'>{i}. {_h(SECTIONS[i - 1])}</h2>"


def _fmt(v):
    if v is None or (isinstance(v, float) and v != v):
        return ""
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return fmt_num(v)
    return str(v)


def _mask(df, sens):
    df = df.copy()
    for c in df.columns:
        if c in sens:
            df[c] = df[c].map(lambda v: mask_value(v) if v else v)
    return df


def _check_result(checks, prefix):
    c = next((c for c in checks if c["check"].startswith(prefix)), None)
    return "not checked" if not c else ("passed" if c["passed"] else "FAILED: " + str(c["detail"]))


def _later_changes(ch, log):
    """For each step, how many of the cells it changed were changed again by a later step."""
    if not len(ch):
        return {}
    order = {e["op_id"]: i for i, e in enumerate(log)}
    c = ch.assign(pos=ch["op_id"].map(order))
    last = c.groupby([ROW_ID, "column"])["pos"].transform("max")
    again = c[c["pos"] < last]
    return again.groupby("op_id").size().to_dict()


def _interactive(d, e):
    """Optional interactive charts (plotly), embedded with the plotly.js library inline so the page stays offline."""
    try:
        import plotly.graph_objects as go
        import plotly.io as pio
    except ImportError:
        return ("<div class='callout'>Interactive charts were requested but plotly is not installed; the static "
                "charts above show the same data.</div>")
    figs = []
    ts = (e.get("time_series") or {})
    cleaned = d["cleaned"]
    if ts.get("applicable") and ts.get("column") in cleaned.columns:
        t = pd.to_datetime(cleaned[ts["column"]].mask(cleaned[ts["column"]] == ""), errors="coerce", utc=True)
        for m in list(ts.get("series", {}))[:3]:
            if m == "rows" or m not in cleaned.columns:
                continue
            y = pd.to_numeric(cleaned[m].mask(cleaned[m] == ""), errors="coerce")
            per = {"month": "MS", "week": "W", "day": "D"}.get(ts["frequency"], "D")
            agg = ts["series"][m]["aggregation"]
            s = pd.Series(y.values, index=t).dropna().sort_index()
            s = getattr(s.resample(per), agg)() if agg in ("sum", "mean", "median") else s.resample(per).mean()
            fig = go.Figure(go.Scatter(x=s.index, y=s.values, mode="lines+markers", line={"color": "#0f766e"}))
            fig.update_layout(title=f"{m} per {ts['frequency']} ({agg}); drag to zoom, hover for values",
                              height=340, margin={"l": 40, "r": 10, "t": 50, "b": 40}, template="simple_white")
            figs.append(fig)
    cor = sorted(e.get("correlations", []), key=lambda r: -abs(r["spearman_rho"]))[:1]
    for c in cor:
        x = pd.to_numeric(cleaned[c["a"]].mask(cleaned[c["a"]] == ""), errors="coerce")
        y = pd.to_numeric(cleaned[c["b"]].mask(cleaned[c["b"]] == ""), errors="coerce")
        dd = pd.DataFrame({"x": x, "y": y, "id": cleaned[ROW_ID]}).dropna().head(5000)
        fig = go.Figure(go.Scattergl(x=dd["x"], y=dd["y"], mode="markers", text=dd["id"],
                                     hovertemplate=f"{c['a']} %{{x}}<br>{c['b']} %{{y}}<br>row %{{text}}<extra></extra>",
                                     marker={"size": 5, "opacity": 0.5, "color": "#1e3a5f"}))
        fig.update_layout(title=f"{c['b']} against {c['a']} (Spearman rho {c['spearman_rho']:.2f}, n {c['n']:,})",
                          height=380, margin={"l": 40, "r": 10, "t": 50, "b": 40}, template="simple_white")
        figs.append(fig)
    if not figs:
        return "<p class='muted'>No time series or correlation to show interactively.</p>"
    parts = []
    for i, f in enumerate(figs):
        parts.append(pio.to_html(f, full_html=False, include_plotlyjs=(i == 0), config={"displaylogo": False}))
    return "<h3>Interactive charts</h3><div class='charts'>" + "".join(f"<div>{p}</div>" for p in parts) + "</div>"
