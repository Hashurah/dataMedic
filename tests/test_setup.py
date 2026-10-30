"""Installation helpers: setup.py --check and dm.py's choice of Python."""

import os
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DM = os.path.join(ROOT, "scripts", "dm.py")


def _run(args, **env):
    e = {k: v for k, v in os.environ.items() if not k.startswith("DATAMEDIC_")}
    e.update(env)
    return subprocess.run([sys.executable, *args], capture_output=True, text=True, env=e)


def test_dm_uses_the_python_named_in_datamedic_python():
    res = _run([DM, "check"], DATAMEDIC_PYTHON=sys.executable)
    assert res.returncode == 0, res.stderr
    assert "Environment: DATAMEDIC_PYTHON" in res.stdout and "pandas" in res.stdout


def test_dm_reports_a_missing_interpreter_clearly(tmp_path):
    res = _run([DM, "check"], DATAMEDIC_PYTHON=str(tmp_path / "no-such-python"))
    assert res.returncode == 1
    assert "cannot run the Python" in res.stderr and "Traceback" not in res.stderr


def test_exit_codes_pass_through_the_environment_switch():
    res = _run([DM, "profile", "does-not-exist.csv"], DATAMEDIC_PYTHON=sys.executable)
    assert res.returncode == 1 and "File not found" in res.stderr


def test_setup_check_reports_installed_libraries():
    if os.path.exists(os.path.join(ROOT, ".venv")):
        return  # --check inspects the skill's own environment when it exists
    res = _run([os.path.join(ROOT, "setup.py"), "--check"])
    assert res.returncode == 0, res.stdout
    assert "Ready." in res.stdout and "pandas" in res.stdout
