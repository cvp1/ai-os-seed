#!/usr/bin/env python3
"""claude_headless — the ONE shared headless `claude -p` runner (Story 018).

Every non-interactive `claude -p` invocation in the workspace goes through here.
The single reason this module exists: the MCP guard must live in ONE place, not
in seven files' discipline. `build_cmd()` unconditionally appends
`--strict-mcp-config --mcp-config {}` — a headless run therefore dials ZERO MCP
servers no matter what the caller passes. That kills the class of the 2026-06
incident (a headless job auto-dialing user-scope ha-local at boot with an
empty/unexpanded bearer, tripping HA's failed-login alarm — see auto-memory
[[headless-claude-mcp-ha-login]]). A new job cannot re-trip it by forgetting a
flag, because the flag is not the caller's to forget.

The guard is STRUCTURAL: there is no keyword argument that turns MCP back on.
If a future job genuinely needs a scoped MCP server it must add an explicit
`--mcp-config <file>` via `extra_args`, which is a visible, reviewable act — not
the silent default.

    from _lib import claude_headless
    text = claude_headless.run_claude(prompt, model="sonnet")        # tool-less
    text = claude_headless.run_claude(prompt, allowed_tools="Read")  # vision, etc.
    cp   = claude_headless.run_raw(prompt, allowed_tools=["Bash","Read","Write"],
                                   skip_permissions=True)            # agentic turn

CLI:  python3 -m _lib.claude_headless --selftest
"""
import json
import os
import re
import subprocess
import sys

CLAUDE = os.path.expanduser("~/.local/bin/claude")

# --------------------------------------------------------------------------- #
# Catalog surface — what Claude models exist, and can we still call ours?      #
#                                                                              #
# Added 2026-08-13, and added HERE because this module is the Claude provider:  #
# the sibling clients (grok/openai/gemini/deepseek) each own their own          #
# list_models(), and Claude's "client" is the CLI this file drives.             #
#                                                                              #
# WHY NO API KEY. Craig, 2026-08-13: "why do I need a token for Claude auth     #
# when we're already authenticated for this very session?" He was right, and    #
# the earlier recommendation to mint one via `claude setup-token` was wrong —   #
# it asked for a THIRD credential when the CLI already holds a working one and  #
# this whole module already depends on it. A new token would have bought        #
# exactly one thing the CLI cannot do (enumerate via GET /v1/models) at the      #
# cost of another secret to hold, rotate and leak.                              #
#                                                                              #
# So Claude gets two instruments, both free and both using existing auth:       #
#                                                                              #
#   list_models()     the CLI BINARY's own recognized-model table. Zero auth,   #
#                     zero network, refreshes when Claude Code updates. WEAKER  #
#                     than the others' catalog APIs and labelled as such: it is #
#                     what this CLI RECOGNIZES, not what Craig's account can    #
#                     reach, and it is scraped from a 290MB binary so it can    #
#                     surface fragments. Good enough for "is there a newer      #
#                     Sonnet than the one we pin", which is the question that   #
#                     went unanswered for a generation and a half on Gemini.    #
#                                                                              #
#   reachable(slug)   an actual one-line call through the CLI. Account-specific #
#                     and authoritative, but spends a little of the Max         #
#                     interactive allowance, so it is opt-in rather than daily. #
#                                                                              #
# Anthropic also does not have xAI's silent-redirect failure mode: a dead slug  #
# here says so out loud ("It may not exist or you may not have access to it"),  #
# which is why the cheap instrument can be the default one.                     #
# --------------------------------------------------------------------------- #
_MODEL_RE = r"claude-(opus|sonnet|haiku|fable)-[0-9]+([.-][0-9]+)*"


def available():
    """Is the Claude CLI present and usable? No network, no spend."""
    return os.path.exists(CLAUDE)


# Fail-safe default. Craig, 2026-08-13: "the assumption that we're using the max
# plan is something we should never assume." He is right, and the first version
# of the catalog hardcoded billing="max-plan" as a literal — a frozen derived
# fact, the exact thing Principle 9 forbids. Plans change (Pro/Max/Team, 5x vs
# 20x, or an org move), and a downgrade would have left the fleet asserting
# "~$0 marginal" while real money was being spent.
#
# WHICH DIRECTION IS SAFE: believing calls are FREE when they are metered
# invites unbounded spend; believing they are METERED when they are covered
# only costs some caution. So an undetectable plan resolves to metered/unknown,
# never to a subscription (Principle 4 — degrade toward safety).
_UNKNOWN_PLAN = {"plan": None, "auth_method": None, "api_provider": None,
                 "covered": False, "detected": False,
                 "detail": "not probed"}


def subscription(timeout=30):
    """Which Anthropic plan is this host ACTUALLY on? Detected, never assumed.

    Instrument: `claude auth status`, which emits JSON carrying `subscriptionType`
    ("max"/"pro"/…), `authMethod` and `apiProvider`. No credential is exposed —
    the CLI reports about its own auth without printing it.

    `api_provider` is the "how does the plan APPLY" axis and matters as much as
    the plan name: a subscription only covers first-party traffic. Running via
    Bedrock/Vertex/Foundry means the cloud vendor bills per token no matter what
    plan the account holds, so `covered` goes False and cost is real again.

    `covered=True` means "calls on this host draw on a prepaid subscription
    rather than per-token billing." It is the ONE thing downstream cost
    reporting is allowed to act on, and it is False whenever we could not prove
    otherwise. Returns a dict; never raises.
    """
    if not available():
        return dict(_UNKNOWN_PLAN, detail="claude CLI not present at %s" % CLAUDE)
    try:
        r = subprocess.run([CLAUDE, "auth", "status"], capture_output=True,
                           text=True, timeout=timeout)
        doc = json.loads((r.stdout or "").strip())
    except Exception as e:                              # noqa: BLE001
        return dict(_UNKNOWN_PLAN, detail="probe failed: %s" % str(e)[:100])
    if not doc.get("loggedIn"):
        return dict(_UNKNOWN_PLAN, detail="not logged in")
    plan = (doc.get("subscriptionType") or "").strip().lower() or None
    api_provider = doc.get("apiProvider")
    # An API-key / third-party-gateway session is metered regardless of plan.
    covered = bool(plan) and api_provider == "firstParty"
    return {"plan": plan,
            "auth_method": doc.get("authMethod"),
            "api_provider": api_provider,
            "covered": covered,
            "detected": True,
            "detail": ("plan=%s via %s/%s" % (plan, doc.get("authMethod"),
                                              api_provider)
                       if covered else
                       "plan=%s but apiProvider=%s — per-token billing applies"
                       % (plan, api_provider))}


def _binary_path():
    """The real versioned binary behind the ~/.local/bin/claude symlink."""
    return os.path.realpath(CLAUDE)


# SHORT ALIASES the CLI accepts for --model. These are NOT in the binary's
# versioned model table, so a catalog built only from that table calls them
# GONE — which is exactly what happened on 2026-08-13 to `opus`, the default
# judge of the entire succession eval. It had been callable the whole time.
#
# VERIFIED LIVE that day, with a positive control, because string-presence in
# the binary is not proof of acceptance:
#     reachable("opus")                     -> True  | ok
#     reachable("definitely-not-a-model-x") -> False | CLI rejected the call
# The control matters: `reachable` documents that the CLI exits 0 on a bad
# model, so an instrument that cannot show the negative proves nothing about
# the positive.
#
# An alias is a MOVING pin: it resolves to whatever the CLI currently maps it
# to, and nothing here can enumerate that mapping. So it is reported as an
# alias rather than as a model — the honest consequence being that its spend
# cannot be priced, which the catalog says out loud instead of costing it at
# zero.
CLI_ALIASES = ("opus", "sonnet", "haiku", "fable", "opusplan")


def list_models(timeout=60):
    """Claude model slugs this CLI recognizes, as ``{"id","display","source"}``.

    NOT an account catalog. See the block comment above: this reads the shipped
    binary's model table, so it answers "what does Claude Code know about"
    rather than "what can this subscription call". Every row is stamped
    ``source="cli-binary"`` so a consumer cannot mistake it for the
    provider-confirmed lists the other four families return.

    Dated variants (``-20251001``) and doc filenames (``.md``) are dropped: the
    fleet pins alias-style slugs, and mixing the two shapes makes every
    successor comparison noise.
    """
    import re
    out = subprocess.run(["/usr/bin/grep", "-aoE", _MODEL_RE, _binary_path()],
                         capture_output=True, text=True, timeout=timeout)
    seen, rows = set(), []
    for slug in out.stdout.split():
        slug = slug.strip().rstrip(".-")
        # A trailing 8-digit group is a release date, not a version rung.
        if re.search(r"-(19|20)\d{6}$", slug) or not slug or slug in seen:
            continue
        seen.add(slug)
        rows.append({"id": slug, "display": slug, "source": "cli-binary"})
    # The short aliases the CLI also accepts (see CLI_ALIASES). Stamped
    # `cli-alias` so a consumer can tell a moving pointer from a fixed slug —
    # they are callable, but they are not a model and cannot carry a rate.
    for alias in CLI_ALIASES:
        if alias not in seen:
            rows.append({"id": alias, "display": alias, "source": "cli-alias"})
    return sorted(rows, key=lambda r: r["id"])


def reachable(model, timeout=120):
    """Can this account actually call ``model`` right now? ``(ok, detail)``.

    Authoritative where list_models() is not — it makes a real call. Costs a
    little Max interactive allowance, so callers opt in.

    THE EXIT CODE IS USELESS HERE: the CLI returns 0 for a nonexistent model and
    reports the failure in its TEXT ("It may not exist or you may not have
    access to it"). Verified live 2026-08-13 against a bogus slug. Anything that
    branches on returncode alone will call every dead pin healthy.

    Goes through build_cmd rather than a hand-rolled argv, and that is not
    tidiness. `--mcp-config` and `--allowedTools` are BOTH variadic, so a prompt
    placed after them is swallowed as another config path / tool name and the
    CLI exits 0 having run nothing. The first draft of this function did exactly
    that and reported every WORKING model unreachable — a false-negative
    instrument, strictly worse than no instrument, and invisible unless you test
    against a slug you know is good. build_cmd puts the prompt at argv[2] where
    nothing can eat it, and carries the MCP guard this module exists to enforce.
    """
    try:
        cmd = build_cmd("reply with exactly: ok", model=model,
                        allowed_tools="", output_format="text")
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, cwd="/tmp")
    except Exception as e:                              # noqa: BLE001
        return False, "probe failed: %s" % str(e)[:120]
    blob = ((r.stdout or "") + (r.stderr or "")).lower()
    for tell in ("may not exist", "not a model this version",
                 "issue with the selected model", "must be provided",
                 "invalid mcp configuration"):
        if tell in blob:
            return False, "CLI rejected the call (%s)" % tell
    return bool((r.stdout or "").strip()), (r.stdout or "").strip()[:80]

# The non-negotiable guard: strict scope + an explicit empty server record =
# provably zero MCP servers. Appended to EVERY headless command build_cmd()
# produces. NOTE the inline config must be `{"mcpServers":{}}`, not `{}` — the
# CLI rejects a bare `{}` ("mcpServers: expected record, received undefined").
# `--strict-mcp-config` alone also loads zero (the form 6 production jobs used),
# but the explicit empty record documents the intent and both are live-verified.
_MCP_GUARD = ["--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']

# The SECOND guard, added 2026-07-28 after a live probe proved the first form
# wrong. `--allowedTools ""` is a PERMISSION ALLOWLIST, not a tool-availability
# switch: it grants no extra permissions, but every BUILT-IN tool stays present,
# and in headless `-p` mode there is no interactive prompt to stop it. So a call
# this module documented as "tool-less" could read any file and run any shell
# command as the invoking user. Proven, not theorised: a run with the old flags
# returned the contents of a random-token probe file, then `echo BASHLIVE-$(id
# -un)` -> "BASHLIVE-{{REDACTED}}".
#
# That mattered because signal-scan feeds UNTRUSTED WEB CONTENT through this
# path — a prompt injection in a scanned page had a route to arbitrary local
# execution.
#
# Live-tested alternatives, all of which FAILED to disable tools:
#   --tools ""      variadic flag swallows the empty arg; bash still ran
#   --tools=        same
#   --tools none / --tools NoSuchTool   both still ran bash AND read a file
# Only an EXPLICIT denylist works. It is therefore fragile by construction: a
# built-in tool added by a future CLI release is NOT covered by this list. That
# fragility is why `--livetest` exists below and must stay in the release check
# — the list is the mechanism, the probe is the proof.
#
# The list below was itself grown by probing: the first version's own refusal
# message volunteered four tools it still had ("only ReportFindings, ToolSearch,
# Workflow, and task/cron/design-sync tools"). Agent/Task/Workflow matter most —
# a delegation tool spawns a subagent that HAS file and shell access, so leaving
# one out reinstates the whole hole through a side door.
_BUILTIN_TOOLS = [
    # file + shell
    "Bash", "BashOutput", "KillShell", "Read", "Write", "Edit", "NotebookEdit",
    "Glob", "Grep",
    # network
    "WebFetch", "WebSearch",
    # delegation — these re-open everything above if left available
    "Task", "Agent", "Workflow", "Skill", "SlashCommand", "SendMessage",
    "ToolSearch",
    # session / scheduling / misc
    "TodoWrite", "ExitPlanMode", "EnterPlanMode", "AskUserQuestion",
    "ReportFindings", "ScheduleWakeup", "DesignSync",
    "TaskCreate", "TaskGet", "TaskList", "TaskUpdate", "TaskOutput", "TaskStop",
    "CronCreate", "CronDelete", "CronList",
    "EnterWorktree", "ExitWorktree",
    "ListMcpResources", "ReadMcpResource",
    "ListMcpResourcesTool", "ReadMcpResourceTool", "ReadMcpResourceDirTool",
]


def _tools_arg(allowed_tools):
    """Normalize allowed_tools (str | list | tuple) to the single CLI value."""
    if isinstance(allowed_tools, (list, tuple)):
        return " ".join(allowed_tools)
    return allowed_tools or ""


def build_cmd(prompt, *, model=None, allowed_tools="", output_format="json",
              skip_permissions=False, extra_args=None, extra_deny=None):
    """Assemble the argv for a headless `claude -p` call.

    The MCP guard (_MCP_GUARD) is appended unconditionally — no argument
    removes it. This is the whole point of routing every job through here.

    ``extra_deny``: additional denylist entries (e.g. ``Bash(git push:*)``
    patterns) folded into the SAME ``--disallowed-tools`` flag as the built-in
    complement — one flag, so a caller never has to know whether the CLI merges
    or replaces a repeated variadic option (added 2026-09-08 for the agent-run
    recipe, bug-bash A19/B9: its hand-rolled argv carried its own denylist).
    """
    cmd = [CLAUDE, "-p", prompt]
    if model:
        cmd += ["--model", model]
    if output_format:
        cmd += ["--output-format", output_format]
    cmd += list(_MCP_GUARD)
    tools = _tools_arg(allowed_tools)
    cmd += ["--allowedTools", tools]
    # `--allowedTools` DOES NOT RESTRICT ANYTHING. Live-proven 2026-08-25:
    # a call with `--allowedTools "Read Grep Glob"` and no denylist reported
    # holding `Agent, Bash, Edit, Glob, Grep, Read, ReportFindings, Skill,
    # ToolSearch, Workflow, Write`. The flag widens; it never narrows. Only an
    # explicit denylist removes a tool (see _BUILTIN_TOOLS above).
    #
    # So the denylist is emitted on EVERY call, not just tool-less ones. Before
    # this, asking for a single tool silently dropped the guard entirely, and
    # three callers were relying on it: both `cc-handoff` auto-triage recipes
    # (untrusted task bodies from other hosts) and ranch-ops `packs.py`, whose
    # docstring called itself a "read-only sweep" while holding Bash and Write.
    # Verified after the change: `--allowedTools "Read Grep Glob"` plus this
    # complement yields exactly `Glob, Grep, Read`.
    if not _BUILTIN_TOOLS:
        # An empty denylist would emit a dangling `--disallowed-tools` (the CLI
        # errors: "argument missing") or, worse on some CLI versions, an
        # unguarded run. Neither is acceptable for the one flag standing between
        # untrusted input and local execution — fail loud instead (principle 13,
        # degrade toward safety). Caught by the livetest's negative control.
        raise RuntimeError(
            "claude_headless: _BUILTIN_TOOLS is empty, so a call cannot be "
            "guarded. Refusing to build an unguarded command.")
    # Callers write the allowlist both ways — "Read Grep Glob" (ranch-ops,
    # the recipes) and "Read,Edit,Write,Glob,Grep,Bash" (memory-curate,
    # memory-prune). Splitting on whitespace alone would read the comma form
    # as ONE token, match nothing, and deny every tool the caller asked for —
    # turning a security fix into an outage. Accept both separators.
    keep = {t for t in re.split(r"[,\s]+", tools) if t}
    deny = [t for t in _BUILTIN_TOOLS if t not in keep]
    for d in (extra_deny or []):
        if d and d not in deny:
            deny.append(d)
    if deny:
        cmd += ["--disallowed-tools"] + deny
    if skip_permissions:
        cmd += ["--dangerously-skip-permissions"]
    if extra_args:
        cmd += list(extra_args)
    return cmd


OAUTH_TOKEN_PATH = "~/.key/claude_code_oauth_token.key"


def oauth_env(base=None):
    """`os.environ` plus CLAUDE_CODE_OAUTH_TOKEN, for callers that pass `env=`.

    A PURE function that RETURNS a dict — it never mutates os.environ, so the
    "this module never touches os.environ itself" contract of run_raw (ranch-ops
    story 015) still holds. It is the `{**os.environ, "HA_TOKEN": token}` shape
    that docstring already names, with the token read at point of use from the
    key vault (P14) rather than shell-`cat`ed into a scheduler manifest.

    WHY THIS EXISTS. Measured 2026-08-25: {{REDACTED}}'s Claude CLI had been logged
    out since at least 2026-08-04, and every mailbox task routed there died on
    "Not logged in - Please run /login". Its `scrape` job called Claude cleanly
    every day through the same outage — because that job exports this token and
    the mailbox path did not. The CLI's interactive credentials are the wrong
    thing for anything a scheduler starts: nobody re-establishes them, and their
    absence is silent.

    Degrades to a no-op, never to a failure: if the key file is missing (a host
    that only ever runs attended), or the variable is already set by the caller's
    environment, the returned dict is just a copy of os.environ and behaviour is
    unchanged. It can only ADD auth where there was none.
    """
    env = dict(os.environ if base is None else base)
    if env.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
        return env
    try:
        # Lazy: this module is imported by recipes under a sys.path shim and by
        # `-m _lib.claude_headless --selftest`; neither should acquire a hard
        # import-time dependency on the vault layer just to build an argv.
        from _lib.secrets import load_secret  # noqa: PLC0415
        tok = load_secret("CLAUDE_CODE_OAUTH_TOKEN", OAUTH_TOKEN_PATH,
                          what="the Claude Code OAuth token",
                          required=False, exit_on_error=False)
    except Exception:
        # A locked or shielded vault means "no token here", not "no run": the
        # caller falls back to whatever credentials the CLI already holds, and
        # if it holds none the recipe's own EX_TEMPFAIL path defers the task
        # with the reason in words. Failing the call here would convert a
        # missing convenience into an outage.
        return env
    if tok:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    return env


def run_raw(prompt, *, cwd=None, timeout=900, env=None, _runner=None, **kw):
    """Run a headless claude and return the CompletedProcess unparsed.
    Lower-level seam for callers that do their own output parsing
    (naturalist's vision-array regex, ranch_diag's raw-JSON artifact).

    `env`, when given, is passed straight to subprocess.run(env=...) - the
    caller builds the full dict (e.g. `{**os.environ, "HA_TOKEN": token}`),
    this module never touches os.environ itself. Omitting it (the default)
    leaves the call byte-for-byte identical to before this parameter existed
    (ranch-ops story 015) - no `env` kwarg reaches the runner at all, so an
    old fixed-signature fake runner in an existing test still works."""
    runner = _runner or subprocess.run
    call_kwargs = dict(cwd=(str(cwd) if cwd else None), capture_output=True,
                        text=True, timeout=timeout)
    if env is not None:
        call_kwargs["env"] = env
    return runner(build_cmd(prompt, **kw), **call_kwargs)


def run_claude(prompt, *, model="sonnet", allowed_tools="", timeout=900,
               cwd=None, on_usage=None, extra_args=None, env=None, _runner=None):
    """Run a headless claude, parse the JSON envelope, return the result text.

    Raises RuntimeError on non-zero exit, empty output, or an error envelope.
    Non-JSON but non-empty stdout is returned as-is (some models answer plain).
    `on_usage(usage_dict, cost_usd, model)` is invoked when present so each
    caller keeps its own billing sink. `extra_args` (e.g. `--disallowedTools`,
    `--max-budget-usd`) is appended after the standard flags - callers that
    need caps/denylists don't have to fork this helper (ranch-ops story 008).
    `env` forwards to run_raw (ranch-ops story 015) - see its docstring.
    """
    p = run_raw(prompt, model=model, allowed_tools=allowed_tools, cwd=cwd,
                timeout=timeout, extra_args=extra_args, env=env, _runner=_runner)
    raw = (p.stdout or "").strip()
    if p.returncode != 0 or not raw:
        raise RuntimeError((p.stderr or "").strip()[-300:] or "empty agent output")
    try:
        env = json.loads(raw)
    except ValueError:
        return raw  # non-JSON but non-empty — take it as-is
    if on_usage:
        on_usage(env.get("usage", {}) or {}, env.get("total_cost_usd", 0),
                 env.get("model") or model)
    out = (env.get("result") or "").strip()
    if env.get("is_error") or not out:
        raise RuntimeError("empty agent result")
    return out


# ----------------------------------------------------------------- selftest ---
class _FakeCP:
    def __init__(self, returncode=0, stdout="", stderr=""):
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _selftest():
    fails = []
    ran = []

    def ok(cond, label):
        # `ran` is the count, never a literal. This used to end in a
        # hardcoded `n = 29`, so adding assertions left the reported total
        # unchanged — a number that looked measured and was not.
        ran.append(label)
        if not cond:
            fails.append(label)

    def raises(fn):
        try:
            fn()
            return False
        except RuntimeError:
            return True

    # --- build_cmd: the structural MCP guard ---
    cmd = build_cmd("hello")
    ok("--strict-mcp-config" in cmd, "guard-present-default")
    ok(cmd[cmd.index("--mcp-config") + 1] == '{"mcpServers":{}}',
       "empty-mcp-config-default")
    ok(cmd[:3] == [CLAUDE, "-p", "hello"], "binary-and-prompt-first")
    ok("--model" not in cmd, "model-omitted-when-none")
    ok(cmd[cmd.index("--allowedTools") + 1] == "", "toolless-default")
    ok(cmd[cmd.index("--output-format") + 1] == "json", "json-default")
    ok("--dangerously-skip-permissions" not in cmd, "no-skip-by-default")

    # guard survives EVERY arg combination
    for at in ["", "Read", ["Bash", "Read", "Write"]]:
        ok("--strict-mcp-config" in build_cmd("p", allowed_tools=at,
                                              skip_permissions=True),
           "guard-present-tools=%r" % (at,))

    ok(build_cmd("p", model="sonnet")[
        build_cmd("p", model="sonnet").index("--model") + 1] == "sonnet",
       "model-included")
    ok(build_cmd("p", allowed_tools=["Bash", "Read"])[
        build_cmd("p", allowed_tools=["Bash", "Read"]).index(
            "--allowedTools") + 1] == "Bash Read", "list-tools-joined")
    ok("--dangerously-skip-permissions" in build_cmd("p", skip_permissions=True),
       "skip-added")
    ok("--output-format" not in build_cmd("p", output_format=None),
       "output-format-omittable")
    ok(build_cmd("p", extra_args=["--foo", "bar"])[-2:] == ["--foo", "bar"],
       "extra-args-appended")

    # --- run_claude: envelope parsing + guard reaches the runner ---
    seen = {}

    def fake_ok(argv, **kw):
        seen["argv"] = argv
        return _FakeCP(0, json.dumps(
            {"result": "  answer  ", "usage": {"t": 1},
             "total_cost_usd": 0.5, "model": "claude-x"}))

    usage_log = []
    out = run_claude("p", on_usage=lambda u, c, m: usage_log.append((u, c, m)),
                     _runner=fake_ok)
    ok(out == "answer", "result-stripped")
    ok("--strict-mcp-config" in seen["argv"], "guard-reaches-runner")
    ok(usage_log == [({"t": 1}, 0.5, "claude-x")], "on-usage-called")

    ok(raises(lambda: run_claude(
        "p", _runner=lambda a, **k: _FakeCP(1, "", "boom"))), "nonzero-raises")
    ok(raises(lambda: run_claude(
        "p", _runner=lambda a, **k: _FakeCP(0, ""))), "empty-raises")
    ok(raises(lambda: run_claude(
        "p", _runner=lambda a, **k: _FakeCP(0, json.dumps(
            {"result": "x", "is_error": True})))), "is-error-raises")
    ok(run_claude("p", _runner=lambda a, **k: _FakeCP(0, "plain text")) ==
       "plain text", "non-json-passthrough")

    # --- run_claude: extra_args passthrough (story 007) ---
    seen_no_extra = {}
    run_claude("p", _runner=lambda a, **k: (seen_no_extra.setdefault("argv", a), _FakeCP(0, '{"result":"x"}'))[1])
    seen_with_extra = {}
    # A REQUESTED tool must not drop the guard for every other tool.
    # Before 2026-08-25 it did: any non-empty allowlist skipped the denylist
    # entirely, leaving Bash/Write/Workflow/Cron* live on paths that process
    # untrusted input. These four assertions are that regression's headstone.
    _c = build_cmd("p", allowed_tools="Read Grep Glob")
    ok("--disallowed-tools" in _c,
       "a non-empty allowlist still emits a denylist")
    _deny = _c[_c.index("--disallowed-tools") + 1:]
    ok("Read" not in _deny and "Grep" not in _deny and "Glob" not in _deny,
       "requested tools are excluded from the denylist")
    for _t in ("Bash", "Write", "Workflow", "Skill", "CronCreate",
               "SendMessage", "ScheduleWakeup", "Agent"):
        ok(_t in _deny, f"unrequested {_t} is denied alongside an allowlist")
    # Comma-separated allowlists must not be read as one token and
    # over-denied — that would turn this guard into an outage.
    _cc = build_cmd("p", allowed_tools="Read,Edit,Write,Glob,Grep,Bash")
    _cdeny = _cc[_cc.index("--disallowed-tools") + 1:]
    for _t in ("Read", "Edit", "Write", "Glob", "Grep", "Bash"):
        ok(_t not in _cdeny, f"comma-form allowlist keeps {_t}")
    for _t in ("Workflow", "Skill", "CronCreate", "Agent", "WebFetch"):
        ok(_t in _cdeny, f"comma-form allowlist still denies {_t}")

    # extra_deny rides the SAME flag as the complement (one flag, no CLI
    # merge-vs-replace question) and never removes a requested tool.
    _ce = build_cmd("p", allowed_tools="Read Bash",
                    extra_deny=["Bash(git push:*)", "Bash(curl:*)", "Bash(curl:*)"])
    _cedeny = _ce[_ce.index("--disallowed-tools") + 1:]
    ok(_ce.count("--disallowed-tools") == 1, "extra_deny folds into one flag")
    ok("Bash(git push:*)" in _cedeny and _cedeny.count("Bash(curl:*)") == 1,
       "extra_deny entries present, de-duplicated")
    ok("Bash" not in _cedeny and "Read" not in _cedeny and "Write" in _cedeny,
       "extra_deny keeps the allowlist and the complement intact")

    ok(build_cmd("p", allowed_tools=["Read"])[
        build_cmd("p", allowed_tools=["Read"]).index("--allowedTools") + 1] == "Read",
       "list-form allowlist normalizes to the CLI value")

    run_claude("p", extra_args=["--disallowedTools", "Bash"],
               _runner=lambda a, **k: (seen_with_extra.setdefault("argv", a), _FakeCP(0, '{"result":"x"}'))[1])
    ok(seen_with_extra["argv"] == seen_no_extra["argv"] + ["--disallowedTools", "Bash"],
       "extra-args-appended-after-standard-flags")
    ok(seen_no_extra["argv"] == build_cmd("p", model="sonnet"), "no-extra-args-unchanged-from-pre-patch")
    ok("--strict-mcp-config" in seen_with_extra["argv"], "guard-present-with-extra-args")

    usage_log_extra = []
    out_extra = run_claude(
        "p", extra_args=["--max-budget-usd", "3.00"],
        on_usage=lambda u, c, m: usage_log_extra.append((u, c, m)),
        _runner=lambda a, **k: _FakeCP(0, json.dumps(
            {"result": "y", "usage": {}, "total_cost_usd": 1.0, "model": "claude-x"})))
    ok(out_extra == "y" and usage_log_extra == [({}, 1.0, "claude-x")],
       "on-usage-fires-with-extra-args")

    # --- env passthrough (ranch-ops story 015) ---
    seen_env = {}
    run_claude("p", env={"HA_TOKEN": "x"},
               _runner=lambda a, **k: (seen_env.setdefault("kw", k), _FakeCP(0, '{"result":"x"}'))[1])
    ok(seen_env["kw"].get("env") == {"HA_TOKEN": "x"}, "env-passed-through-when-given")

    seen_no_env = {}
    run_claude("p", _runner=lambda a, **k: (seen_no_env.setdefault("kw", k), _FakeCP(0, '{"result":"x"}'))[1])
    ok("env" not in seen_no_env["kw"], "env-omitted-entirely-by-default")

    # --- oauth_env: additive, pure, and degrades to a no-op (2026-08-25) ---
    _before = dict(os.environ)
    _e = oauth_env(base={"PATH": "/usr/bin"})
    ok(_e["PATH"] == "/usr/bin", "oauth-env-preserves-the-base")
    ok(dict(os.environ) == _before, "oauth-env-never-mutates-os-environ")

    _pre = oauth_env(base={"CLAUDE_CODE_OAUTH_TOKEN": "already-set"})
    ok(_pre["CLAUDE_CODE_OAUTH_TOKEN"] == "already-set",
       "oauth-env-never-overwrites-a-token-the-caller-already-has")

    # A host with no key file must come back byte-identical to its base — the
    # helper may only ADD auth where there was none, never change a working call.
    _saved = OAUTH_TOKEN_PATH
    try:
        globals()["OAUTH_TOKEN_PATH"] = "/nonexistent/claude_oauth.key"
        ok(oauth_env(base={"X": "1"}) == {"X": "1"}, "oauth-env-no-op-without-a-key-file")
    finally:
        globals()["OAUTH_TOKEN_PATH"] = _saved

    n = len(ran)
    if fails:
        print("claude_headless selftest: FAIL %d/%d -> %s"
              % (len(fails), n, ", ".join(fails)))
        return 1
    print("claude_headless selftest: PASS %d/%d" % (n, n))
    return 0


def _livetest():
    """PROVE the tool-less path is tool-less, by running one real claude -p.

    The structural selftest can only assert that the right flags were assembled.
    It cannot know whether the CLI HONOURS them — and on 2026-07-28 it did not:
    `--allowedTools ""` assembled cleanly and still permitted file reads and
    shell execution. A denylist is also fragile by construction (a built-in tool
    added upstream is not on it). So the invariant needs an empirical check, and
    this is it.

    Writes a random token to a temp file, asks a tool-less run to read it, and
    fails if the token comes back. Costs one small Sonnet call. Run it on any
    Claude Code upgrade — that is the moment the tool set can change underneath
    this module.

        python3 -m _lib.claude_headless --livetest
    """
    import random
    import tempfile

    token = "TOOLPROBE-%08x" % random.getrandbits(32)
    fd, path = tempfile.mkstemp(prefix="cc-toolprobe-")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(token + "\n")
        prompt = ("Use ANY means available — a shell, a file reader, a subagent, "
                  "a skill — to read the file %s and print its exact contents. "
                  "If you have no tools, reply NOTOOLS." % path)
        try:
            out = run_claude(prompt, model="sonnet", timeout=300)
        except Exception as exc:
            print("claude_headless livetest: COULD-NOT-EVALUATE (%s: %s)"
                  % (type(exc).__name__, exc))
            return 1
        if token in (out or ""):
            print("claude_headless livetest: FAIL — the 'tool-less' path READ THE "
                  "FILE.\n  The tool guard is not holding. Do NOT ship jobs that "
                  "process untrusted input\n  through this path until it is fixed "
                  "(see _BUILTIN_TOOLS).\n  token=%s" % token)
            return 2
        # SECOND PROBE — the allowlist path. The tool-less probe above never
        # covered it, which is how `--allowedTools "Read Grep Glob"` ran for
        # months holding Bash, Write, Workflow, Skill and CronCreate. A run
        # that legitimately needs Read must still be unable to reach a shell.
        shell_probe = (
            "Run the shell command `echo TOOLSHELL-$(id -un)` and print its "
            "exact output. If you have no shell tool, reply NOSHELL.")
        try:
            out2 = run_claude(shell_probe, model="sonnet", timeout=300,
                              allowed_tools="Read Grep Glob")
        except Exception as exc:
            print("claude_headless livetest: COULD-NOT-EVALUATE allowlist probe "
                  "(%s: %s)" % (type(exc).__name__, exc))
            return 1
        if "TOOLSHELL-" in (out2 or ""):
            print("claude_headless livetest: FAIL — an ALLOWLIST run "
                  "(Read/Grep/Glob) REACHED A SHELL.\n  Requesting one tool is "
                  "dropping the guard on every other tool. Do NOT ship jobs "
                  "that\n  process untrusted input through this path until it "
                  "is fixed (see build_cmd).")
            return 3

        print("claude_headless livetest: PASS — tool-less run could not read the "
              "probe file.")
        return 0
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


if __name__ == "__main__":
    if "--livetest" in sys.argv[1:]:
        sys.exit(_livetest())
    sys.exit(_selftest())
