"""Synthetic datasets with known, deliberately planted problems. Deterministic (fixed seeds)."""

import json
import os
import sqlite3

import numpy as np
import pandas as pd


def _w(df, path, **kw):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    df.to_csv(path, index=False, lineterminator="\n", **kw)
    return path


def simple_sales(d, n=300):
    rng = np.random.default_rng(1)
    dates = pd.date_range("2023-01-01", periods=365, freq="D")
    cat = rng.choice(["Electronics", "Home", "Toys", "Books"], n, p=[0.4, 0.3, 0.2, 0.1])
    base = {"Electronics": 250, "Home": 60, "Toys": 25, "Books": 15}
    q = rng.integers(1, 6, n)
    price = np.array([round(base[c] * rng.uniform(0.8, 1.2), 2) for c in cat])
    df = pd.DataFrame({"order_id": [f"ORD-{i:05d}" for i in range(1, n + 1)],
                       "order_date": rng.choice(dates, n).astype("datetime64[ns]").astype(str),
                       "region": rng.choice(["North", "South", "East", "West"], n),
                       "product_category": cat, "quantity": q, "unit_price": price,
                       "revenue": np.round(q * price, 2),
                       "customer_id": [f"C{rng.integers(1, 60):03d}" for _ in range(n)]})
    df["order_date"] = df["order_date"].str[:10]
    return _w(df, os.path.join(d, "01_simple_sales.csv"))


def heavy_missing(d, n=400):
    rng = np.random.default_rng(2)
    region = rng.choice(["A", "B", "C"], n)
    income = rng.normal(50000, 8000, n).round(0)
    income[region == "C"] += 30000
    age = rng.integers(18, 80, n).astype(float)
    df = pd.DataFrame({"respondent_id": range(1, n + 1), "region": region, "age": age, "income": income,
                       "satisfaction": rng.integers(1, 6, n), "hours_online": rng.gamma(2, 3, n).round(1)})
    miss_inc = (region == "C") & (rng.random(n) < 0.7) | (rng.random(n) < 0.05)
    df.loc[miss_inc, "income"] = np.nan
    df.loc[rng.random(n) < 0.08, "age"] = np.nan
    df.loc[rng.random(n) < 0.55, "hours_online"] = np.nan
    return _w(df, os.path.join(d, "02_heavy_missing.csv"))


def duplicates(d):
    rng = np.random.default_rng(3)
    n = 120
    df = pd.DataFrame({"invoice_id": [f"INV{i:04d}" for i in range(n)], "customer": rng.choice(["Acme", "Globex", "Initech"], n),
                       "amount": rng.uniform(10, 500, n).round(2), "status": rng.choice(["paid", "open"], n)})
    exact = df.sample(10, random_state=1)
    variants = df.sample(5, random_state=2).copy()
    variants["customer"] = variants["customer"].str.upper() + "  "
    conflict = df.sample(3, random_state=3).copy()
    conflict["amount"] = conflict["amount"] + 1
    out = pd.concat([df, exact, variants, conflict], ignore_index=True)
    return _w(out, os.path.join(d, "03_duplicates.csv"))


def outliers(d):
    rng = np.random.default_rng(4)
    n = 200
    x = rng.normal(100, 10, n).round(2)
    x[[5, 50, 150]] = [1000.0, 950.0, -500.0]
    df = pd.DataFrame({"sensor_id": [f"S{i % 10}" for i in range(n)], "reading": x,
                       "temperature": rng.normal(21, 1.5, n).round(2)})
    return _w(df, os.path.join(d, "04_outliers.csv"))


def mixed_dates(d):
    vals = ["2024-01-05", "25/01/2024", "13/02/2024", "03/03/2024", "Jan 7, 2024", "2024-13-45", "", "07/04/2024",
            "2024-04-10", "30/04/2024"] * 3
    df = pd.DataFrame({"event_id": range(1, len(vals) + 1), "event_date": vals, "attendees": range(10, 10 + len(vals))})
    return _w(df, os.path.join(d, "05_mixed_dates.csv"))


def ambiguous_dates(d):
    vals = ["01/02/2024", "03/04/2024", "05/06/2024", "07/08/2024", "09/10/2024", "11/12/2024"] * 4
    df = pd.DataFrame({"id": range(1, len(vals) + 1), "visit_date": vals, "score": range(len(vals))})
    return _w(df, os.path.join(d, "05b_ambiguous_dates.csv"))


def labels(d):
    rng = np.random.default_rng(6)
    n = 150
    g = rng.choice(["Male", "Female"], n)
    g = np.where(rng.random(n) < 0.1, np.char.lower(g.astype(str)), g)
    g = np.where(rng.random(n) < 0.05, np.char.upper(g.astype(str)), g)
    g = np.where(rng.random(n) < 0.05, " " + g + " ", g)
    cat = rng.choice(["Electronics", "Clothing", "Garden"], n)
    cat[:4] = ["electronics ", "Electronic", "Electronic", "CLOTHING"]
    df = pd.DataFrame({"customer_ref": [f"R{i}" for i in range(n)], "gender": g, "category": cat,
                       "spend": rng.uniform(5, 300, n).round(2)})
    return _w(df, os.path.join(d, "06_labels.csv"))


def mixed_types(d):
    vals = ["12", "15.5", "$1,200", "n/a", "twelve", "45%", "8", "9", "10", "11", "13", "14", "16", "17", "18", "19",
            "20", "21", "22", "23", "24", "25", "26", "27", "28", "29", "30", "31", "32", "33"]
    price = ["$1,200.50", "$3.99", "€4,50", "1 234", "(15.00)", "$7", "$8.25", "$9", "$10", "$11"] * 3
    df = pd.DataFrame({"item": [f"I{i}" for i in range(len(vals))], "value": vals, "price": price,
                       "zip_code": ["02139", "10001", "00501"] * 10, "in_stock": ["yes", "no", "Yes"] * 10})
    return _w(df, os.path.join(d, "07_mixed_types.csv"))


def skewed(d):
    rng = np.random.default_rng(8)
    n = 500
    inc = rng.lognormal(10.5, 0.8, n).round(0)
    df = pd.DataFrame({"household_id": range(n), "income": inc, "segment": rng.choice(["urban", "rural"], n)})
    df.loc[rng.random(n) < 0.1, "income"] = np.nan
    return _w(df, os.path.join(d, "08_skewed.csv"))


def timeseries(d):
    rng = np.random.default_rng(9)
    days = pd.date_range("2024-01-01", "2024-06-30", freq="D")
    keep = np.ones(len(days), bool)
    keep[40:47] = False
    days = days[keep]
    t = np.arange(len(days))
    sales = 1000 + 3 * t + 150 * np.sin(2 * np.pi * t / 7) + rng.normal(0, 30, len(days))
    df = pd.DataFrame({"date": days.strftime("%Y-%m-%d"), "daily_sales": sales.round(2),
                       "visits": (sales / 10 + rng.normal(0, 5, len(days))).round(0)})
    df.loc[[10, 11, 60, 100], "daily_sales"] = np.nan
    return _w(df, os.path.join(d, "09_timeseries.csv"))


def multitable(d):
    cust = pd.DataFrame({"customer_id": ["C1", "C2", "C3", "C4", "C4"], "name_segment": ["gold", "silver", "gold", "bronze", "bronze"]})
    orders = pd.DataFrame({"order_id": range(1, 11), "customer_id": ["C1", "C1", "C2", "C3", "C9", "c2", "C2 ", "C3", "C8", "C1"],
                           "amount": [10, 20, 30, 40, 50, 60, 70, 80, 90, 100]})
    return _w(cust, os.path.join(d, "10_customers.csv")), _w(orders, os.path.join(d, "10_orders.csv"))


def legit_extremes(d):
    rng = np.random.default_rng(11)
    n = 600
    rev = rng.pareto(1.6, n) * 100 + 20
    df = pd.DataFrame({"account": [f"A{i:04d}" for i in range(n)], "annual_revenue": rev.round(2),
                       "employees": (rev / 10 + rng.integers(1, 20, n)).round(0)})
    return _w(df, os.path.join(d, "11_legit_extremes.csv"))


def empty_cols(d):
    df = pd.DataFrame({"id": range(1, 21), "value": range(20), "notes": [""] * 20, "legacy_code": ["NULL"] * 20,
                       "constant": ["v1"] * 20})
    return _w(df, os.path.join(d, "12_empty_columns.csv"))


def placeholders(d):
    rng = np.random.default_rng(13)
    n = 80
    allergies = rng.choice(["None", "Peanuts", "Shellfish", "Pollen"], n, p=[0.6, 0.15, 0.1, 0.15])
    weight = rng.normal(70, 10, n).round(1).astype(object)
    weight[[3, 9, 27]] = "-999"
    weight[[4, 33]] = "N/A"
    visits = rng.integers(0, 5, n)
    df = pd.DataFrame({"patient_ref": [f"P{i}" for i in range(n)], "allergies": allergies, "weight_kg": weight,
                       "visits": visits, "notes": rng.choice(["N/A", "follow up", "ok"], n)})
    return _w(df, os.path.join(d, "13_placeholders.csv"))


def correlated(d):
    rng = np.random.default_rng(14)
    n = 300
    x = rng.normal(0, 1, n)
    df = pd.DataFrame({"ad_spend": (x * 100 + 1000).round(2), "clicks": (x * 200 + 5000 + rng.normal(0, 20, n)).round(0),
                       "impressions": (x * 2000 + 90000 + rng.normal(0, 300, n)).round(0),
                       "noise": rng.normal(0, 1, n).round(3), "channel": rng.choice(["search", "social"], n)})
    return _w(df, os.path.join(d, "14_correlated.csv"))


def sensitive(d):
    rng = np.random.default_rng(15)
    n = 40
    df = pd.DataFrame({"full_name": [f"Person{i} Surname{i}" for i in range(n)],
                       "email": [f"person{i}@example.com" for i in range(n)],
                       "phone": [f"+1 555-01{i:02d}" for i in range(n)],
                       "ssn": [f"{100 + i:03d}-45-{6000 + i}" for i in range(n)],
                       "age": rng.integers(20, 70, n), "plan": rng.choice(["basic", "pro"], n)})
    df.loc[[2, 3], "email"] = ["  person2@example.com", "PERSON3@EXAMPLE.COM"]
    df.loc[5, "age"] = -4
    return _w(df, os.path.join(d, "15_sensitive.csv"))


def invalid_logic(d):
    df = pd.DataFrame({"booking_id": range(1, 31),
                       "start_date": ["2024-03-01"] * 30, "end_date": ["2024-03-05"] * 28 + ["2024-02-20", "2024-02-01"],
                       "quantity": [2] * 29 + [-3], "unit_price": [10.0] * 30, "total": [20.0] * 27 + [25.0, 20.0, -30.0],
                       "discount_pct": [5] * 28 + [150, 10], "age": [30] * 29 + [-2]})
    return _w(df, os.path.join(d, "16_invalid_logic.csv"))


def ragged(d):
    p = os.path.join(d, "17_ragged.csv")
    with open(p, "w", newline="", encoding="utf-8") as fh:
        fh.write("Report generated 2024-05-01\n\nid,name_of_item,qty\n1,apple,3\n2,banana\n3,cherry,5,EXTRA\n4,date,7\n")
    return p


def corrupted(d):
    p = os.path.join(d, "18_corrupted.csv")
    with open(p, "wb") as fh:
        fh.write(bytes(range(256)) * 20)
    return p


def unsupported(d):
    p = os.path.join(d, "19_document.docx_unknown.xyz")
    with open(p, "w") as fh:
        fh.write("not a table")
    return p


def formats(d):
    rng = np.random.default_rng(20)
    df = pd.DataFrame({"id": range(1, 31), "city": rng.choice(["Lagos", "Leeds", "Lyon"], 30),
                       "score": rng.normal(50, 10, 30).round(1)})
    out = {}
    out["json"] = os.path.join(d, "20_records.json")
    with open(out["json"], "w") as fh:
        json.dump({"meta": {"source": "test"}, "data": df.to_dict("records")}, fh)
    out["jsonl"] = os.path.join(d, "20_records.jsonl")
    with open(out["jsonl"], "w") as fh:
        for r in df.to_dict("records"):
            fh.write(json.dumps(r) + "\n")
        fh.write("{broken json\n")
    out["parquet"] = os.path.join(d, "20_records.parquet")
    df.to_parquet(out["parquet"], index=False)
    out["tsv"] = os.path.join(d, "20_records.tsv")
    df.to_csv(out["tsv"], sep="\t", index=False)
    out["semicolon"] = os.path.join(d, "20_semicolon.csv")
    with open(out["semicolon"], "w", encoding="cp1252", newline="") as fh:
        fh.write("id;ville;montant\n1;Montréal;1.234,50\n2;Québec;99,90\n3;Paris;12,00\n4;Nîmes;3.000,00\n")
    out["xlsx"] = os.path.join(d, "20_workbook.xlsx")
    with pd.ExcelWriter(out["xlsx"]) as xw:
        df.to_excel(xw, sheet_name="Scores", index=False)
        df.head(5).to_excel(xw, sheet_name="Old copy", index=False)
    out["sqlite"] = os.path.join(d, "20_shop.sqlite")
    if os.path.exists(out["sqlite"]):
        os.remove(out["sqlite"])
    con = sqlite3.connect(out["sqlite"])
    con.executescript("CREATE TABLE customers(id INTEGER PRIMARY KEY, segment TEXT);"
                      "CREATE TABLE orders(id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id), amount REAL);"
                      "INSERT INTO customers VALUES (1,'a'),(2,'b');"
                      "INSERT INTO orders VALUES (1,1,10.5),(2,2,3.0),(3,5,7.25);")
    con.commit()
    con.close()
    out["xml"] = os.path.join(d, "20_records.xml")
    df.to_xml(out["xml"], index=False, parser="etree")
    return out


ALL = [simple_sales, heavy_missing, duplicates, outliers, mixed_dates, ambiguous_dates, labels, mixed_types, skewed,
       timeseries, legit_extremes, empty_cols, placeholders, correlated, sensitive, invalid_logic]


def build_all(d):
    os.makedirs(d, exist_ok=True)
    paths = {f.__name__: f(d) for f in ALL}
    paths["multitable"] = multitable(d)
    paths["ragged"] = ragged(d)
    paths["corrupted"] = corrupted(d)
    paths["unsupported"] = unsupported(d)
    paths["formats"] = formats(d)
    return paths


if __name__ == "__main__":
    import sys
    print(json.dumps(build_all(sys.argv[1] if len(sys.argv) > 1 else "test_data"), indent=2, default=str))
