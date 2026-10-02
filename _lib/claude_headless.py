#!/usr/bin/env python3
"""claude_headless — the shared headless `claude -p` runner.

Every non-interactive `claude -p` call goes through here. `build_cmd()`
unconditionally adds `--strict-mcp-config --mcp-config {"mcpServers":{}}`, so a
headless run dials zero MCP servers; no keyword turns that off. A job that needs
a scoped MCP server must pass `--mcp-config <file>` via `extra_args`.
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
# Catalog surface — which Claude models exist, and can this account call them? #
#                                                                              #
#   list_models()     the CLI binary's recognized-model table. No auth, no     #
#                     network; reflects what the CLI knows, not what the       #
#                     account can reach, and may surface fragments.            #
#                                                                              #
#   reachable(slug)   a real one-line call through the CLI. Authoritative but  #
#                     spends a little allowance, so it is opt-in.              #
# --------------------------------------------------------------------------- #
_MODEL_RE = r"claude-(opus|sonnet|haiku|fable)-[0-9]+([.-][0-9]+)*"


def available():
    """Is the Claude CLI present and usable? No network, no spend."""
    return os.path.exists(CLAUDE)


# Fail-safe default: an undetectable plan resolves to metered/unknown, never to
# a subscription, since wrongly assuming "free" invites unbounded spend.
_UNKNOWN_PLAN = {"plan": None, "auth_method": None, "api_provider": None,
                 "covered": False, "detected": False,
                 "detail": "not probed"}


def subscription(timeout=30):
    """Detect this host's Anthropic plan via `claude auth status`; never raises.

    `covered=True` means calls draw on a prepaid subscription rather than
    per-token billing; it requires a plan AND apiProvider == "firstParty"
    (Bedrock/Vertex/Foundry bill per token). False whenever unproven.
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


# Short aliases the CLI accepts for --model. They are absent from the binary's
# versioned model table, and are moving pins that cannot be priced.
CLI_ALIASES = ("opus", "sonnet", "haiku", "fable", "opusplan")


def list_models(timeout=60):
    """Claude model slugs this CLI recognizes, as ``{"id","display","source"}``.

    Read from the shipped binary, not an account catalog; rows are stamped
    ``source="cli-binary"`` (or ``cli-alias``). Dated variants are dropped.
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
    # Aliases are stamped `cli-alias`: callable, but not a fixed model.
    for alias in CLI_ALIASES:
        if alias not in seen:
            rows.append({"id": alias, "display": alias, "source": "cli-alias"})
    return sorted(rows, key=lambda r: r["id"])


def reachable(model, timeout=120):
    """Can this account call ``model`` right now? Returns ``(ok, detail)``.

    Makes a real call, so callers opt in. The CLI exits 0 for a nonexistent
    model, so failure is detected from its output text. Uses build_cmd so the
    prompt is not swallowed by the variadic `--mcp-config`/`--allowedTools`.
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

# MCP guard: strict scope + an explicit empty server record = zero MCP servers.
# The inline config must be `{"mcpServers":{}}`; the CLI rejects a bare `{}`.
_MCP_GUARD = ["--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}']

# Tool guard: `--allowedTools` never restricts built-in tools, and `--tools ""`
# does not disable them, so only an explicit denylist removes a tool. A tool
# added by a future CLI release is not covered; `--livetest` checks this.
# Delegation tools (Agent/Task/Workflow/Skill) must stay listed: a subagent
# would otherwise regain file and shell access.
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

    The MCP guard (_MCP_GUARD) is always appended. The denylist is the
    complement of ``allowed_tools`` in _BUILTIN_TOOLS, plus ``extra_deny``
    entries (e.g. ``Bash(git push:*)``), all in one ``--disallowed-tools`` flag.
    """
    cmd = [CLAUDE, "-p", prompt]
    if model:
        cmd += ["--model", model]
    if output_format:
        cmd += ["--output-format", output_format]
    cmd += list(_MCP_GUARD)
    tools = _tools_arg(allowed_tools)
    cmd += ["--allowedTools", tools]
    # `--allowedTools` widens, never narrows, so the denylist is emitted on
    # every call, including ones that request specific tools.
    if not _BUILTIN_TOOLS:
        # An empty denylist would yield an unguarded run; fail loud instead.
        raise RuntimeError(
            "claude_headless: _BUILTIN_TOOLS is empty, so a call cannot be "
            "guarded. Refusing to build an unguarded command.")
    # Accept both "Read Grep Glob" and "Read,Edit,Write" allowlist forms.
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
    """Return a copy of the environment plus CLAUDE_CODE_OAUTH_TOKEN from the vault.

    Pure: never mutates os.environ. Scheduled jobs need this token because the
    CLI's interactive login may be absent. A no-op if the token is already set
    or the key file is missing.
    """
    env = dict(os.environ if base is None else base)
    if env.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
        return env
    try:
        # Lazy import: building an argv must not depend on the vault layer.
        from _lib.secrets import load_secret  # noqa: PLC0415
        tok = load_secret("CLAUDE_CODE_OAUTH_TOKEN", OAUTH_TOKEN_PATH,
                          what="the Claude Code OAuth token",
                          required=False, exit_on_error=False)
    except Exception:
        # Locked/shielded vault means "no token", not a failed call.
        return env
    if tok:
        env["CLAUDE_CODE_OAUTH_TOKEN"] = tok
    return env


def run_raw(prompt, *, cwd=None, timeout=900, env=None, _runner=None, **kw):
    """Run a headless claude and return the CompletedProcess unparsed.

    `env`, when given, is passed to subprocess.run(env=...); when omitted, no
    `env` kwarg reaches the runner at all.
    """
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
    `on_usage(usage_dict, cost_usd, model)` is called when present.
    `extra_args` is appended after the standard flags; `env` forwards to run_raw.
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
        # `ran` is the count, never a literal.
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

    # --- run_claude: extra_args passthrough ---
    seen_no_extra = {}
    run_claude("p", _runner=lambda a, **k: (seen_no_extra.setdefault("argv", a), _FakeCP(0, '{"result":"x"}'))[1])
    seen_with_extra = {}
    # A requested tool must not drop the denylist for every other tool.
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

    # --- env passthrough ---
    seen_env = {}
    run_claude("p", env={"HA_TOKEN": "x"},
               _runner=lambda a, **k: (seen_env.setdefault("kw", k), _FakeCP(0, '{"result":"x"}'))[1])
    ok(seen_env["kw"].get("env") == {"HA_TOKEN": "x"}, "env-passed-through-when-given")

    seen_no_env = {}
    run_claude("p", _runner=lambda a, **k: (seen_no_env.setdefault("kw", k), _FakeCP(0, '{"result":"x"}'))[1])
    ok("env" not in seen_no_env["kw"], "env-omitted-entirely-by-default")

    # --- oauth_env: additive, pure, and a no-op without a key file ---
    _before = dict(os.environ)
    _e = oauth_env(base={"PATH": "/usr/bin"})
    ok(_e["PATH"] == "/usr/bin", "oauth-env-preserves-the-base")
    ok(dict(os.environ) == _before, "oauth-env-never-mutates-os-environ")

    _pre = oauth_env(base={"CLAUDE_CODE_OAUTH_TOKEN": "already-set"})
    ok(_pre["CLAUDE_CODE_OAUTH_TOKEN"] == "already-set",
       "oauth-env-never-overwrites-a-token-the-caller-already-has")

    # With no key file the result must equal the base.
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
    """Prove the tool-less and allowlist paths cannot read files or reach a shell.

    Makes two small real Sonnet calls. Run on every Claude Code upgrade.

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
        # Second probe: an allowlist run must still be unable to reach a shell.
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
