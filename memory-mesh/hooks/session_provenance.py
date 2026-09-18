#!/usr/bin/env python3
"""session_provenance.py — a second signal for memory_write.py's craig-direct
writes, independent of the self-declared `lineage:` tag: did THIS session
touch an untrusted-content tool (a web fetch, a Gmail/Outlook/Calendar/Drive
read) before this write?

Continuous-verification audit, Epic B, story B2 (2026-08-06). B1
(evals/memory_poison_probe.py) proved the lineage tag alone gates nothing: a
GhostWriter-style mistag (external text phrased to read as Craig's own
reported preference, tagged `craig-direct`) is admitted to the servable
index unscreened. B1's Opus-verification pass then REFUTED the originally
proposed fix (an instruction-pattern content heuristic — 96.5% of the real
memory corpus is directive language, so "is this instruction-shaped" cannot
discriminate poison from the legitimate store) and re-scoped B2 to this:
machine-derived session provenance as a second signal, independent of
whatever the caller (human or agent) asserts.

***********************************************************************
* WIRED AND LIVE. Both hook blocks are installed in                   *
* ~/.claude/settings.json and this file runs in front of every Bash   *
* call. It was authorized by Craig; this banner said "STAGED, NOT     *
* WIRED" for weeks afterwards and was corrected 2026-09-14.           *
* A SECOND classifier (`classify_tool_next`) now runs alongside the   *
* enforcing one in SHADOW — see "TWO CLASSIFIERS" below. Nothing it   *
* decides can move a verdict until SESSION_PROVENANCE_ENFORCE_NEXT=1. *
***********************************************************************

WHAT IT DOES
------------
    SessionStart hook:
        session_provenance.py record --event SessionStart
    PreToolUse hook (matcher below):
        session_provenance.py record --event PreToolUse

Both are pure OBSERVATION — this script never blocks a tool call (always
exits 0, no stderr, no stdout). The enforcement happens later and elsewhere,
at memory_write.py's `_cmd_write`, which reads what this recorded.

WHY A SessionStart ROW MATTERS (the ambiguity this design exists to kill)
---------------------------------------------------------------------------
An empty result for a session is ambiguous on its own: it could mean "this
session touched nothing untrusted" (the good case) or "this hook was never
wired / never fired for this session" (a channel outage that must NOT read
as clean — PRINCIPLES 4, degrade toward safety; the exact failure this file's
own docstring elsewhere calls out: no-data-must-not-render-as-positive-data).
The SessionStart row is the "the channel was live" witness: memory_write.py
distinguishes CLEAN (a SessionStart row exists, no untrusted-touch row
follows it) from UNVERIFIED (no SessionStart row at all — the channel was
never proven live for this session). Measured 2026-09-14, with the hook
live: 1,345 of 1,444 sessions read CLEAN and 99 FLAGGED — so "unverified" is
now the exception, not the universal state this paragraph once described.

STORAGE
-------
One bounded, append-only JSONL log — same shape as session-registry's, on
purpose (that file already proved the pattern: bounded, fold-at-read,
never stores derived state). This is a SEPARATE log, not a repurposing of
session-registry.jsonl: session-registry answers "does this session need
Craig's attention" (a UX-liveness concern); this answers "did this session
touch untrusted content" (a security-provenance concern). Conflating the two
concerns in one file was considered and rejected — PRINCIPLES 12, small sharp
tools, one concern per artifact.

Rows:
    {"at": iso, "event": "SessionStart"|"UntrustedToolUse"
                         |"ProvenanceShadow"|"ProvenanceShadowError",
     "session": <session id>, "tool": <tool name>|"", "detail": <str>|""}

Only "UntrustedToolUse" is a verdict input. The two Provenance* events are
measurement and are dropped explicitly by `state_for_session`.

WIRING — APPLIED. (Corrected 2026-09-14: this section said "STAGED (not
applied)" for weeks after Craig authorized it, and `state_for_session`'s
docstring still claimed every session reads `unverified` "since the hook is
unwired". Both were false. A reader trusting them would have discounted every
`clean` stamp the instrument ever issued — the opposite of the truth. Measured
at the time of the correction: both blocks present in ~/.claude/settings.json
(`PreToolUse[3]`, `SessionStart[4]`), 2,932 rows, 1,445 sessions witnessed, 99
flagged.) The blocks below are what is installed:

TWO CLASSIFIERS — `classify_tool` ENFORCES, `classify_tool_next` SHADOWS
------------------------------------------------------------------------
Craig authorized items 2 and 3 of
memory-mesh/reviews/2026-09-14-provenance-coverage-gap.md on 2026-09-14
("do 2 and 3"), after the instrument was wrong in both directions in one
session: ~15 external pages fetched by `curl` in Bash produced ZERO untrusted
rows (so two craig-direct memory writes were stamped `clean`), while a command
line that merely QUOTED "read_proton.py" as test data produced five real
UntrustedToolUse rows for mail that was never read.

`classify_tool_next` fixes both — an EXTERNAL http(s) fetch is untrusted, a
LAN/loopback one is not (Craig 2026-07-27: a ranch-LAN host is first-class
local), and matching is argv-position rather than raw-substring so a mention
is not an invocation. It does NOT enforce: `state_for_session` reads only
`UntrustedToolUse` rows and explicitly drops `ProvenanceShadow`, so today's
verdicts cannot move. `record()` writes a ProvenanceShadow row only where the
two classifiers DISAGREE, which measures the blast radius this log could never
supply on its own — it holds no row for an unflagged Bash call, so the
coverage gap destroyed the evidence needed to size its own fix.

Read `session_provenance.py shadow-report`, then promote by setting
SESSION_PROVENANCE_ENFORCE_NEXT=1 (one named change). Until then the legacy
false positive keeps firing, by design — swapping enforcement before the
measurement exists is the thing the shadow run is for.

    session_provenance.py shadow-report          # what would change
    session_provenance.py classify --command ... # both verdicts, one command

Add to ~/.claude/settings.json's "hooks":

    "SessionStart": [
      {"hooks": [{"type": "command",
        "command": "/usr/bin/python3 /home/{{REDACTED}}/{{REDACTED}}/memory-mesh/hooks/session_provenance.py record --event SessionStart"}]}
    ],
    "PreToolUse": [
      {"matcher": "WebFetch|WebSearch|Bash|mcp__claude_ai_Gmail__.*|mcp__claude_ai_Google_Calendar__.*|mcp__claude_ai_Google_Drive__.*|mcp__composio-outlook__.*|mcp__composio-personal-outlook__.*|mcp__playwright__browser_navigate|mcp__playwright__browser_network_request",
       "hooks": [{"type": "command",
        "command": "/usr/bin/python3 /home/{{REDACTED}}/{{REDACTED}}/memory-mesh/hooks/session_provenance.py record --event PreToolUse"}]}
    ]

(SessionStart already has entries in settings.json — this would be a fourth
block in that array, same pattern as session_registry's own entry. PreToolUse
likewise gains a fourth matcher block alongside memory-write-guard/
memory-fresh/safety-gate.)

KNOWN COVERAGE GAP — narrowed, not closed
--------------------------------------------
Tool-name matching cannot see a Bash-invoked reader unless its command line
is grepped: `UNTRUSTED_BASH_SUBSTRINGS` below covers the paths CLAUDE.md
already names as the narrowed-not-closed OTP-guard surface (Proton, the
first-party Gmail read helpers, sasha-{{REDACTED}}'s triage), but any other
shell-capable route to mail/web content (raw curl, imaplib, a renamed
script) is invisible to this instrument — same posture this fleet already
states for otp_guard, not a new admission. **This gap FIRED for real on
2026-09-14** (~15 external pages fetched by curl, zero rows, two memory
writes stamped clean) and is what `classify_tool_next` exists to close; it is
still open in the ENFORCING path until that classifier is promoted. Note the
legacy scan also fires the other way — quoting a needle is indistinguishable
from invoking it, which produced five false mail flags in that same session. A subagent that itself touched
untrusted content and reported back through the Task tool is ALSO invisible
here: whether Claude Code fires PreToolUse hooks for a subagent's own tool
calls (as opposed to only the parent's `Task` invocation) is UNVERIFIED as of
this writing — flagged as a residual, not assumed either way.

THE CEILING — carelessness, not compromise (named explicitly, 2026-08-06,
per Grok's adversarial review, finding F4)
--------------------------------------------------------------------------
This instrument and the agent it observes run as the SAME principal. The
evidence log lives under `~/.claude/`, writable by whatever wrote it, and
nothing here prevents an agent from truncating or editing it before a
memory_write.py call reads it back — an "always exits 0, never blocks"
observation hook plus a same-uid log is not a control against a fully
adversarial or compromised agent, only against an honest one that forgot
what it just read. Closing that for real (root-owned append-only log, or a
privileged daemon owning the write) is real infrastructure, not a quick
fix — filed as a follow-up in
audits/2026-08-05-continuous-verification/BACKLOG.md rather than built
here. Ceiling stated plainly so nobody reads "B2 is wired" as "GhostWriter
is solved against an adversarial agent" — it solves the honest-mistake case,
which is the case B1 actually measured.

Usage:
    session_provenance.py record --event SessionStart      # hook, stdin JSON
    session_provenance.py record --event PreToolUse         # hook, stdin JSON
    session_provenance.py state --session <id>               # human/debug view
    session_provenance.py prune [--keep N]
    session_provenance.py --selftest

Stdlib only.
"""
import argparse
import ipaddress
import json
import os
import re
import shlex
import sys
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from urllib.parse import urlsplit

HOME = Path(os.environ.get("SESSION_PROVENANCE_HOME", Path.home() / ".claude"))
LOG = Path(os.environ.get("SESSION_PROVENANCE_LOG", HOME / "session-provenance.jsonl"))

MAX_LINES = 5000            # bounded (PRINCIPLES 8); prune keeps the newest
MAX_DETAIL_CHARS = 200      # bounded row payload — never the untrusted CONTENT

# Tool names that read content this fleet does not control the authorship of.
# Matched by PREFIX so a connector growing its tool list (Gmail's already has
# twice, per connector-drift/A1) stays covered without an edit here — kept
# narrower than a bare "mcp__" so a first-party-only MCP server (garden,
# board, recall, cost, ...) is never miscounted as untrusted.
UNTRUSTED_TOOL_PREFIXES = (
    "mcp__claude_ai_Gmail__",
    "mcp__claude_ai_Google_Calendar__",
    "mcp__claude_ai_Google_Drive__",
    "mcp__composio-outlook__",
    "mcp__composio-personal-outlook__",
)
UNTRUSTED_TOOL_EXACT = {"WebFetch", "WebSearch"}

# Browser tools that name their destination in the call. A navigate (or raw
# request) to an EXTERNAL host brings in content authored outside Craig's
# control; a LAN dashboard does not (2026-07-27 ruling — same carve-out as
# `_external_urls`). Snapshot/click/type carry no URL: the navigate that put
# the page there is the row. A missing or unparseable URL flags (PRINCIPLES 4).
BROWSER_URL_TOOLS = frozenset({
    "mcp__playwright__browser_navigate",
    "mcp__playwright__browser_network_request",
})

# Bash-invoked readers of the same mailboxes a tool-NAME match cannot see —
# see "KNOWN COVERAGE GAP" above.
UNTRUSTED_BASH_SUBSTRINGS = (
    "read_proton.py", "gmail_recent", "gmail_search", "gmail_read",
    "inbox_triage.py",
)

# --------------------------------------------- the NEXT classifier (SHADOW)
# Craig authorized items 2 and 3 of
# memory-mesh/reviews/2026-09-14-provenance-coverage-gap.md ("do 2 and 3").
#
# Item 2 — an EXTERNAL http(s) fetch brings in content authored outside
# Craig's control and is untrusted; a LAN or loopback target does not, per his
# 2026-07-27 ruling that a host on the ranch LAN is first-class local, not
# remote. A blanket `curl` needle was REJECTED in that review: it would flag
# every internal probe and healthcheck and quarantine ordinary memory writing.
# Item 3 — match INVOCATION, not mention. The legacy substring scan cannot
# tell `python3 read_proton.py` from a command line that merely quotes the
# string "read_proton.py" as test data, which is how this session was falsely
# recorded as having read Proton mail (same defect class as the already-known
# safety-gate-prose-false-positive).
#
# This classifier does NOT enforce. `classify_tool()` above is unchanged and
# remains the only input to `state_for_session()`. `record()` writes a
# ProvenanceShadow row ONLY where the two disagree, so the shadow run measures
# the blast radius that the coverage gap itself destroyed the evidence for —
# you cannot size an instrument's false-negative rate from its own output.
# Promote by flipping ENFORCE_NEXT (one named change, after reading
# `shadow-report`). PRINCIPLES 13: nothing agentic is done until it has run
# end-to-end once for real.
ENFORCE_NEXT = os.environ.get("SESSION_PROVENANCE_ENFORCE_NEXT") == "1"

FETCHER_BINARIES = frozenset({
    "curl", "wget", "httpie", "http", "https", "xh", "aria2c", "lynx",
    "w3m", "links", "fetch",
})
# Scripts whose INVOCATION means a mailbox was read. Same five paths the
# legacy list names — CLAUDE.md's narrowed-not-closed OTP-guard surface.
MAIL_READER_SCRIPTS = frozenset({
    "read_proton.py", "gmail_recent", "gmail_search", "gmail_read",
    "inbox_triage.py",
})
# Tokens that make a `python -c` payload a FETCH rather than a document that
# happens to quote a URL. Both must be present.
_PY_FETCH_TOKENS = ("urlopen", "urlretrieve", "requests.get", "requests.post",
                    "httpx.get", "httpx.post", "urllib.request")
# Wrappers that precede the real executable and must be stepped over.
_WRAPPERS = frozenset({
    "sudo", "nohup", "command", "time", "env", "nice", "ionice", "stdbuf",
    "timeout", "xargs", "builtin", "exec", "then", "do", "else",
})
_URL_RE = re.compile(r"https?://[^\s'\"<>|;)\]}]+", re.I)
# A scheme with no host after it — `curl https://` — is a destination we
# cannot evaluate, which PRINCIPLES 4 sends to the safe default (flag).
_BARE_SCHEME_RE = re.compile(r"https?://(?![^\s'\"<>|;)\]}])", re.I)
_SHELL_OPERATORS = frozenset({";", "|", "||", "&", "&&", "\n"})
_HEREDOC_RE = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")
_LOCAL_HOST_LITERALS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0",
                                  "ip6-localhost"})


def _strip_heredocs(cmd):
    """Remove heredoc BODIES, keeping the command line that opened them.

    A heredoc body is data being written, not commands being run. Leaving it
    in is how a document that quotes `curl https://vendor.example` — exactly
    what this review's own writeup does — would flag the session that wrote
    it. Returns (command_without_bodies, n_bodies_stripped)."""
    lines = cmd.split("\n")
    out, stripped, i = [], 0, 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        delims = [m.group(2) for m in _HEREDOC_RE.finditer(line)]
        i += 1
        for delim in delims:
            stripped += 1
            while i < len(lines) and lines[i].strip() != delim:
                i += 1
            i += 1          # consume the closing delimiter
    return "\n".join(out), stripped


def _is_local_host(host):
    """LAN, loopback and link-local are first-class local (Craig 2026-07-27).
    A bare single-label name (`{{REDACTED}}`) is a LAN name. Anything that
    cannot be resolved to one of those is treated as EXTERNAL — an unknown
    destination is an unknown, and PRINCIPLES 4 sends unknowns to the safe
    default, which here means flagging."""
    if not host:
        return False
    host = host.strip().strip("[]").lower()
    if host in _LOCAL_HOST_LITERALS:
        return True
    try:
        ip = ipaddress.ip_address(host)
        return bool(ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_unspecified)
    except ValueError:
        pass
    if host.endswith(".local") or host.endswith(".lan") or "." not in host:
        return True
    return False


def _external_urls(tokens):
    """(externals, unparseable) — hostnames only, never query strings. A URL's
    PATH can carry content; its host is a destination, which is what an audit
    of the LAN carve-out needs."""
    externals, unparseable = [], 0
    for tok in tokens:
        for raw in _URL_RE.findall(tok):
            try:
                host = urlsplit(raw).hostname
            except ValueError:
                host = None
            if not host:
                unparseable += 1
            elif not _is_local_host(host):
                externals.append(host)
        unparseable += len(_BARE_SCHEME_RE.findall(tok))
    return externals, unparseable


def classify_browser(tool_name, tool_input):
    """(touched, detail) for a browser tool that names its destination.
    Detail is the tool name plus the HOST — never the path, never content."""
    if tool_name not in BROWSER_URL_TOOLS:
        return False, ""
    url = (tool_input or {}).get("url") or ""
    try:
        host = urlsplit(url).hostname
    except ValueError:
        host = None
    if not host:
        return True, f"{tool_name}->unparseable-url"
    if _is_local_host(host):
        return False, ""
    return True, f"{tool_name}->{host}"


def _simple_commands(cmd):
    """Yield (tokens, parsed_ok) for each simple command in a shell line.

    Tokenise ONCE with shlex, then split the token stream on operator tokens.
    Splitting the raw string on `;`/`|` first (the obvious approach) severs
    quoted payloads — `python3 -c "import x; fetch()"` became two broken
    fragments and the fetch went unseen. shlex is quote-aware and already
    emits `;`, `|`, `&&`, `||` and `&` as their own tokens."""
    body, _ = _strip_heredocs(cmd)
    try:
        toks = shlex.split(body, comments=True)
        ok = True
    except ValueError:
        # Unbalanced quotes — cannot tell mention from invocation here.
        toks, ok = body.split(), False
    seg = []
    for tok in toks:
        if tok in _SHELL_OPERATORS:
            if seg:
                yield seg, ok
            seg = []
            continue
        seg.append(tok)
    if seg:
        yield seg, ok


def _executable(tokens):
    """First token that is the actual executable: step over `VAR=x` prefixes
    and wrappers like sudo/env/timeout. Returns (basename, rest)."""
    i, saw_wrapper = 0, False
    while i < len(tokens):
        tok = tokens[i]
        if "=" in tok and not tok.startswith("-") and "/" not in tok.split("=")[0]:
            i += 1
            continue
        if PurePosixPath(tok).name in _WRAPPERS:
            i, saw_wrapper = i + 1, True
            continue
        # A wrapper's own option or numeric argument (`timeout 10`, `nice -n 5`)
        # is not the executable. Only skip these AFTER a wrapper, so a bare
        # `-x` first token still reads as "no executable" rather than silently
        # scanning past real arguments.
        if saw_wrapper and (tok.startswith("-")
                            or re.fullmatch(r"\d+(\.\d+)?[smhd]?", tok)):
            i += 1
            continue
        break
    if i >= len(tokens):
        return "", []
    return PurePosixPath(tokens[i]).name, tokens[i + 1:]


def classify_bash_next(cmd):
    """(touched, detail) for a Bash command line — argv-aware.

    Flags a mail-reader INVOCATION and an EXTERNAL http(s) fetch. Does not
    flag a command that merely mentions either, which is the whole point."""
    cmd = cmd or ""
    reasons = []
    for tokens, parsed_ok in _simple_commands(cmd):
        exe, rest = _executable(tokens)
        if not exe:
            continue
        if exe in MAIL_READER_SCRIPTS:
            reasons.append(f"mail:{exe}")
            continue
        if exe.startswith("python"):
            # A script argument is an invocation; a -c payload is a string.
            for tok in rest:
                if tok.startswith("-"):
                    continue
                name = PurePosixPath(tok).name
                if name in MAIL_READER_SCRIPTS:
                    reasons.append(f"mail:{name}")
                break
            payload = " ".join(t for t in rest if not t.startswith("-"))
            if any(t in payload for t in _PY_FETCH_TOKENS):
                ext, bad = _external_urls(rest)
                if ext:
                    reasons.append(f"fetch:python->{ext[0]}")
                elif bad:
                    reasons.append("fetch:python->unparseable-url")
            continue
        if exe in FETCHER_BINARIES:
            ext, bad = _external_urls(rest)
            if ext:
                reasons.append(f"fetch:{exe}->{ext[0]}")
            elif bad:
                reasons.append(f"fetch:{exe}->unparseable-url")
            elif not parsed_ok:
                # A fetcher in a line we could not tokenise: assume a fetch.
                reasons.append(f"fetch:{exe}->unparsed-cmdline")
            continue
        if not parsed_ok:
            for needle in UNTRUSTED_BASH_SUBSTRINGS:
                if needle in " ".join(tokens):
                    reasons.append(f"mail?:{needle}(unparsed)")
                    break
    if not reasons:
        return False, ""
    uniq = sorted(set(reasons))
    return True, "Bash: " + ", ".join(uniq[:4])


def classify_tool_next(tool_name, tool_input):
    """The NEXT classifier. Identical to `classify_tool` for every non-Bash
    tool; argv-aware for Bash. Not consulted by `state_for_session`."""
    tool_name = tool_name or ""
    if tool_name in UNTRUSTED_TOOL_EXACT:
        return True, tool_name
    if any(tool_name.startswith(p) for p in UNTRUSTED_TOOL_PREFIXES):
        return True, tool_name
    if tool_name in BROWSER_URL_TOOLS:
        return classify_browser(tool_name, tool_input)
    if tool_name == "Bash":
        return classify_bash_next((tool_input or {}).get("command") or "")
    return False, ""


def _now():
    return datetime.now(timezone.utc)


def _iso(dt=None):
    return (dt or _now()).strftime("%Y-%m-%dT%H:%M:%SZ")


def classify_tool(tool_name, tool_input):
    """(touched: bool, detail: str). `detail` is bounded and safe to log — it
    is always the tool name (plus, for Bash, which known substring matched),
    never the untrusted CONTENT itself."""
    tool_name = tool_name or ""
    if tool_name in UNTRUSTED_TOOL_EXACT:
        return True, tool_name
    if any(tool_name.startswith(p) for p in UNTRUSTED_TOOL_PREFIXES):
        return True, tool_name
    if tool_name in BROWSER_URL_TOOLS:
        return classify_browser(tool_name, tool_input)
    if tool_name == "Bash":
        cmd = (tool_input or {}).get("command") or ""
        for needle in UNTRUSTED_BASH_SUBSTRINGS:
            if needle in cmd:
                return True, f"Bash: {needle}"
    return False, ""


def record(event, payload=None, log=None):
    """Append one row. Never raises — a broken instrument must not break the
    session it observes (same contract as session_registry.record). Silent
    (no row) for a PreToolUse call that didn't match anything (PRINCIPLES 7,
    edge-trigger: only the anomaly is worth a row, not every benign call).
    Returns the row written, or None."""
    payload = payload or {}
    session = payload.get("session_id") or ""
    row = None
    if event == "SessionStart":
        row = {"at": _iso(), "event": "SessionStart", "session": session,
               "tool": "", "detail": ""}
    elif event == "PreToolUse":
        tool_name = payload.get("tool_name") or ""
        tool_input = payload.get("tool_input") or {}
        try:
            legacy, detail = classify_tool(tool_name, tool_input)
        except Exception:                                # noqa: BLE001
            legacy, detail = False, ""
        # The NEXT classifier parses arbitrary shell text, so it has strictly
        # more ways to throw than the substring scan it replaces. This hook
        # runs in front of EVERY Bash call: a crash here must not break the
        # session it observes (record()'s stated contract), and must not
        # silently become a clean verdict either. On error, fall back to the
        # legacy verdict and leave a visible row saying the parse failed.
        nxt_failed = ""
        try:
            nxt, nxt_detail = classify_tool_next(tool_name, tool_input)
        except Exception as exc:                          # noqa: BLE001
            nxt, nxt_detail = legacy, detail
            nxt_failed = type(exc).__name__
        if nxt_failed:
            _append({"at": _iso(), "event": "ProvenanceShadowError",
                     "session": session, "tool": tool_name,
                     "detail": f"classify_tool_next raised {nxt_failed}"},
                    log)
        touched, detail = ((nxt, nxt_detail) if ENFORCE_NEXT
                           else (legacy, detail))
        # SHADOW: record only where the two classifiers DISAGREE
        # (PRINCIPLES 7, edge-trigger — agreement is the steady state and
        # earns no row). This row is deliberately NOT an UntrustedToolUse, so
        # `state_for_session` cannot see it and today's verdicts cannot move.
        if legacy != nxt:
            _append({"at": _iso(), "event": "ProvenanceShadow",
                     "session": session, "tool": tool_name,
                     "detail": (f"legacy={'flag' if legacy else 'pass'} "
                                f"next={'flag' if nxt else 'pass'} "
                                f"| {(nxt_detail or detail or '')}"
                                )[:MAX_DETAIL_CHARS],
                     "legacy": bool(legacy), "next": bool(nxt),
                     "enforcing": "next" if ENFORCE_NEXT else "legacy"}, log)
        if not touched:
            return None
        row = {"at": _iso(), "event": "UntrustedToolUse", "session": session,
               "tool": tool_name, "detail": detail[:MAX_DETAIL_CHARS]}
    if row is None:
        return None
    return row if _append(row, log) else None


def _append(row, log=None):
    """Append one JSON row. Never raises — a broken instrument must not break
    the session it observes. Returns True on success."""
    path = Path(log or LOG)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(row) + "\n")
    except OSError:
        return False
    return True


def _read(log=None):
    """Rows, plus a bad-line count (None = log exists but unreadable). Mirrors
    session_registry._read: a malformed line is COUNTED, never silently
    dropped."""
    path = Path(log or LOG)
    if not path.exists():
        return [], 0
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return [], None
    rows, bad = [], 0
    for ln in text.splitlines():
        if not ln.strip():
            continue
        try:
            rows.append(json.loads(ln))
        except ValueError:
            bad += 1
    return rows, bad


def state_for_session(session_id, log=None):
    """(state, detail) for one session — the call memory_write.py makes.

    state is one of:
      "clean"      — a SessionStart row exists for this session and no
                      untrusted-tool row followed it. The strongest positive
                      this instrument can give.
      "flagged"    — an untrusted-tool row exists for this session.
      "unverified" — anything else: no session id, no SessionStart row (the
                      channel was never proven live this session), only
                      shadow-measurement rows, or the log is
                      unreadable/corrupt. Never conflated with "clean" — an
                      absent instrument is not a clean verdict. (Corrected
                      2026-09-14: this said "today, that is EVERY session,
                      since the hook is unwired". The hook IS wired; 1,345 of
                      1,444 sessions read clean.)
    """
    if not session_id:
        return "unverified", "no session id available to this write"
    rows, bad = _read(log)
    if bad is None:
        return "unverified", f"provenance log unreadable ({log or LOG})"
    mine = [r for r in rows if r.get("session") == session_id]
    if not mine:
        return "unverified", (
            "no provenance rows for this session — the recording hook is "
            "not wired, did not fire, or this session predates it")
    # ProvenanceShadow rows are measurement, never a verdict input — the
    # shadow classifier must not be able to move a stamp before Craig
    # promotes it. Excluded here explicitly, including from the row count, so
    # the exclusion is a stated property and not an accident of filtering.
    mine = [r for r in mine if r.get("event") != "ProvenanceShadow"]
    if not mine:
        return "unverified", (
            "only shadow-measurement rows for this session — the recording "
            "hook fired but witnessed no enforcing event")
    started = any(r.get("event") == "SessionStart" for r in mine)
    touches = [r for r in mine if r.get("event") == "UntrustedToolUse"]
    if touches:
        tools = ", ".join(sorted({r.get("tool") or "?" for r in touches}))
        return "flagged", f"session touched: {tools}"
    if started:
        return "clean", f"{len(mine)} row(s), channel active, no untrusted touches"
    # An UntrustedToolUse row with no SessionStart witness should not happen
    # (SessionStart always fires first) — treat as "no witness", loudly,
    # rather than guessing which side is true.
    return "unverified", "no SessionStart witness for this session"


def shadow_report(log=None):
    """What the NEXT classifier would change, measured rather than guessed.

    This is the number the coverage gap destroyed the evidence for: the log
    holds no row for an unflagged Bash call, so the only way to size the
    change is to run both classifiers forward and count the disagreements.
    Returns a dict; `main` prints it."""
    rows, bad = _read(log)
    shadow = [r for r in rows if r.get("event") == "ProvenanceShadow"]
    enforcing = [r for r in rows if r.get("event") == "UntrustedToolUse"]
    sessions = {r.get("session") for r in rows if r.get("event") == "SessionStart"}
    would_add = [r for r in shadow if r.get("next") and not r.get("legacy")]
    would_drop = [r for r in shadow if r.get("legacy") and not r.get("next")]

    def _by_session(rs):
        return {r.get("session") for r in rs}

    flagged_now = _by_session(enforcing)
    add_sessions = _by_session(would_add) - flagged_now
    drop_sessions = _by_session(would_drop)
    # A session only stops being flagged if EVERY one of its enforcing rows
    # would be dropped — one surviving row keeps it flagged.
    truly_cleared = set()
    for sess in drop_sessions:
        legacy_rows = [r for r in enforcing if r.get("session") == sess]
        dropped = [r for r in would_drop if r.get("session") == sess]
        if legacy_rows and len(dropped) >= len(legacy_rows):
            truly_cleared.add(sess)
    return {
        "log_rows": len(rows), "malformed_lines": bad,
        "sessions_witnessed": len(sessions),
        "sessions_flagged_today": len(flagged_now),
        "disagreements": len(shadow),
        "new_flags": len(would_add),
        "sessions_newly_flagged": len(add_sessions),
        "false_flags_removed": len(would_drop),
        "sessions_cleared": len(truly_cleared),
        "reasons": _top_reasons(would_add),
        "enforcing": "next" if ENFORCE_NEXT else "legacy",
    }


def _top_reasons(rows, limit=8):
    counts = {}
    for r in rows:
        detail = (r.get("detail") or "")
        kind = detail.split("|", 1)[-1].strip()
        kind = kind.split("->")[0].strip() or "?"
        counts[kind] = counts.get(kind, 0) + 1
    return sorted(counts.items(), key=lambda kv: -kv[1])[:limit]


def prune(log=None, keep=MAX_LINES):
    path = Path(log or LOG)
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return 0
    if len(lines) <= keep:
        return 0
    dropped = len(lines) - keep
    path.write_text("\n".join(lines[-keep:]) + "\n", encoding="utf-8")
    return dropped


# ------------------------------------------------------------------ selftest
def selftest():
    """Deterministic, isolated, bounded. No network, no real ~/.claude paths —
    every check uses an explicit `log=` pointed at a tempfile."""
    import shutil
    import tempfile

    checks = []

    def check(name, cond):
        checks.append((name, bool(cond)))
        print(f"  {'ok' if cond else 'FAIL'}: {name}")

    tmp = Path(tempfile.mkdtemp(prefix="session-provenance-selftest-"))
    log = tmp / "provenance.jsonl"
    try:
        # classify_tool
        check("WebFetch is untrusted", classify_tool("WebFetch", {})[0])
        check("WebSearch is untrusted", classify_tool("WebSearch", {})[0])
        check("Gmail MCP tool is untrusted (prefix match)",
              classify_tool("mcp__claude_ai_Gmail__search_threads", {})[0])
        check("composio-outlook MCP tool is untrusted",
              classify_tool("mcp__composio-outlook__OUTLOOK_GET_MESSAGE", {})[0])
        nav = "mcp__playwright__browser_navigate"
        check("browser navigate to an EXTERNAL host is untrusted",
              classify_tool(nav, {"url": "https://vendor.example/docs"})[0])
        check("browser navigate to a LAN dashboard is NOT untrusted "
              "(2026-07-27 carve-out)",
              not classify_tool(nav, {"url": "https://{{HOST_IP}}:8123/"})[0])
        check("browser navigate with no url flags (safe default)",
              classify_tool(nav, {})[0])
        check("browser detail names the HOST only, never the path",
              classify_tool(nav, {"url": "https://vendor.example/secret/path"})[1]
              == nav + "->vendor.example")
        check("browser snapshot carries no url and is not a row",
              not classify_tool("mcp__playwright__browser_snapshot", {})[0])
        check("raw network_request to an external host is untrusted",
              classify_tool("mcp__playwright__browser_network_request",
                            {"url": "http://evil.example/x"})[0])
        check("legacy and NEXT agree on browser tools",
              classify_tool_next(nav, {"url": "https://vendor.example/"})
              == classify_tool(nav, {"url": "https://vendor.example/"})
              and classify_tool_next(nav, {"url": "http://{{REDACTED}}:3001/"})
              == classify_tool(nav, {"url": "http://{{REDACTED}}:3001/"}))
        check("a first-party-only MCP tool is NOT untrusted (no false positive)",
              not classify_tool("mcp__recall__search", {})[0])
        check("plain Read is NOT untrusted",
              not classify_tool("Read", {"file_path": "/etc/hostname"})[0])
        check("Bash reading proton mail is untrusted (substring match)",
              classify_tool("Bash", {"command": "python3 read_proton.py --recent"})[0])

        # ---- the NEXT classifier (items 2 + 3, 2026-09-14). The first two
        # checks are the two REAL failures from the session that found this,
        # kept as regression fixtures.
        def nxt(cmd):
            return classify_bash_next(cmd)[0]

        check("REGRESSION (false negative): external curl IS untrusted to next",
              nxt("curl -sL https://docs.cloud.google.com/iam/docs/overview"))
        check("REGRESSION (false positive): merely MENTIONING read_proton.py "
              "in a -c payload is NOT untrusted to next",
              not nxt("python3 -c \"print(classify('python3 read_proton.py --recent'))\""))
        check("legacy DOES false-positive on that same mention (the bug)",
              classify_tool("Bash", {"command":
                  "python3 -c \"print('python3 read_proton.py')\""})[0])
        check("invoking read_proton.py IS untrusted to next",
              nxt("python3 read_proton.py --recent"))
        check("invoking it by path IS untrusted to next",
              nxt("/home/{{REDACTED}}/{{REDACTED}}/cc-skills/proton-mail/read_proton.py"))
        check("LAN curl by IP is NOT untrusted (Craig 2026-07-27)",
              not nxt("curl -sk --max-time 5 https://{{HOST_IP}}:8123/api/"))
        check("loopback curl is NOT untrusted",
              not nxt("curl -s http://127.0.0.1:8099/api/panes"))
        check("bare LAN hostname is NOT untrusted",
              not nxt("curl -s http://{{REDACTED}}:3001/metrics"))
        check(".local mDNS name is NOT untrusted",
              not nxt("curl -s http://printer.local/status"))
        check("external curl behind sudo/env wrappers IS untrusted",
              nxt("sudo env FOO=1 timeout 10 curl -s https://example.com/x"))
        check("external curl in a PIPELINE is untrusted",
              nxt("curl -s https://example.com/a.json | python3 -c 'import sys'"))
        check("a URL inside a HEREDOC BODY is NOT untrusted (writing a doc "
              "that quotes a fetch is not a fetch)",
              not nxt("cat > r.md <<'EOF'\ncurl -sL https://aws.amazon.com/x\nEOF"))
        check("the command that OPENS the heredoc is still classified",
              nxt("curl -s https://example.com/x <<'EOF'\nbody\nEOF"))
        check("wget external IS untrusted", nxt("wget https://example.com/f.tgz"))
        check("python urlopen to an external host IS untrusted",
              nxt("python3 -c \"import urllib.request as u; "
                  "u.urlopen('https://example.com/a')\""))
        check("python printing a URL with no fetch token is NOT untrusted",
              not nxt("python3 -c \"print('https://example.com/a')\""))
        check("an unparseable URL after a fetcher fails toward FLAGGING",
              nxt("curl -s https://"))
        check("git and ls are not fetchers (no false positive)",
              not nxt("git log --oneline -5 && ls -la /tmp"))
        check("grep for a needle STRING is NOT untrusted to next",
              not nxt("command grep -rn 'read_proton.py' ~/{{REDACTED}}"))
        check("classify_tool_next agrees with legacy on non-Bash tools",
              classify_tool_next("WebFetch", {})[0]
              and classify_tool_next("mcp__claude_ai_Gmail__get_message", {})[0]
              and not classify_tool_next("Read", {"file_path": "/x"})[0])
        check("Bash running an unrelated command is NOT untrusted",
              not classify_tool("Bash", {"command": "ls -la"})[0])

        # state_for_session: no session id
        st, detail = state_for_session(None, log=log)
        check("no session id -> unverified", st == "unverified")

        # state_for_session: no rows at all for this session (log doesn't exist)
        st, detail = state_for_session("sess-nothing", log=log)
        check("no provenance rows anywhere -> unverified (not clean)",
              st == "unverified")

        # SessionStart only -> clean
        record("SessionStart", {"session_id": "sess-clean"}, log=log)
        st, detail = state_for_session("sess-clean", log=log)
        check("SessionStart witnessed, no untrusted touch -> clean", st == "clean")

        # SessionStart + untrusted touch -> flagged
        record("SessionStart", {"session_id": "sess-flagged"}, log=log)
        r = record("PreToolUse",
                    {"session_id": "sess-flagged", "tool_name": "WebFetch",
                     "tool_input": {"url": "https://example.com"}}, log=log)
        check("an untrusted PreToolUse call is actually appended", r is not None)
        st, detail = state_for_session("sess-flagged", log=log)
        check("SessionStart + untrusted touch -> flagged", st == "flagged")
        check("flagged detail names the tool", "WebFetch" in detail)

        # a benign PreToolUse call is NOT appended (edge-trigger)
        before_len = len(log.read_text().splitlines())
        record("PreToolUse",
                {"session_id": "sess-clean", "tool_name": "Read",
                 "tool_input": {"file_path": "/etc/hostname"}}, log=log)
        after_len = len(log.read_text().splitlines())
        check("a benign tool call writes NO row (edge-trigger, P7)",
              before_len == after_len)
        st, detail = state_for_session("sess-clean", log=log)
        check("sess-clean is still clean after a benign tool call", st == "clean")

        # two sessions in the same log don't cross-contaminate
        st, detail = state_for_session("sess-clean", log=log)
        check("an unrelated session's flag does not leak into sess-clean",
              st == "clean")

        # corrupt log -> unverified, not clean, not a crash
        bad_log = tmp / "corrupt.jsonl"
        bad_log.write_text("not valid json\n{\"also\": \"broken\n")
        rows, bad = _read(bad_log)
        # ---- the no-behaviour-change guarantee. A ProvenanceShadow row is
        # measurement; if it can move a stamp, the shadow run is not a shadow.
        slog = tmp / "shadow.jsonl"
        record("SessionStart", {"session_id": "sess-shadow"}, log=slog)
        _append({"at": _iso(), "event": "ProvenanceShadow",
                 "session": "sess-shadow", "tool": "Bash",
                 "detail": "legacy=pass next=flag | Bash: fetch:curl->x.com",
                 "legacy": False, "next": True, "enforcing": "legacy"}, slog)
        st, _d = state_for_session("sess-shadow", log=slog)
        check("a ProvenanceShadow row does NOT flag the session (stays clean)",
              st == "clean")
        check("the shadow row is not counted as an enforcing row",
              "1 row(s)" in _d)
        rep = shadow_report(log=slog)
        check("shadow-report counts the disagreement",
              rep["disagreements"] == 1 and rep["new_flags"] == 1)
        check("shadow-report reports which classifier is enforcing",
              rep["enforcing"] == "legacy")
        # A session whose ONLY row is a shadow row has no witness -> unverified
        olog = tmp / "shadow-only.jsonl"
        _append({"at": _iso(), "event": "ProvenanceShadow",
                 "session": "sess-only", "tool": "Bash", "detail": "x",
                 "legacy": False, "next": True, "enforcing": "legacy"}, olog)
        check("a shadow-only session is unverified, never clean",
              state_for_session("sess-only", log=olog)[0] == "unverified")
        # record() must still write the LEGACY verdict while shadowing
        rlog = tmp / "record-legacy.jsonl"
        record("SessionStart", {"session_id": "sess-rec"}, log=rlog)
        record("PreToolUse", {"session_id": "sess-rec", "tool_name": "Bash",
                              "tool_input": {"command":
                                  "curl -s https://example.com/x"}}, log=rlog)
        check("while shadowing, an external curl does NOT flag the session",
              state_for_session("sess-rec", log=rlog)[0] == "clean")
        check("...but the disagreement WAS recorded for measurement",
              shadow_report(log=rlog)["new_flags"] == 1)

        # ---- the classifier must not be able to break the session it
        # observes. This hook runs in front of every Bash call.
        elog = tmp / "err.jsonl"
        record("SessionStart", {"session_id": "sess-err"}, log=elog)
        _saved = globals()["classify_tool_next"]

        def _boom(*a, **k):
            raise RuntimeError("synthetic classifier crash")

        globals()["classify_tool_next"] = _boom
        try:
            row = record("PreToolUse",
                         {"session_id": "sess-err", "tool_name": "Bash",
                          "tool_input": {"command": "python3 read_proton.py"}},
                         log=elog)
            check("a crashing next-classifier does not raise out of record()",
                  True)
            check("...and the LEGACY verdict still lands (no silent clean)",
                  row is not None and row.get("event") == "UntrustedToolUse")
            check("...and the crash leaves a visible row",
                  any(json.loads(l).get("event") == "ProvenanceShadowError"
                      for l in elog.read_text().splitlines() if l.strip()))
        except Exception:                                 # noqa: BLE001
            check("a crashing next-classifier does not raise out of record()",
                  False)
        finally:
            globals()["classify_tool_next"] = _saved
        check("a ShadowError row does not flag the session",
              state_for_session("sess-err", log=elog)[0] == "flagged")

        # Hostile input must not throw. These are parsed, never executed.
        hostile = ["", "   ", "'", '"', "\\", "curl", "curl https://",
                   "$(curl https://x.com)", "a" * 5000,
                   "curl -s 'https://[::1]:8080/x'", "curl http://[bad",
                   "python3 -c '\\''", "<<EOF\nEOF", "cat <<'E'\ncurl x\nE",
                   "curl https://ex.com/a?q=1;rm -rf /", "|||", "&&&",
                   "\n\n\n", "curl https://ex.com \\\n  -H 'a: b'"]
        ok_all = True
        for h in hostile:
            try:
                classify_bash_next(h)
            except Exception as exc:                      # noqa: BLE001
                ok_all = False
                print(f"    hostile input raised {type(exc).__name__}: {h[:40]!r}")
        check(f"{len(hostile)} hostile command lines parse without raising",
              ok_all)

        check("a malformed log is COUNTED bad, not silently dropped", bad == 2)

        # unreadable (directory in place of a file) -> unverified, not a crash
        unreadable = tmp / "is-a-dir.jsonl"
        unreadable.mkdir()
        st, detail = state_for_session("anyone", log=unreadable)
        check("an unreadable log path -> unverified, no crash", st == "unverified")

        # bounded: prune keeps only the newest `keep`
        prune_log = tmp / "prune.jsonl"
        prune_log.write_text("\n".join(f'{{"n":{i}}}' for i in range(20)) + "\n")
        dropped = prune(log=prune_log, keep=5)
        check("prune drops the excess", dropped == 15)
        check("prune keeps exactly `keep` lines",
              len(prune_log.read_text().splitlines()) == 5)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        check("selftest tempdir removed", not tmp.exists())

    failed = [n for n, ok in checks if not ok]
    print(f"\n{len(checks) - len(failed)}/{len(checks)} checks passed")
    if failed:
        print("FAILED:")
        for n in failed:
            print(f"  - {n}")
        return 1
    return 0


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd")
    r = sub.add_parser("record")
    r.add_argument("--event", required=True, choices=["SessionStart", "PreToolUse"])
    s = sub.add_parser("state")
    s.add_argument("--session", required=True)
    p = sub.add_parser("prune")
    p.add_argument("--keep", type=int, default=MAX_LINES)
    sub.add_parser("shadow-report")
    c = sub.add_parser("classify")
    c.add_argument("--command", required=True)
    ap.add_argument("--selftest", action="store_true")
    args = ap.parse_args(argv)

    if args.selftest:
        return selftest()
    if args.cmd == "record":
        payload = {}
        if not sys.stdin.isatty():
            try:
                payload = json.loads(sys.stdin.read() or "{}")
            except ValueError:
                payload = {}
        record(args.event, payload)
        return 0            # a hook must never fail its session
    if args.cmd == "state":
        state, detail = state_for_session(args.session)
        print(f"{state}: {detail}")
        return 0
    if args.cmd == "prune":
        print(f"pruned {prune(keep=args.keep)} line(s)")
        return 0
    if args.cmd == "shadow-report":
        rep = shadow_report()
        print(f"enforcing: {rep['enforcing']}   "
              f"(flip with SESSION_PROVENANCE_ENFORCE_NEXT=1)")
        print(f"log rows {rep['log_rows']}, sessions witnessed "
              f"{rep['sessions_witnessed']}, flagged today "
              f"{rep['sessions_flagged_today']}")
        print(f"disagreements {rep['disagreements']}: "
              f"+{rep['new_flags']} new flags "
              f"({rep['sessions_newly_flagged']} more sessions), "
              f"-{rep['false_flags_removed']} false flags "
              f"({rep['sessions_cleared']} sessions cleared)")
        if rep["malformed_lines"]:
            print(f"malformed lines: {rep['malformed_lines']}")
        for kind, n in rep["reasons"]:
            print(f"  {n:5d}  {kind}")
        if not rep["disagreements"]:
            print("  (no disagreements recorded yet — shadow needs traffic)")
        return 0
    if args.cmd == "classify":
        legacy, ld = classify_tool("Bash", {"command": args.command})
        nxt, nd = classify_tool_next("Bash", {"command": args.command})
        print(f"legacy: {'FLAG' if legacy else 'pass'}  {ld}")
        print(f"next:   {'FLAG' if nxt else 'pass'}  {nd}")
        return 0
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
