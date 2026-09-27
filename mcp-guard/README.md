# mcp-guard — the MCP supply-chain drift alarm

An MCP server can change what it does after you approved it (a "rug pull"),
and a floating version (`@latest`) lets it change without you touching
anything. `guard.py` records each server's launch surface — command, args
including the pinned version, URL — and reports ANY later change.

It reads every scope Claude Code launches servers from: the user scope and
each project scope in `~/.claude.json`, plus this workspace's `.mcp.json`
(add more with `MCP_GUARD_CONFIGS`). Secret-named arguments and URL query
strings are blanked in the stored snapshot; the drift hash still covers them.

    python3 mcp-guard/guard.py snapshot    # after you have vetted every server
    python3 mcp-guard/guard.py check       # diff live vs snapshot (exit 0; FINDINGS on drift)
    python3 mcp-guard/guard.py --selftest

The snapshot is a deliberate human step: vet the servers first, then record
them. Until you do, `check` says there is no snapshot rather than trusting
whatever it finds. The snapshot lives in `observability/data/mcp-guard/`.
`allowlist.py` is the deny-by-default map of which MCP servers each headless
job may load — empty until you vet a job's needs.

To schedule it, uncomment the `mcp_guard` example in `scheduler/manifest.yml`
and run `scheduler/sync.sh`.
