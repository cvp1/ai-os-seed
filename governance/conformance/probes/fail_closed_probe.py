"""fail-closed probe (SEED-063). Reviewer sees: a corrupted classification
artifact does not silently allow — the hook denies everything and names the
crash, rather than becoming indistinguishable from "no opinion".
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from probe_base import Probe, ProbeResult, decision_of, reason_of  # noqa: E402


def run(ctx):
    original = ctx.classification_path.read_text()
    try:
        ctx.classification_path.write_text("{not valid json at all")
        result = ctx.call("Read", {"file_path": "a.md"})
        decision, reason = decision_of(result), reason_of(result)
        ok = decision == "deny" and "crashed" in reason
        return ProbeResult("PASS" if ok else "FAIL", f"decision={decision} reason={reason!r}")
    finally:
        ctx.classification_path.write_text(original)


PROBE = Probe(
    id="dynamic-fail-closed", category="dynamic",
    description="Corrupt the compiled classification artifact, then attempt an "
                 "ordinary call. Reviewer sees: denied, naming the crash — never a "
                 "silent allow when the governance data itself is broken.",
    run=run,
    live_eligible=False,  # would require tampering with a live install's compiled output
)
