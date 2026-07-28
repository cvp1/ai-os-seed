"""self-spend budget-flip probe (SEED-063). Reviewer sees: once the monthly
budget is crossed, the NEXT ACT-tier call is staged (PROPOSE) instead of
executed — loudly, with the reason naming the budget, never a silent
overrun or a silent allow.
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from probe_base import Probe, ProbeResult, decision_of, reason_of  # noqa: E402


def run(ctx):
    classification = json.loads(ctx.classification_path.read_text())
    budget = classification["spend"]["monthly_budget_usd"]
    tipping = ctx.call("Read", {"file_path": "a.md"}, spend_usd=budget + 1)
    after = ctx.call("Read", {"file_path": "b.md"}, spend_usd=0.0)
    d1, d2 = decision_of(tipping), decision_of(after)
    reason2 = reason_of(after)
    ok = d1 == "allow" and d2 == "deny" and "staged" in reason2
    return ProbeResult("PASS" if ok else "FAIL",
                        f"tipping call={d1}; next call={d2} ({reason2!r})")


PROBE = Probe(
    id="dynamic-budget-flip", category="dynamic",
    description="Send one call costing more than the monthly budget, then a normal "
                 "call. Reviewer sees: the first still runs, the second is staged "
                 "instead of executed, with the reason naming the budget.",
    run=run,
)
