#!/usr/bin/env python3
"""Self-test for _lib: one logic check per module, and every module must import
under `python3 -I -S` (stdlib-only). No network, no secrets.

Run: /usr/bin/python3 _lib/selftest.py  (non-zero exit lists failed checks)
"""
import os
import subprocess
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _lib import claude_headless, event_bus, frontmatter, report, secrets  # noqa: E402,F401

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

# --- frontmatter -----------------------------------------------------------
fm_meta, fm_body = frontmatter.parse('---\nname: foo\ntype: feedback\n---\n\nbody text\n')
check("frontmatter.parse: splits meta and body",
      fm_meta == {"name": "foo", "type": "feedback"} and fm_body == "body text")
check("frontmatter.parse: no leading '---' falls back to ({}, text)",
      frontmatter.parse("just text") == ({}, "just text"))

# --- claude_headless --------------------------------------------------------
cmd = claude_headless.build_cmd("hello")
check("claude_headless: every call dials zero MCP servers",
      "--strict-mcp-config" in cmd and '{"mcpServers":{}}' in cmd)
check("claude_headless: every call denies the built-in tools",
      "--disallowed-tools" in " ".join(cmd) or "--disallowedTools" in " ".join(cmd))

# --- stdlib-only invariant --------------------------------------------------
here = os.path.dirname(os.path.abspath(__file__))
for mod in ["secrets", "event_bus", "frontmatter", "report", "claude_headless"]:
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
