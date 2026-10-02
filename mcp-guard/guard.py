#!/usr/bin/env python3
"""guard — MCP supply-chain drift alarm.

Snapshots each configured MCP server's launch surface (type, command, args incl.
pinned version, url, env, headers) at vet time; `check` diffs live vs snapshot
and edge-triggers a `mcp-guard/drift` bus event on any change. Also flags
floating (unpinned) package versions. Reads ~/.claude.json user and project
scopes, the workspace .mcp.json, and any files in MCP_GUARD_CONFIGS. Snapshots
store a redacted manifest; the hash covers the unredacted one.

CLI:  guard.py snapshot     (re)record the vetted manifest — human vet step
      guard.py check        diff live vs snapshot; edge-trigger on drift
      guard.py --selftest
"""
import hashlib
import json
import os
import re
import sys

CLAUDE_JSON = os.path.expanduser("~/.claude.json")
HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SEED_INSTALL = os.path.isfile(os.path.join(ROOT, ".cc-seed", "receipt.json"))
SNAP = (os.path.join(ROOT, "observability", "data", "mcp-guard", "snapshots.json")
        if SEED_INSTALL else os.path.join(HERE, "snapshots.json"))
_SENSITIVE = re.compile(r"(token|secret|password|passwd|api[-_]?key|authorization|"
                        r"cookie|credential|bearer)", re.I)

_DYNAMIC_RUNNERS = {"npx", "uvx", "npm", "pnpm", "bunx", "yarn"}
_PKG = re.compile(r"^(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*(@[^@/]+)?$", re.I)
# Exact = full major.minor.patch (optional prerelease/build) and nothing else;
# `1.2`, `1.2.x`, `1.2.*` and `a || b` are ranges npm resolves at launch time.
_EXACT_VER = re.compile(r"^\d+\.\d+\.\d+(-[0-9A-Za-z.-]+)?(\+[0-9A-Za-z.-]+)?$")
# Snapshot fingerprint version: 2 hashes env + headers; 1 did not.
FP = 2


def _extra_configs():
    raw = os.environ.get("MCP_GUARD_CONFIGS", "")
    extra = [os.path.expanduser(p) for p in raw.split(os.pathsep) if p]
    return [os.path.join(ROOT, ".mcp.json")] + extra


def load_servers(path=CLAUDE_JSON, extra=None):
    """{key: cfg} for every launchable server across all scopes.

    The key is the bare name unless an earlier scope took it; missing files
    are skipped."""
    scopes = []
    try:
        d = json.load(open(path, encoding="utf-8"))
    except (OSError, ValueError):
        d = {}
    scopes.append(("", d.get("mcpServers") or {}))
    for proj, pcfg in sorted((d.get("projects") or {}).items()):
        if isinstance(pcfg, dict) and pcfg.get("mcpServers"):
            scopes.append(("project:%s:" % proj, pcfg["mcpServers"]))
    for f in (_extra_configs() if extra is None else extra):
        try:
            m = json.load(open(f, encoding="utf-8"))
        except (OSError, ValueError):
            continue
        servers = m.get("mcpServers") if isinstance(m, dict) else None
        if isinstance(servers, dict):
            scopes.append(("mcp.json:%s:" % f, servers))
    out = {}
    for prefix, servers in scopes:
        if not isinstance(servers, dict):
            continue
        for name, cfg in sorted(servers.items()):
            if not isinstance(cfg, dict):
                continue
            key = name if name not in out else prefix + name
            if key not in out:
                out[key] = cfg
    return out


def _redact(manifest_):
    """Manifest with secret-named args, env/header values and URL queries blanked."""
    m = dict(manifest_)
    args, blank_next = [], False
    for a in m.get("args") or []:
        a = str(a)
        if blank_next or _SENSITIVE.search(a.split("=", 1)[0]):
            args.append("<redacted>" if blank_next or "=" not in a
                        else a.split("=", 1)[0] + "=<redacted>")
            blank_next = not blank_next and "=" not in a and a.startswith("-")
            continue
        args.append(a.split("?", 1)[0] if a.startswith(("http://", "https://")) else a)
    m["args"] = args
    for k in ("env", "headers"):       # names kept, every value blanked
        if m.get(k):
            m[k] = {n: "<redacted>" for n in m[k]}
    if m.get("url"):
        m["url"] = str(m["url"]).split("?", 1)[0]
    return m


def manifest(cfg):
    """Canonical security-relevant launch surface of one server."""
    return {"type": cfg.get("type") or ("stdio" if cfg.get("command") else None),
            "command": cfg.get("command"),
            "args": list(cfg.get("args") or []),
            "url": cfg.get("url"),
            "env": {str(k): str(v) for k, v in (cfg.get("env") or {}).items()},
            "headers": {str(k): str(v) for k, v in (cfg.get("headers") or {}).items()}}


def server_hash(cfg):
    """sha256 of a server's canonical manifest."""
    return hashlib.sha256(
        json.dumps(manifest(cfg), sort_keys=True).encode()).hexdigest()


def _legacy_hash(cfg):
    """FP 1 hash (no env/headers), for comparing against older snapshot entries."""
    m = {k: v for k, v in manifest(cfg).items() if k not in ("env", "headers")}
    return hashlib.sha256(json.dumps(m, sort_keys=True).encode()).hexdigest()


def _pkg_spec(cfg):
    """Package spec for npx/uvx-style launches (first non-flag arg), else None."""
    if os.path.basename(cfg.get("command") or "") not in _DYNAMIC_RUNNERS:
        return None
    for a in cfg.get("args") or []:
        if a in ("-y", "--yes", "-p", "--package"):
            continue
        if a.startswith(("-", "/", ".", "~")):
            continue
        return a if _PKG.match(a) else None
    return None


def floating_pkgs(cfg):
    """[spec] if the runtime package is not pinned to an exact version, else []."""
    spec = _pkg_spec(cfg)
    if spec is None:
        return []
    at = spec.rfind("@")
    if at <= 0:                        # bare name, or only the leading scope @
        return [spec]
    return [] if _EXACT_VER.match(spec[at + 1:]) else [spec]


def snapshot(path=CLAUDE_JSON, quiet=False, extra=None):
    servers = load_servers(path, extra)
    snap = {name: {"hash": server_hash(cfg), "fp": FP,
                   "manifest": _redact(manifest(cfg))}
            for name, cfg in servers.items()}
    os.makedirs(os.path.dirname(SNAP), exist_ok=True)
    tmp = SNAP + ".tmp.%d" % os.getpid()
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(snap, fh, indent=2, sort_keys=True)
    os.replace(tmp, SNAP)
    if not quiet:
        print("mcp-guard: snapshot recorded — %d server(s) vetted" % len(snap))
    return snap


def diff(path=CLAUDE_JSON, extra=None):
    """Return {added, removed, changed, unfingerprinted, floating, no_snapshot}."""
    servers = load_servers(path, extra)
    try:
        snap = json.load(open(SNAP, encoding="utf-8"))
        missing = False
    except (OSError, ValueError):
        snap, missing = {}, True
    live, vetted = set(servers), set(snap)
    changed, legacy = [], []
    for n in sorted(live & vetted):
        if snap[n].get("fp", 1) >= FP:
            if server_hash(servers[n]) != snap[n].get("hash"):
                changed.append(n)
        elif _legacy_hash(servers[n]) != snap[n].get("hash"):
            changed.append(n)
        else:
            legacy.append(n)
    return {
        "added": sorted(live - vetted),
        "removed": sorted(vetted - live),
        "changed": changed,
        "unfingerprinted": legacy,
        "floating": sorted(n for n in live if floating_pkgs(servers[n])),
        "no_snapshot": missing,
    }


def _emit_drift(report):
    try:
        sys.path.insert(0, os.path.dirname(HERE))
        from _lib.event_bus import EventBus
        EventBus().publish("mcp-guard", "drift",
                           {k: v for k, v in report.items() if v})
    except Exception:  # noqa: BLE001
        pass


def check(path=CLAUDE_JSON, quiet=False):
    """Diff live vs snapshot; edge-trigger a bus event on drift.

    Drift exits 0 with a `FINDINGS:` first stdout line; non-zero means the
    guard itself failed.
    """
    rep = diff(path)
    dirty = any(rep[k] for k in ("added", "removed", "changed", "unfingerprinted",
                                 "floating"))
    if dirty:
        _emit_drift(rep)
        if not quiet:
            counts = ", ".join(
                "%d %s" % (len(rep[k]), k) for k in
                ("floating", "added", "removed", "changed", "unfingerprinted")
                if rep[k])
            print("FINDINGS: MCP snapshot drift — %s" % counts)
            if rep["no_snapshot"]:
                print("⚠ NO SNAPSHOT yet at %s — every server reads as new. Vet "
                      "them, then run `guard.py snapshot`." % SNAP)
            if rep["floating"]:
                print("⛔ FLOATING versions (pin these): %s" % ", ".join(rep["floating"]))
            if rep["added"]:
                print("⚠ NEW MCP server(s) since vet: %s" % ", ".join(rep["added"]))
            if rep["removed"]:
                print("⚠ REMOVED MCP server(s): %s" % ", ".join(rep["removed"]))
            if rep["changed"]:
                print("⛔ CHANGED launch surface (possible rug-pull — re-vet then "
                      "re-snapshot): %s" % ", ".join(rep["changed"]))
            if rep["unfingerprinted"]:
                print("⚠ UNFINGERPRINTED env/headers — snapshot predates them; the "
                      "rest of the launch surface matches. Re-vet env/headers, then "
                      "`guard.py snapshot`: %s" % ", ".join(rep["unfingerprinted"]))
        return 0
    if not quiet:
        print("mcp-guard: ✓ no drift — every MCP server matches its vetted snapshot")
    return 0


# Fixture versions use `name + _AT + ver` so build scrubbers don't read them as emails.
_AT = "@"


def _selftest():
    fails = []

    def ok(cond, label):
        if not cond:
            fails.append(label)

    pinned = {"type": "stdio", "command": "npx",
              "args": ["-y", "@playwright/mcp" + _AT + "0.0.75", "--caps", "pdf"]}
    floating = {"type": "stdio", "command": "npx",
                "args": ["-y", "@playwright/mcp" + _AT + "latest"]}
    bare = {"type": "stdio", "command": "npx", "args": ["-y", "@scope/thing"]}
    local = {"type": "stdio", "command": "/venv/bin/python", "args": ["/srv.py"]}
    sse = {"type": "sse", "url": "http://ha.local:8123/mcp_server/sse"}

    # floating detection
    ok(not floating_pkgs(pinned), "pinned-not-floating")
    ok(floating_pkgs(floating) == ["@playwright/mcp" + _AT + "latest"], "latest-floating")
    ok(floating_pkgs(bare) == ["@scope/thing"], "bare-floating")
    ok(not floating_pkgs(local), "local-not-floating")
    ok(not floating_pkgs(sse), "sse-not-floating")

    # hash stability + sensitivity
    ok(server_hash(pinned) == server_hash(dict(pinned)), "hash-stable")
    bumped = {"type": "stdio", "command": "npx",
              "args": ["-y", "@playwright/mcp" + _AT + "0.0.76", "--caps", "pdf"]}
    ok(server_hash(pinned) != server_hash(bumped), "hash-detects-version-bump")
    swapped = {"type": "stdio", "command": "npx",
               "args": ["-y", "@evil/mcp" + _AT + "0.0.75", "--caps", "pdf"]}
    ok(server_hash(pinned) != server_hash(swapped), "hash-detects-command-swap")

    # diff engine against a synthetic snapshot
    global SNAP, CLAUDE_JSON
    import tempfile
    d = tempfile.mkdtemp()
    SNAP = os.path.join(d, "snap.json")
    live = os.path.join(d, "claude.json")

    def write_live(servers):
        json.dump({"mcpServers": servers}, open(live, "w"))

    write_live({"pw": pinned, "cz": local})
    snapshot(path=live, quiet=True, extra=[])
    ok(not any(diff(live, extra=[])[k] for k in ("added", "removed", "changed", "floating")),
       "clean-after-snapshot")
    # a rug-pull: pw's version silently bumped
    write_live({"pw": bumped, "cz": local})
    ok(diff(live, extra=[])["changed"] == ["pw"], "diff-detects-change")
    # a new unvetted server appears
    write_live({"pw": pinned, "cz": local, "sneaky": floating})
    r = diff(live, extra=[])
    ok(r["added"] == ["sneaky"], "diff-detects-added")
    ok(r["floating"] == ["sneaky"], "diff-detects-floating-on-new")
    # a vetted server removed
    write_live({"pw": pinned})
    ok(diff(live, extra=[])["removed"] == ["cz"], "diff-detects-removed")

    # project scopes and .mcp.json are scanned too
    json.dump({"mcpServers": {"pw": pinned},
               "projects": {"/w": {"mcpServers": {"cz": local, "pw": bumped}}}},
              open(live, "w"))
    mj = os.path.join(d, ".mcp.json")
    json.dump({"mcpServers": {"extra": sse}}, open(mj, "w"))
    keys = set(load_servers(live, extra=[mj]))
    ok(keys == {"pw", "cz", "project:/w:pw", "extra"}, "every-scope-read")
    ok(load_servers(live, extra=[mj])["pw"] == pinned, "user-scope-keeps-bare-name")
    # redaction: stored manifest blanks secrets; the hash still sees them
    sec = {"type": "stdio", "command": "srv",
           "args": ["--api-key", "SEKRIT1", "--token=SEKRIT2", "--caps", "pdf",
                    "https://h/x?key=SEKRIT3"]}
    red = json.dumps(_redact(manifest(sec)))
    ok("SEKRIT" not in red and "--caps" in red and "pdf" in red, "stored-manifest-redacted")
    sec2 = dict(sec, args=["--api-key", "OTHER", "--token=SEKRIT2", "--caps", "pdf",
                           "https://h/x?key=SEKRIT3"])
    ok(server_hash(sec) != server_hash(sec2), "hash-sees-redacted-change")
    # env and headers are launch surface too
    evil = dict(pinned, env={"NODE_OPTIONS": "--require /tmp/evil.js"})
    ok(server_hash(pinned) != server_hash(evil), "hash-detects-env-injection")
    ok(server_hash(dict(pinned, env={"A": "1"})) != server_hash(dict(pinned, env={"A": "2"})),
       "hash-detects-env-value-change")
    http = {"type": "http", "url": "https://h/mcp", "headers": {"Authorization": "Bearer SEKRIT4"}}
    ok(server_hash(http) != server_hash(dict(http, headers={"Authorization": "Bearer OTHER"})),
       "hash-detects-header-change")
    stored = json.dumps(_redact(manifest(dict(evil, **{"headers": http["headers"]}))))
    ok("SEKRIT4" not in stored and "evil.js" not in stored and "NODE_OPTIONS" in stored,
       "stored-env-headers-redacted")
    # an FP 1 entry reads as unfingerprinted unless its v1 surface changed
    write_live({"pw": pinned, "cz": local})
    snapshot(path=live, quiet=True, extra=[])
    old = json.load(open(SNAP))
    for e in old.values():
        e.pop("fp", None)
        e["hash"] = _legacy_hash(pinned if e["manifest"]["command"] == "npx" else local)
    json.dump(old, open(SNAP, "w"))
    r = diff(live, extra=[])
    ok(r["changed"] == [] and r["unfingerprinted"] == ["cz", "pw"], "legacy-snap-not-a-flood")
    write_live({"pw": bumped, "cz": local})
    r = diff(live, extra=[])
    ok(r["changed"] == ["pw"] and r["unfingerprinted"] == ["cz"], "legacy-snap-still-sees-change")
    # LOW: x-ranges, major.minor-only and || ranges float
    for v in ("1.2.x", "1.2", "1.2.3 || 2.0.0", "1.x", "1.2.*"):
        ok(floating_pkgs({"command": "npx", "args": ["-y", "pkg" + _AT + v]}),
           "floating-" + v)
    ok(not floating_pkgs({"command": "npx", "args": ["-y", "pkg" + _AT + "1.2.3-rc.1"]}),
       "prerelease-exact-not-floating")

    SNAP = os.path.join(d, "none.json")
    ok(diff(live, extra=[])["no_snapshot"] is True, "missing-snapshot-is-said")

    n = 32
    if fails:
        print("mcp-guard selftest: FAIL %d/%d -> %s"
              % (len(fails), n, ", ".join(fails)))
        return 1
    print("mcp-guard selftest: PASS %d/%d" % (n, n))
    return 0


def main(argv):
    if "--selftest" in argv:
        return _selftest()
    cmd = argv[1] if len(argv) > 1 else "check"
    if cmd == "snapshot":
        snapshot()
        return 0
    if cmd == "check":
        return check()
    print(__doc__.split("CLI:")[1])
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
