#!/usr/bin/env python3
"""allowlist — per-job MCP server allowlists, deny-by-default.

A headless job loads only the MCP servers listed for it; an unlisted job gets
none. servers_for(job) feeds `--strict-mcp-config` + a scoped `--mcp-config`.

CLI:  allowlist.py <job>       print the allowed servers for a job (one per line)
      allowlist.py --selftest
"""
import sys

# job name -> MCP server names it may load (names match ~/.claude.json keys).
# Absent or empty = no tools. Each entry is the vet record for that job.
_ALLOW = {
    # e.g. "triage": ["google-connector"],
}


def servers_for(job):
    """The MCP servers `job` is allowed to load. Deny-by-default: [] if unlisted."""
    return list(_ALLOW.get(job, []))


def strict_mcp_args(job, config_path):
    """`claude -p` flags that pin the MCP surface to exactly this job's allowlist."""
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
