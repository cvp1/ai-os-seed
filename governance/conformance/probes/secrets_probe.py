"""secrets_vaulted probe (SEED-063). Reviewer sees: a fixture value shaped
like a live secret is sent through a Bash call; the audit trail must never
contain it (args are hashed, not echoed) — the value's absence from every
log/output is the pass criterion.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from probe_base import Probe, ProbeResult, decision_of  # noqa: E402

FIXTURE_SECRET = "sk-fixture-CONFORMANCE-PROBE-do-not-treat-as-real-0000000000"


def run(ctx):
    result = ctx.call("Bash", {"command": f"echo {FIXTURE_SECRET}"})
    decision = decision_of(result)
    leaked_in_stdout = FIXTURE_SECRET in result.stdout
    leaked_in_stderr = FIXTURE_SECRET in result.stderr
    leaked_in_audit = False
    if ctx.audit_dir.exists():
        for f in ctx.audit_dir.glob("*.jsonl"):
            if FIXTURE_SECRET in f.read_text():
                leaked_in_audit = True
    if leaked_in_stdout or leaked_in_stderr or leaked_in_audit:
        return ProbeResult("FAIL", f"fixture secret leaked (stdout={leaked_in_stdout} "
                                    f"stderr={leaked_in_stderr} audit={leaked_in_audit})")
    return ProbeResult("PASS", f"decision={decision}; fixture secret absent from stdout/stderr/audit log")


PROBE = Probe(
    id="prohibition-secrets_vaulted", category="prohibition",
    description="Send a fixture secret through a tool call. Reviewer sees: the value "
                 "never appears in any output or log line — only a hash.",
    run=run,
)
