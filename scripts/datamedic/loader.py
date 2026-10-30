"""Find out what a file is and load it as a table of raw strings.

Everything is loaded as text first (nothing is converted, no value is silently turned
into a missing value), so the cleaning stage can decide and log every conversion.
"""

import codecs
import csv
import io
import json
import os
import random
import re
import sqlite3
import sys

import pandas as pd

from .common import ROW_ID, DataMedicError, canon_value, optional, sha256_file

csv.field_size_limit(min(sys.maxsize, 2 ** 31 - 1))

TEXT_FORMATS = {"csv", "tsv", "txt", "json", "jsonl", "xml", "sql"}
EXT_FORMAT = {
    ".csv": "csv", ".tsv": "tsv", ".tab": "tsv", ".txt": "txt", ".dat": "txt", ".psv": "txt",
    ".xlsx": "xlsx", ".xlsm": "xlsx", ".xls": "xls",
    ".json": "json", ".jsonl": "jsonl", ".ndjson": "jsonl",
    ".parquet": "parquet", ".pq": "parquet", ".feather": "feather", ".arrow": "feather",
    ".xml": "xml", ".sqlite": "sqlite", ".sqlite3": "sqlite", ".db": "sqlite", ".sql": "sql",
}


# ---------------------------------------------------------------- format and encoding

def detect_format(path):
    if not os.path.exists(path):
        raise DataMedicError(f"File not found: {path}")
    if os.path.isdir(path):
        raise DataMedicError(f"{path} is a folder. Pass the data files inside it.")
    with open(path, "rb") as fh:
        head = fh.read(16)
    if head.startswith(b"SQLite format 3\x00"):
        return "sqlite"
    if head.startswith(b"PAR1"):
        return "parquet"
    if head.startswith(b"ARROW1"):
        return "feather"
    ext = os.path.splitext(path)[1].lower()
    fmt = EXT_FORMAT.get(ext)
    if head.startswith(b"PK\x03\x04"):
        if fmt in ("xlsx", None):
            return "xlsx"
        raise DataMedicError(f"{os.path.basename(path)} is a ZIP archive, not a {ext} file. "
                             "If it is a compressed dataset, extract it first.")
    if head.startswith(b"\xd0\xcf\x11\xe0"):
        return "xls"
    if fmt is None:
        raise DataMedicError(f"Unsupported file type '{ext or '(none)'}' for {os.path.basename(path)}. "
                             "Supported: CSV, TSV, TXT, Excel (.xlsx/.xls), JSON, JSON Lines, Parquet, "
                             "Feather, XML, SQLite and SQL dumps.")
    return fmt


def detect_encoding(path, sample_bytes=2_000_000):
    with open(path, "rb") as fh:
        raw = fh.read(sample_bytes)
    if raw.startswith(codecs.BOM_UTF8):
        return "utf-8-sig", raw
    if raw.startswith(codecs.BOM_UTF16_LE) or raw.startswith(codecs.BOM_UTF16_BE):
        return "utf-16", raw
    head = raw[:200_000]
    control = sum(head.count(bytes([b])) for b in range(32) if b not in (9, 10, 13))
    if head and (raw.count(b"\x00") > len(raw) * 0.001 or control > len(head) * 0.05):
        raise DataMedicError(f"{os.path.basename(path)} contains binary data (NUL bytes) and is not a "
                             "readable text file. It may be corrupted or in a different format.")
    dec = codecs.getincrementaldecoder("utf-8")()
    try:
        dec.decode(raw, final=len(raw) < sample_bytes)
        return "utf-8", raw
    except UnicodeDecodeError:
        pass
    try:
        raw.decode("cp1252")
        return "cp1252", raw
    except UnicodeDecodeError:
        return "latin-1", raw


def _read_text(path, encoding):
    with open(path, encoding=encoding, newline="") as fh:
        return fh.read()


# ---------------------------------------------------------------- delimited text

def sniff_delimited(text, fmt):
    lines = [ln for ln in text.splitlines()[:300]]
    if fmt == "tsv":
        cands = ["\t"]
    else:
        cands = [",", ";", "\t", "|"]
    best = None
    for d in cands:
        try:
            counts = [len(r) for r in csv.reader(io.StringIO("\n".join(lines)), delimiter=d)]
        except csv.Error:
            continue
        counts = [c for c in counts if c > 0]
        if not counts:
            continue
        mode = max(set(counts), key=counts.count)
        consistency = counts.count(mode) / len(counts)
        score = (mode > 1, consistency, mode)
        if best is None or score > best[0]:
            best = (score, d, mode)
    if best is None or not best[0][0]:
        return {"delimiter": cands[0], "fields": 1}
    return {"delimiter": best[1], "fields": best[2]}


def _looks_like_value(s):
    s = s.strip()
    if not s:
        return False
    if re.match(r"^[+-]?[$€£]?\d[\d,.\s]*%?$", s):
        return True
    if re.match(r"^\d{1,4}[-/.]\d{1,2}[-/.]\d{1,4}", s):
        return True
    return False


def detect_header(rows, n_fields):
    """Find the first row that has the modal field count; decide whether it is a header."""
    start = 0
    for i, r in enumerate(rows[:50]):
        nonempty = [c for c in r if c.strip()]
        if len(r) == n_fields and len(nonempty) >= max(1, n_fields // 2):
            start = i
            break
    first = rows[start] if rows else []
    later = rows[start + 1:start + 51]
    first_values = sum(_looks_like_value(c) for c in first)
    later_values = 0
    for r in later:
        later_values += sum(_looks_like_value(c) for c in r)
    later_rate = later_values / max(1, len(later) * max(1, n_fields))
    nonblank = [c.strip() for c in first if c.strip()]
    header = first_values == 0 and len(nonblank) >= 1 and (later_rate > 0 or len(set(nonblank)) == len(nonblank))
    if first_values > 0 and later_rate > 0:
        header = False
    return start, header


def _fix_headers(names):
    out, renames, seen = [], {}, {}
    for i, n in enumerate(names):
        orig = "" if n is None else str(n)
        new = orig if orig.strip() else f"column_{i + 1}"
        if new == ROW_ID:
            new = ROW_ID + "_source"
        base, k = new, 2
        while new in seen:
            new = f"{base}_{k}"
            k += 1
        seen[new] = True
        if new != orig:
            renames[f"{i}:{orig}"] = new
        out.append(new)
    return out, renames


def _rows_to_frame(header, rows, row_ids):
    data = {ROW_ID: row_ids}
    for j, name in enumerate(header):
        data[name] = [r[j] if j < len(r) else "" for r in rows]
    return pd.DataFrame(data, dtype=object).astype({ROW_ID: "int64"})


def _reservoir(iterable, k, seed):
    rng = random.Random(seed)
    res, total = [], 0
    for item in iterable:
        total += 1
        if len(res) < k:
            res.append(item)
        else:
            j = rng.randrange(total)
            if j < k:
                res[j] = item
    res.sort(key=lambda t: t[0])
    return res, total


def load_delimited(path, fmt, meta, sample=None, seed=42, delimiter=None, header_row=None):
    encoding, raw = detect_encoding(path)
    meta["encoding"] = encoding
    sniff_text = raw.decode(encoding if encoding != "utf-8-sig" else "utf-8-sig", errors="replace")
    sn = sniff_delimited(sniff_text, fmt)
    delim = delimiter or sn["delimiter"]
    meta["delimiter"] = delim
    with open(path, encoding=encoding, newline="", errors="strict") as fh:
        try:
            head_rows = []
            reader = csv.reader(fh, delimiter=delim)
            for r in reader:
                head_rows.append(r)
                if len(head_rows) >= 60:
                    break
        except UnicodeDecodeError as exc:
            raise DataMedicError(f"Could not decode {os.path.basename(path)} as {encoding}: {exc}") from exc
        except csv.Error as exc:
            raise DataMedicError(f"{os.path.basename(path)} is not valid delimited text: {exc}") from exc
    n_fields = sn["fields"] if not delimiter else max((len(r) for r in head_rows), default=1)
    if header_row is not None:
        start, has_header = header_row, True
    else:
        start, has_header = detect_header(head_rows, n_fields)
    meta["preamble_rows_skipped"] = start
    meta["header_detected"] = has_header
    malformed, padded = [], 0

    def gen():
        nonlocal padded
        with open(path, encoding=encoding, newline="") as fh2:
            rd = csv.reader(fh2, delimiter=delim)
            for _ in range(start + (1 if has_header else 0)):
                next(rd, None)
            rid = 0
            for r in rd:
                if not r or (len(r) == 1 and not r[0].strip() and n_fields > 1):
                    meta["blank_lines_skipped"] = meta.get("blank_lines_skipped", 0) + 1
                    continue
                rid += 1
                if len(r) > n_fields:
                    if any(c.strip() for c in r[n_fields:]):
                        malformed.append({"line": rd.line_num, ROW_ID: rid, "fields": len(r),
                                          "raw": delim.join(r)})
                        continue
                    r = r[:n_fields]
                elif len(r) < n_fields:
                    padded += 1
                    r = r + [""] * (n_fields - len(r))
                yield rid, r

    try:
        if has_header:
            header_raw = head_rows[start] if start < len(head_rows) else []
            header_raw = (header_raw + [""] * n_fields)[:n_fields]
        else:
            header_raw = [f"column_{i + 1}" for i in range(n_fields)]
        if sample:
            items, total = _reservoir(gen(), sample, seed)
            meta["sampled"] = total > sample
            meta["rows_before_sampling"] = total
        else:
            items = list(gen())
    except UnicodeDecodeError as exc:
        raise DataMedicError(f"{os.path.basename(path)} has bytes that are not valid {encoding}: {exc}") from exc
    except csv.Error as exc:
        raise DataMedicError(f"{os.path.basename(path)} could not be parsed as delimited text: {exc}") from exc
    header, renames = _fix_headers(header_raw)
    meta["columns_original"] = header_raw
    meta["header_renames"] = renames
    meta["malformed_rows"] = malformed
    meta["short_rows_padded"] = padded
    return _rows_to_frame(header, [r for _, r in items], [i for i, _ in items])


# ---------------------------------------------------------------- Excel

def _excel_sheets(path):
    openpyxl = optional("openpyxl")
    if openpyxl is None:
        raise DataMedicError("Reading .xlsx files needs the Python package 'openpyxl'. Install it with: pip install openpyxl")
    try:
        wb = openpyxl.load_workbook(path, read_only=True, data_only=True)
    except Exception as exc:  # openpyxl raises many types for damaged files
        raise DataMedicError(f"Could not open {os.path.basename(path)} as an Excel workbook: {exc}") from exc
    return wb


def _sheet_rows(ws):
    rows = []
    for r in ws.iter_rows(values_only=True):
        rows.append(["" if v is None else canon_value(v) for v in r])
    width = max((len(r) for r in rows), default=0)
    rows = [r + [""] * (width - len(r)) for r in rows]
    # trim fully empty trailing columns
    while width and all(not r[width - 1].strip() for r in rows):
        width -= 1
        rows = [r[:width] for r in rows]
    return rows


def table_blocks(rows):
    """Split a sheet into blocks of non-blank rows (separate tables are separated by blank rows)."""
    blocks, cur = [], None
    for i, r in enumerate(rows):
        if any(c.strip() for c in r):
            if cur is None:
                cur = [i, i]
            cur[1] = i
        elif cur is not None:
            blocks.append(tuple(cur))
            cur = None
    if cur is not None:
        blocks.append(tuple(cur))
    return blocks


def describe_excel(path):
    wb = _excel_sheets(path)
    out = []
    for ws in wb.worksheets:
        rows = _sheet_rows(ws)
        blocks = table_blocks(rows)
        info = {"sheet": ws.title, "rows": len(rows), "columns": len(rows[0]) if rows else 0,
                "tables": [{"first_row": a + 1, "last_row": b + 1, "rows": b - a + 1} for a, b in blocks]}
        if blocks:
            a, _ = blocks[0]
            info["first_header"] = [c for c in rows[a] if c.strip()][:30]
        out.append(info)
    wb.close()
    return out


def load_excel(path, meta, sheet=None, block=None):
    if path.lower().endswith(".xls"):
        xlrd = optional("xlrd")
        if xlrd is None:
            raise DataMedicError("Reading legacy .xls files needs 'xlrd' (pip install xlrd), "
                                 "or save the workbook as .xlsx.")
        frames = pd.read_excel(path, sheet_name=None, header=None, dtype=object)
        names = list(frames)
        sheet = sheet or names[0]
        if sheet not in frames:
            raise DataMedicError(f"Sheet '{sheet}' not found. Sheets: {', '.join(names)}")
        rows = [[canon_value(v) if not (isinstance(v, float) and v != v) else "" for v in r]
                for r in frames[sheet].itertuples(index=False)]
        meta["sheets"] = names
    else:
        wb = _excel_sheets(path)
        names = wb.sheetnames
        meta["sheets"] = names
        if sheet is None:
            nonempty = [ws.title for ws in wb.worksheets if ws.max_row and ws.max_row > 1]
            if len(nonempty) > 1:
                wb.close()
                raise DataMedicError(
                    "The workbook has several sheets with data: " + ", ".join(nonempty) +
                    ". Run 'discover' to compare them, then choose one with --sheet "
                    "(and process the others separately if they are separate tables).")
            sheet = (nonempty or names)[0]
        if sheet not in names:
            wb.close()
            raise DataMedicError(f"Sheet '{sheet}' not found. Sheets: {', '.join(names)}")
        rows = _sheet_rows(wb[sheet])
        wb.close()
    meta["sheet"] = sheet
    blocks = table_blocks(rows)
    if not blocks:
        raise DataMedicError(f"Sheet '{sheet}' is empty.")
    meta["tables_in_sheet"] = len(blocks)
    if len(blocks) > 1 and block is None:
        meta.setdefault("warnings", []).append(
            f"Sheet '{sheet}' has {len(blocks)} blocks separated by blank rows; using block 1 "
            f"(rows {blocks[0][0] + 1}-{blocks[0][1] + 1}). Use --block N to load another.")
    a, b = blocks[(block or 1) - 1]
    body = rows[a:b + 1]
    width = max(len(r) for r in body)
    start, has_header = detect_header(body, width)
    meta["header_detected"] = has_header
    meta["preamble_rows_skipped"] = start + a
    header_raw = body[start] if has_header else [f"column_{i + 1}" for i in range(width)]
    data = body[start + (1 if has_header else 0):]
    header, renames = _fix_headers(header_raw)
    meta["columns_original"] = header_raw
    meta["header_renames"] = renames
    return _rows_to_frame(header, data, list(range(1, len(data) + 1)))


# ---------------------------------------------------------------- JSON, XML, columnar, SQL

def _records_from_json(obj, meta):
    if isinstance(obj, list):
        return obj, "$"
    if isinstance(obj, dict):
        lists = [(k, v) for k, v in obj.items() if isinstance(v, list) and v and isinstance(v[0], dict)]
        if lists:
            k, v = max(lists, key=lambda kv: len(kv[1]))
            if len(lists) > 1:
                meta.setdefault("warnings", []).append(
                    "JSON has several lists of records: " + ", ".join(k2 for k2, _ in lists) + f". Using '{k}'.")
            return v, f"$.{k}"
        if all(isinstance(v, list) for v in obj.values()) and len({len(v) for v in obj.values()}) == 1:
            return pd.DataFrame(obj).to_dict("records"), "$ (columns)"
        return [obj], "$ (single object)"
    raise DataMedicError("The JSON does not contain a table (expected a list of objects).")


def _frame_from_records(records, meta):
    if records and not isinstance(records[0], dict):
        records = [{"value": r} for r in records]
    df = pd.json_normalize(records, sep=".")
    nested = [c for c in df.columns if df[c].map(lambda v: isinstance(v, (list, dict))).any()]
    if nested:
        meta.setdefault("warnings", []).append(
            "Nested lists kept as JSON text in: " + ", ".join(nested[:10]))
    rows = []
    for rec in df.itertuples(index=False):
        rows.append([json.dumps(v, ensure_ascii=False) if isinstance(v, (list, dict)) else
                     ("" if v is None or (isinstance(v, float) and v != v) else canon_value(v)) for v in rec])
    header, renames = _fix_headers([str(c) for c in df.columns])
    meta["columns_original"] = [str(c) for c in df.columns]
    meta["header_renames"] = renames
    return _rows_to_frame(header, rows, list(range(1, len(rows) + 1)))


def load_json(path, meta):
    encoding, _ = detect_encoding(path)
    meta["encoding"] = encoding
    try:
        obj = json.loads(_read_text(path, encoding))
    except json.JSONDecodeError as exc:
        raise DataMedicError(f"{os.path.basename(path)} is not valid JSON: {exc}") from exc
    records, where = _records_from_json(obj, meta)
    meta["json_path"] = where
    return _frame_from_records(records, meta)


def load_jsonl(path, meta):
    encoding, _ = detect_encoding(path)
    meta["encoding"] = encoding
    records, bad = [], []
    with open(path, encoding=encoding) as fh:
        for i, line in enumerate(fh, 1):
            if not line.strip():
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError as exc:
                bad.append({"line": i, "raw": line.rstrip("\n")[:500], "error": str(exc)})
    if not records:
        raise DataMedicError(f"{os.path.basename(path)} has no valid JSON lines.")
    meta["malformed_rows"] = bad
    return _frame_from_records(records, meta)


def load_columnar(path, fmt, meta):
    if optional("pyarrow") is None:
        raise DataMedicError(f"Reading {fmt} files needs 'pyarrow' (pip install pyarrow).")
    try:
        df = pd.read_parquet(path) if fmt == "parquet" else pd.read_feather(path)
    except Exception as exc:
        raise DataMedicError(f"Could not read {os.path.basename(path)} as {fmt}: {exc}") from exc
    meta["source_types"] = {str(c): str(t) for c, t in df.dtypes.items()}
    return _frame_from_records(df.to_dict("records"), meta) if len(df) else _empty(df, meta)


def _empty(df, meta):
    header, renames = _fix_headers([str(c) for c in df.columns])
    meta["columns_original"] = [str(c) for c in df.columns]
    meta["header_renames"] = renames
    return _rows_to_frame(header, [], [])


def load_xml(path, meta):
    try:
        df = pd.read_xml(path, parser="etree", dtype=str)
    except Exception as exc:
        raise DataMedicError(f"Could not read {os.path.basename(path)} as a flat XML table: {exc}. "
                             "Nested XML needs an explicit XPath; convert it or say which element holds the rows.") from exc
    return _frame_from_records(df.to_dict("records"), meta)


def sqlite_tables(conn):
    out = []
    for (name,) in conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%' ORDER BY name"):
        n = conn.execute(f'SELECT COUNT(*) FROM "{name}"').fetchone()[0]
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{name}")')]
        fks = [{"column": r[3], "references": f"{r[2]}.{r[4]}"} for r in conn.execute(f'PRAGMA foreign_key_list("{name}")')]
        pk = [r[1] for r in conn.execute(f'PRAGMA table_info("{name}")') if r[5]]
        out.append({"table": name, "rows": n, "columns": cols, "primary_key": pk, "foreign_keys": fks})
    return out


def _open_sql(path, fmt):
    if fmt == "sqlite":
        try:
            conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            conn.execute("SELECT name FROM sqlite_master LIMIT 1")
            return conn
        except sqlite3.DatabaseError as exc:
            raise DataMedicError(f"{os.path.basename(path)} is not a readable SQLite database: {exc}") from exc
    encoding, _ = detect_encoding(path)
    conn = sqlite3.connect(":memory:")
    try:
        conn.executescript(_read_text(path, encoding))
    except sqlite3.Error as exc:
        raise DataMedicError(f"{os.path.basename(path)} could not be loaded as SQLite-compatible SQL ({exc}). "
                             "Load the dump into its own database and export the table as CSV.") from exc
    return conn


def load_sql(path, fmt, meta, table=None):
    conn = _open_sql(path, fmt)
    tables = sqlite_tables(conn)
    meta["tables"] = tables
    if not tables:
        raise DataMedicError(f"{os.path.basename(path)} has no tables.")
    if table is None:
        if len(tables) > 1:
            raise DataMedicError("The database has several tables: " + ", ".join(t["table"] for t in tables) +
                                 ". Choose one with --table; run 'discover' to see keys and relationships.")
        table = tables[0]["table"]
    if table not in [t["table"] for t in tables]:
        raise DataMedicError(f"Table '{table}' not found.")
    meta["table"] = table
    cur = conn.execute(f'SELECT * FROM "{table}"')
    header_raw = [d[0] for d in cur.description]
    rows = [["" if v is None else canon_value(v) for v in r] for r in cur.fetchall()]
    conn.close()
    header, renames = _fix_headers(header_raw)
    meta["columns_original"] = header_raw
    meta["header_renames"] = renames
    return _rows_to_frame(header, rows, list(range(1, len(rows) + 1)))


# ---------------------------------------------------------------- entry points

def load_table(path, sheet=None, table=None, block=None, sample=None, seed=42, delimiter=None, header_row=None):
    """Load any supported file as raw strings. Returns (DataFrame, meta)."""
    fmt = detect_format(path)
    meta = {"path": os.path.abspath(path), "file": os.path.basename(path), "format": fmt,
            "size_bytes": os.path.getsize(path), "sha256": sha256_file(path), "sampled": False}
    if meta["size_bytes"] == 0:
        raise DataMedicError(f"{os.path.basename(path)} is empty (0 bytes).")
    if fmt in ("csv", "tsv", "txt"):
        df = load_delimited(path, fmt, meta, sample=sample, seed=seed, delimiter=delimiter, header_row=header_row)
    elif fmt in ("xlsx", "xls"):
        df = load_excel(path, meta, sheet=sheet, block=block)
    elif fmt == "json":
        df = load_json(path, meta)
    elif fmt == "jsonl":
        df = load_jsonl(path, meta)
    elif fmt in ("parquet", "feather"):
        df = load_columnar(path, fmt, meta)
    elif fmt == "xml":
        df = load_xml(path, meta)
    elif fmt in ("sqlite", "sql"):
        df = load_sql(path, fmt, meta, table=table)
    else:  # pragma: no cover
        raise DataMedicError(f"Unsupported format {fmt}")
    if sample and not meta.get("rows_before_sampling") and len(df) > sample:
        meta["rows_before_sampling"] = len(df)
        df = df.sample(n=sample, random_state=seed).sort_values(ROW_ID).reset_index(drop=True)
        meta["sampled"] = True
    if df.shape[1] <= 1:
        raise DataMedicError(f"{os.path.basename(path)} has no data columns after parsing.")
    meta["rows"] = int(len(df))
    meta["columns"] = int(df.shape[1] - 1)
    return df, meta


def discover(path):
    """Describe a file without committing to one table: format, encoding, sheets, tables, shape."""
    fmt = detect_format(path)
    info = {"path": os.path.abspath(path), "file": os.path.basename(path), "format": fmt,
            "size_bytes": os.path.getsize(path), "sha256": sha256_file(path), "warnings": []}
    if fmt in ("xlsx",):
        info["sheets"] = describe_excel(path)
        filled = [s for s in info["sheets"] if s["rows"] > 1]
        if len(filled) > 1:
            info["warnings"].append("Several sheets hold data. Compare their headers to decide whether they are "
                                    "related tables, separate datasets or versions of one table.")
        for s in info["sheets"]:
            if len(s["tables"]) > 1:
                info["warnings"].append(f"Sheet '{s['sheet']}' has {len(s['tables'])} blocks separated by blank rows.")
        return info
    if fmt in ("sqlite", "sql"):
        conn = _open_sql(path, fmt)
        info["tables"] = sqlite_tables(conn)
        conn.close()
        return info
    meta = {}
    try:
        df, meta = load_table(path)
    except DataMedicError as exc:
        info["error"] = str(exc)
        return info
    info.update({k: meta[k] for k in ("encoding", "delimiter", "header_detected", "preamble_rows_skipped",
                                      "json_path", "source_types") if k in meta})
    info["rows"] = int(len(df))
    info["columns"] = int(df.shape[1] - 1)
    info["column_names"] = [c for c in df.columns if c != ROW_ID]
    info["malformed_rows"] = len(meta.get("malformed_rows", []))
    info["short_rows_padded"] = meta.get("short_rows_padded", 0)
    info["header_renames"] = meta.get("header_renames", {})
    info["warnings"] += meta.get("warnings", [])
    empty_rows = int((df.drop(columns=[ROW_ID]).apply(lambda s: s.str.strip() == "")).all(axis=1).sum())
    info["empty_rows"] = empty_rows
    return info


def compare_schemas(infos, frames=None):
    """For several files: shared columns, likely keys, whether they look like one table split up."""
    cols = {i["file"]: i.get("column_names") or [] for i in infos}
    names = list(cols)
    out = {"files": names, "pairs": []}
    for a in range(len(names)):
        for b in range(a + 1, len(names)):
            ca, cb = cols[names[a]], cols[names[b]]
            shared = [c for c in ca if c in cb]
            norm = {c.strip().casefold().replace(" ", "_"): c for c in ca}
            near = [(norm[k], c) for c in cb for k in [c.strip().casefold().replace(" ", "_")]
                    if k in norm and norm[k] != c]
            if not ca or not cb:
                out["pairs"].append({"a": names[a], "b": names[b], "shared_columns": [], "name_variants": [],
                                     "assessment": "not compared: a workbook or database holds several tables; "
                                                   "compare its sheets or tables individually"})
                continue
            same = set(ca) == set(cb)
            kind = ("same schema (possible split of one table, or versions)" if same else
                    "related tables (shared key candidates)" if shared else "no shared columns")
            out["pairs"].append({"a": names[a], "b": names[b], "shared_columns": shared,
                                 "name_variants": near, "assessment": kind})
    return out
