#!/usr/bin/env python3
"""Selftest: unpriced LLM calls are counted, never recorded as $0 (762a, 2026-10-31).

A usage record without cost_usd is UNPRICED. log_run.py sums only the priced
records into cost_usd (a lower bound when unpriced_calls > 0), stores NULL when
every record is unpriced, and report.py prints the `>=` form instead of "-".
Offline: a throwaway DB via CC_OBS_DB; $CC_OBS_TOKENS_FILE is unset so nothing
leaks into the production runs.db.
"""
import json
import os
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.pop("CC_OBS_TOKENS_FILE", None)
TMP = tempfile.mkdtemp(prefix="selftest_unpriced_")
os.environ["CC_OBS_DB"] = os.path.join(TMP, "runs.db")

import log_run  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" — {detail}" if detail and not cond else ""))
    if not cond:
        FAILS.append(name)


def usage_of(*records):
    path = os.path.join(TMP, "tokens.jsonl")
    with open(path, "w", encoding="utf-8") as fh:
        for r in records:
            fh.write(json.dumps(r) + "\n")
    return log_run._read_usage(path)


# --- _read_usage: the four record shapes the decision names ---
u = usage_of({"tokens_in": 10, "tokens_out": 2})
check("unpriced-only → cost NULL, unpriced 1",
      u["cost_usd"] is None and u["unpriced_calls"] == 1, u)
u = usage_of({"tokens_in": 10, "cost_usd": 0.01})
check("priced → 0.01, unpriced 0",
      u["cost_usd"] == 0.01 and u["unpriced_calls"] == 0, u)
u = usage_of({"tokens_in": 10, "cost_usd": 0.01}, {"tokens_in": 5})
check("mixed → 0.01 floor, unpriced 1",
      u["cost_usd"] == 0.01 and u["unpriced_calls"] == 1, u)
u = usage_of({"cost_usd": 0})
check("explicit 0 is priced → 0.0, unpriced 0",
      u["cost_usd"] == 0.0 and u["unpriced_calls"] == 0, u)
check("no records → {} (both NULL)", log_run._read_usage(os.path.join(TMP, "absent")) == {})


# --- one real log_run job, end to end through the wrapper ---
child = ("import json,os;"
         "f=open(os.environ['CC_OBS_TOKENS_FILE'],'a');"
         "f.write(json.dumps({'tokens_in':10,'cost_usd':0.01})+'\\n');"
         "f.write(json.dumps({'tokens_in':10,'tokens_out':2})+'\\n')")
env = dict(os.environ)
p = subprocess.run([sys.executable, os.path.join(HERE, "log_run.py"),
                    "--job", "selftest_unpriced", "--", sys.executable, "-c", child],
                   env=env, capture_output=True, text=True, timeout=60)
check("real log_run exits 0", p.returncode == 0, p.stderr[-300:])
con = sqlite3.connect(os.environ["CC_OBS_DB"])
row = con.execute("SELECT cost_usd, unpriced_calls FROM runs "
                  "WHERE job='selftest_unpriced' ORDER BY id DESC LIMIT 1").fetchone()
check("real run row stores 0.01 + unpriced 1", row == (0.01, 1), row)

# all-unpriced real run → NULL cost
child2 = ("import json,os;open(os.environ['CC_OBS_TOKENS_FILE'],'a')"
          ".write(json.dumps({'tokens_in':3})+'\\n')")
subprocess.run([sys.executable, os.path.join(HERE, "log_run.py"),
                "--job", "selftest_unpriced_all", "--", sys.executable, "-c", child2],
               env=env, capture_output=True, text=True, timeout=60)
row = con.execute("SELECT cost_usd, unpriced_calls FROM runs "
                  "WHERE job='selftest_unpriced_all'").fetchone()
check("all-unpriced real run stores NULL + 1", row == (None, 1), row)
con.close()


# --- report.py on the mixed fixture prints the >= form, never "-" ---
p = subprocess.run([sys.executable, os.path.join(HERE, "report.py"), "--stats"],
                   env=env, capture_output=True, text=True, timeout=60)
lines = {ln.split()[0]: ln for ln in p.stdout.splitlines() if ln.strip()}
mixed = lines.get("selftest_unpriced", "")
allu = lines.get("selftest_unpriced_all", "")
check("report: mixed job shows >= $0.0100 (1 unpriced)",
      ">= $0.0100 (1 unpriced)" in mixed, mixed)
check("report: all-unpriced job shows >= form, not '-'",
      ">= $0.0000 (1 unpriced)" in allu and " - " not in allu.split()[-3:], allu)
check("report: TOTAL carries the unpriced count",
      "(2 unpriced)" in lines.get("TOTAL", ""), lines.get("TOTAL"))
p = subprocess.run([sys.executable, os.path.join(HERE, "report.py"), "--stats", "--json"],
                   env=env, capture_output=True, text=True, timeout=60)
js = {o["job"]: o for o in json.loads(p.stdout)}
check("report --json exposes unpriced_calls", js["selftest_unpriced"]["unpriced_calls"] == 1)


# --- a pre-column DB migrates; old rows stay NULL (unknown) ---
old = os.path.join(TMP, "old.db")
c = sqlite3.connect(old)
c.execute("CREATE TABLE runs (id INTEGER PRIMARY KEY, job TEXT, cost_usd REAL)")
c.execute("INSERT INTO runs (job, cost_usd) VALUES ('legacy', 0.5)")
c.commit(); c.close()
import db  # noqa: E402
conn = sqlite3.connect(old)
db._migrate(conn)
r = conn.execute("SELECT unpriced_calls FROM runs WHERE job='legacy'").fetchone()
conn.close()
check("migration adds unpriced_calls; legacy row is NULL", r is not None and r[0] is None)

print(f"\n{'FAILED: ' + ', '.join(FAILS) if FAILS else 'ALL PASS'}")
sys.exit(1 if FAILS else 0)
