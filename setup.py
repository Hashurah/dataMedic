#!/usr/bin/env python3
"""Install dataMedic's Python libraries into a private environment inside the skill folder.

    python setup.py              required and optional libraries (see requirements.txt)
    python setup.py --minimal    only what the core needs: pandas, numpy, scipy, matplotlib
    python setup.py --check      report what is installed, change nothing

The environment lives in .venv/ next to this file (ignored by git). scripts/dm.py uses it
automatically, so nothing needs activating. Delete the .venv folder to uninstall.
Uses only the standard library, so it runs on a bare Python 3.9+.
"""

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
VENV = os.path.join(HERE, ".venv")
REQUIREMENTS = os.path.join(HERE, "requirements.txt")
CORE = ["pandas>=2.0", "numpy>=1.24", "scipy>=1.10", "matplotlib>=3.7"]
MODULES = [("pandas", True), ("numpy", True), ("scipy", True), ("matplotlib", True),
           ("openpyxl", False), ("pyarrow", False), ("sklearn", False)]


def venv_python():
    if os.name == "nt":
        return os.path.join(VENV, "Scripts", "python.exe")
    return os.path.join(VENV, "bin", "python")


def check(python):
    code = ("import importlib,json;out={}\n"
            "for m in %r:\n"
            "    try:\n"
            "        out[m]=getattr(importlib.import_module(m),'__version__','installed')\n"
            "    except Exception:\n"
            "        out[m]=None\n"
            "print(json.dumps(out))") % [m for m, _ in MODULES]
    res = subprocess.run([python, "-c", code], capture_output=True, text=True)
    if res.returncode != 0:
        return None
    import json
    return json.loads(res.stdout.strip().splitlines()[-1])


def report(found, python):
    print(f"Python: {python}")
    missing_core = []
    for m, core in MODULES:
        v = found.get(m) if found else None
        print(f"  {m:11} {v or 'missing'}{'' if core else '  (optional)'}")
        if core and not v:
            missing_core.append(m)
    return missing_core


def main():
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--minimal", action="store_true", help="install only pandas, numpy, scipy and matplotlib")
    ap.add_argument("--check", action="store_true", help="show what is installed and exit")
    a = ap.parse_args()

    if sys.version_info < (3, 9):
        print(f"dataMedic needs Python 3.9 or later; this is {sys.version.split()[0]}.")
        return 1
    py = venv_python()
    if a.check:
        target = py if os.path.exists(py) else sys.executable
        missing = report(check(target), target)
        print("Ready." if not missing else "Missing required libraries: " + ", ".join(missing)
              + ". Run: python setup.py")
        return 0 if not missing else 1

    if not os.path.exists(py):
        print(f"Creating a private environment in {VENV}")
        res = subprocess.run([sys.executable, "-m", "venv", VENV])
        if res.returncode != 0 or not os.path.exists(py):
            print("Could not create the environment. On Debian or Ubuntu, install the venv module first: "
                  "sudo apt install python3-venv")
            return 1
    subprocess.run([py, "-m", "pip", "install", "--upgrade", "--quiet", "pip"])
    args = CORE if a.minimal else ["-r", REQUIREMENTS]
    print("Installing " + ("core libraries" if a.minimal else "libraries from requirements.txt") + " ...")
    res = subprocess.run([py, "-m", "pip", "install", "--quiet", *args])
    if res.returncode != 0:
        print("Installation failed; see the pip output above. Nothing outside .venv was changed.")
        return 1
    missing = report(check(py), py)
    if missing:
        print("Missing required libraries after installation: " + ", ".join(missing))
        return 1
    print("\nReady. Run dataMedic with:  python " + os.path.join(HERE, "scripts", "dm.py") + " --help")
    print("It uses this environment automatically. Delete the .venv folder to uninstall.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
