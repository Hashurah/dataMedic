#!/usr/bin/env python3
"""Measure dataMedic on synthetic messy data of increasing size.

    python tests/benchmark.py 10000 100000          # one fresh process per size
    python tests/benchmark.py --one 10000            # (internal) run one size, print JSON

Reports seconds per stage, peak memory of the process, and the size of the audit trail and the HTML
report. Not part of the pytest run; results are recorded in references/scalability.md.
"""

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(os.path.dirname(HERE), "scripts"))


def peak_memory_mb():
    if os.name == "nt":
        import ctypes
        from ctypes import wintypes

        class PMC(ctypes.Structure):
            _fields_ = [("cb", wintypes.DWORD), ("PageFaultCount", wintypes.DWORD),
                        ("PeakWorkingSetSize", ctypes.c_size_t), ("WorkingSetSize", ctypes.c_size_t),
                        ("QuotaPeakPagedPoolUsage", ctypes.c_size_t), ("QuotaPagedPoolUsage", ctypes.c_size_t),
                        ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t), ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                        ("PagefileUsage", ctypes.c_size_t), ("PeakPagefileUsage", ctypes.c_size_t)]
        pmc = PMC()
        pmc.cb = ctypes.sizeof(PMC)
        k32, psapi = ctypes.WinDLL("kernel32"), ctypes.WinDLL("psapi")
        k32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [wintypes.HANDLE, ctypes.POINTER(PMC), wintypes.DWORD]
        psapi.GetProcessMemoryInfo(k32.GetCurrentProcess(), ctypes.byref(pmc), pmc.cb)
        return pmc.PeakWorkingSetSize / 1e6
    import resource
    kb = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return kb / 1e3 if sys.platform != "darwin" else kb / 1e6


def make(n, path, seed=42):
    import numpy as np
    import pandas as pd
    rng = np.random.default_rng(seed)
    days = pd.date_range("2022-01-01", "2024-12-31", freq="D")
    d = rng.choice(days, n)
    region = rng.choice(["North", "south", "EAST ", "West", "north"], n)
    cat = rng.choice(["Home", "Toys", "Garden", "Electronics"], n)
    qty = rng.integers(1, 6, n).astype(object)
    price = (rng.lognormal(3.5, 0.6, n)).round(2)
    price_s = np.where(rng.random(n) < 0.3, ["$" + f"{p:,.2f}" for p in price], price.astype(str)).astype(object)
    disc = rng.choice(["0", "5", "10", "N/A", ""], n, p=[0.5, 0.2, 0.1, 0.1, 0.1]).astype(object)
    rating = rng.choice(["1", "2", "3", "4", "5", "NULL"], n, p=[0.05, 0.1, 0.2, 0.3, 0.25, 0.1]).astype(object)
    df = pd.DataFrame({"order_id": [f"SO-{i:07d}" for i in range(n)],
                       "order_date": pd.Series(d).dt.strftime("%Y-%m-%d"), "region": region, "category": cat,
                       "quantity": qty, "unit_price": price_s, "discount": disc, "rating": rating,
                       "customer_email": [f"c{int(i)}@mail.com" for i in rng.integers(0, max(10, n // 5), n)]})
    df.loc[rng.choice(n, max(1, n // 200), replace=False), "quantity"] = "-999"
    dup = df.sample(n=max(1, n // 100), random_state=seed)
    pd.concat([df, dup]).to_csv(path, index=False)


def one(n):
    from datamedic.eda import EDA
    from datamedic.pipeline import run_clean, run_profile
    from datamedic.report import build_reports
    from datamedic.validate import validate
    tmp = tempfile.mkdtemp(prefix="dm_bench_")
    try:
        src = os.path.join(tmp, f"bench_{n}.csv")
        make(n, src)
        out = os.path.join(tmp, "out")
        t = {}
        s = time.perf_counter()
        run_profile(src, out)
        t["profile_and_plan"] = time.perf_counter() - s
        s = time.perf_counter()
        run_clean(src, os.path.join(out, "plan.json"), out)
        t["clean"] = time.perf_counter() - s
        s = time.perf_counter()
        v = validate(out)
        t["validate_with_revert_and_replay"] = time.perf_counter() - s
        s = time.perf_counter()
        EDA(out).run()
        t["eda_with_charts"] = time.perf_counter() - s
        s = time.perf_counter()
        build_reports(out)
        t["reports"] = time.perf_counter() - s

        def size(p):
            if os.path.isfile(p):
                return os.path.getsize(p)
            return sum(os.path.getsize(os.path.join(a, f)) for a, _, fs in os.walk(p) for f in fs)

        return {"rows": n + max(1, n // 100), "seconds": {k: round(x, 2) for k, x in t.items()},
                "total_seconds": round(sum(t.values()), 2), "peak_memory_mb": round(peak_memory_mb(), 1),
                "input_mb": round(size(src) / 1e6, 2), "audit_mb": round(size(os.path.join(out, "audit")) / 1e6, 2),
                "html_report_mb": round(size(os.path.join(out, "reports", "data_audit_report.html")) / 1e6, 2),
                "validation": v["status"]}
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    if sys.argv[1:2] == ["--one"]:
        print(json.dumps(one(int(sys.argv[2]))))
        return
    sizes = [int(x) for x in sys.argv[1:]] or [10_000, 100_000]
    rows = []
    for n in sizes:
        res = subprocess.run([sys.executable, os.path.abspath(__file__), "--one", str(n)], capture_output=True, text=True)
        if res.returncode:
            print(res.stderr[-2000:])
            continue
        r = json.loads(res.stdout.strip().splitlines()[-1])
        rows.append(r)
        print(json.dumps(r))
    with open(os.path.join(HERE, "benchmark_results.json"), "w", encoding="utf-8") as fh:
        json.dump({"python": sys.version.split()[0], "platform": sys.platform, "results": rows}, fh, indent=2)


if __name__ == "__main__":
    main()
