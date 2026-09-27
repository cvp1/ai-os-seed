#!/usr/bin/env python3
"""allowlist — per-job MCP tool allowlists, deny-by-default (Story 031).

A headless job (`claude -p` cron) should see ONLY the MCP servers its task needs;
a job that needs no tools gets none. This is the deny-by-default half of the
supply-chain guard: it shrinks the lethal-trifecta surface (untrusted content +
secrets + egress) per job, so a poisoned tool a job never loads can't fire.

The MECHANISM lives here; ENFORCEMENT is Story 018's shared headless runner,
which turns `servers_for(job)` into `--strict-mcp-config` + a scoped
`--mcp-config`. Until 018 wires it, this is inert, safe data: the map is the
per-job vet ledger.

Deny-by-default: a job NOT in the map gets `[]` — no MCP servers. Add a job here
only after deciding, deliberately, exactly which servers its task requires.

CLI:  allowlist.py <job>       print the allowed servers for a job (one per line)
      allowlist.py --selftest
"""
import sys

# job name -> the MCP server names it may load. Empty list / absent = no tools.
# Seeded conservatively; grow it as headless jobs that genuinely need a tool are
# vetted (the entry IS the vet record). Server names match ~/.claude.json keys.
_ALLOW = {
    # tool-less jobs (the majority) need no entry — they get [] by default.
    # examples of the shape, uncomment + verify before relying on them:
    # "triage":       ["google-connector"],   # read Gmail only
    # "ranch_what_if": ["ranch-twin"],         # the twin, nothing else
}


def servers_for(job):
    """The MCP servers `job` is allowed to load. Deny-by-default: [] if unlisted."""
    return list(_ALLOW.get(job, []))


def strict_mcp_args(job, config_path):
    """Flags a headless runner (Story 018) passes to `claude -p` to pin the MCP
    surface to exactly this job's allowlist. `--strict-mcp-config` means ONLY the
    given config's servers load — nothing from user/global scope leaks in."""
    allowed = servers_for(job)
    if not allowed:
        # a job with no allowlisted servers runs with NO MCP at all
        return ["--strict-mcp-config"]
    return ["--strict-mcp-config", "--mcp-config", config_path]


def _selftest():
    fails = []

    def ok(cond, label):
        if not cond:
            fails.append(label)

    # deny-by-default: an unknown job gets nothing
    ok(servers_for("some-random-cron") == [], "unknown-job-deny")
    ok("--strict-mcp-config" in strict_mcp_args("some-random-cron", "/x.json"),
       "unknown-job-strict")
    ok("--mcp-config" not in strict_mcp_args("some-random-cron", "/x.json"),
       "unknown-job-no-config")

    # a vetted job (temp-inject one) gets exactly its servers, strictly
    _ALLOW["_test_job"] = ["ranch-twin"]
    try:
        ok(servers_for("_test_job") == ["ranch-twin"], "vetted-job-servers")
        args = strict_mcp_args("_test_job", "/scoped.json")
        ok("--strict-mcp-config" in args and "--mcp-config" in args, "vetted-strict")
        # returned list is a copy — callers can't mutate the ledger
        servers_for("_test_job").append("evil")
        ok(_ALLOW["_test_job"] == ["ranch-twin"], "ledger-immutable")
    finally:
        del _ALLOW["_test_job"]

    n = 6
    if fails:
        print("allowlist selftest: FAIL %d/%d -> %s"
              % (len(fails), n, ", ".join(fails)))
        return 1
    print("allowlist selftest: PASS %d/%d" % (n, n))
    return 0


def main(argv):
    if "--selftest" in argv:
        return _selftest()
    if len(argv) > 1:
        for s in servers_for(argv[1]):
            print(s)
        return 0
    print("usage: allowlist.py <job> | --selftest")
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
