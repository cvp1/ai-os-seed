#!/usr/bin/env python3
"""Stdlib-only self-test for _lib. No network, no secrets touched.

Run: /usr/bin/python3 _lib/selftest.py
Exits 0 on success, non-zero with the failing checks listed.

Two jobs: (1) each shipped module imports and passes one pure-logic check;
(2) the load-bearing invariant that _lib is stdlib-only holds — every
module must import under `python3 -I -S` (no site-packages, no user site).
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _lib import event_bus, report, secrets  # noqa: E402,F401

FAILS = []


def check(name, cond):
    print(("ok   " if cond else "FAIL ") + name)
    if not cond:
        FAILS.append(name)


# --- secrets ---------------------------------------------------------------
os.environ["SEED_SELFTEST_KEY"] = "  from-env  "
check("secrets: env wins and is stripped",
      secrets.load_secret("SEED_SELFTEST_KEY", "/nonexistent") == "from-env")
del os.environ["SEED_SELFTEST_KEY"]
with tempfile.NamedTemporaryFile("w", suffix=".key", delete=False) as fh:
    fh.write("from-file\n")
    keyfile = fh.name
try:
    check("secrets: file fallback and is stripped",
          secrets.load_secret("SEED_SELFTEST_MISSING", keyfile) == "from-file")
finally:
    os.unlink(keyfile)

# --- report ----------------------------------------------------------------
check("report: Report builder exists and is callable",
      callable(getattr(report, "Report", None)))

# --- stdlib-only invariant --------------------------------------------------
here = os.path.dirname(os.path.abspath(__file__))
for mod in ["secrets", "event_bus", "report"]:
    r = subprocess.run(
        [sys.executable, "-I", "-S", "-c",
         f"import sys; sys.path.insert(0, {os.path.dirname(here)!r}); import _lib.{mod}"],
        capture_output=True, text=True)
    check(f"stdlib-only: _lib/{mod}.py imports under -I -S", r.returncode == 0)
    if r.returncode != 0:
        print("      " + (r.stderr.strip().splitlines() or ["?"])[-1])

if FAILS:
    print(f"\n{len(FAILS)} check(s) FAILED")
    sys.exit(1)
print("\nall checks passed")
