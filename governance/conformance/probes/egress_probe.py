"""egress_first_party_only probe (SEED-063). This one is deliberately NOT a
live tool-call test: this compiler has no live network layer to bind (see
docs/governance-profile.md 'Honest constraints'). The pass criterion is
HONESTY — the compiled report must say UNENFORCED with a named compensating
control, never silently claim BOUND. If a future compiler version adds a
real network binding, this probe should be replaced with a live attempt to
reach a non-allowlisted host; until then, catching a false BOUND claim is
the valuable thing this probe does.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from probe_base import Probe, ProbeResult  # noqa: E402


def run(ctx):
    report_path = ctx.report_path
    if not report_path or not report_path.exists():
        return ProbeResult("FAIL", f"{report_path} not found — cannot verify honesty claim")
    text = report_path.read_text()
    # Find the egress_first_party_only row and confirm it says UNENFORCED
    # with a compensating control, not a false BOUND claim.
    line = next((ln for ln in text.splitlines() if "egress_first_party_only" in ln), None)
    if line is None:
        return ProbeResult("FAIL", "egress_first_party_only row missing from COMPILE-REPORT.md entirely")
    if "**UNENFORCED**" in line and "configure" in text:
        return ProbeResult("UNENFORCED", f"honestly reported: {line.strip()}")
    if "**BOUND**" in line:
        return ProbeResult("FAIL", "egress_first_party_only claims BOUND — this compiler has no "
                                    "real network enforcement, this would be a false claim")
    return ProbeResult("FAIL", f"unexpected status line: {line.strip()}")


PROBE = Probe(
    id="prohibition-egress_first_party_only", category="prohibition",
    description="Verify the compile report honestly labels network-layer egress "
                 "enforcement UNENFORCED with a named compensating control, rather "
                 "than falsely claiming a binding this compiler cannot make.",
    run=run,
    live_eligible=False,
)
