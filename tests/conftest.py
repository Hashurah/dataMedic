import os
import sys

import pytest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(os.path.dirname(HERE), "scripts")
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, HERE)

import datasets  # noqa: E402


@pytest.fixture(scope="session")
def data(tmp_path_factory):
    d = tmp_path_factory.mktemp("data")
    return datasets.build_all(str(d))


@pytest.fixture(scope="session")
def runs(tmp_path_factory, data):
    """Run the whole pipeline once per dataset (no charts, for speed) and cache the output folders."""
    from datamedic.eda import EDA
    from datamedic.pipeline import run_clean, run_profile
    from datamedic.report import build_reports
    from datamedic.validate import validate

    root = tmp_path_factory.mktemp("runs")
    cache = {}

    def get(name, path=None, **load):
        key = name
        if key in cache:
            return cache[key]
        src = path or data[name]
        out = str(root / name)
        res = run_profile(src, out, **load)
        run_clean(src, os.path.join(out, "plan.json"), out)
        val = validate(out)
        eda = EDA(out, charts_on=False).run()
        build_reports(out)
        cache[key] = {"out": out, "src": src, "profile": res, "validation": val, "eda": eda}
        return cache[key]

    return get
