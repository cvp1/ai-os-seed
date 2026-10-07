#!/usr/bin/env python3
"""contract_test — the six properties that define "the memory works".

The six properties:

    M1  ONE DOOR        — exactly one writer exists, a write goes through it,
                          and a direct store write is refused.
    M2  ONE STORE       — every component resolves the same store path, and
                          no shipped file hardcodes one.
    M3  FOLD PROJECTS   — an emitted event reaches the store as a memory file,
                          an index row, and a servable-manifest entry.
    M4  IT REACHES THE  — the per-turn channel serves that memory back for a
        SESSION           turn that needs it, and the delivery wiring exists.
    M5  RECALL CITES    — the recall surface returns the memory WITH where it
                          lives; an uncited hit is not recall.
    M6  IT REPORTS ITS  — the instruments fail loudly on a dark corpus and a
        OWN BREAKAGE      stale fold, and something outside the fold watches.

Runs against a disposable install (a fake HOME with its own mesh and store),
never the live one. A property that cannot be attempted is SKIP, reported by
name, and is not a pass.

    contract_test.py                     # script half, every property
    contract_test.py --harness claude    # + the harness half of M1/M4/M5
    contract_test.py --only M1 M3
    contract_test.py --json evidence.json

`--harness` takes claude, codex or grok. codex and grok have no hook event,
so their M4 wiring is SKIP; an unverified harness prompt shape is SKIP too.

Stdlib only; targets /usr/bin/python3.
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parent

PROBE_SLUG = "contract-probe-one-door"
PROBE_DESC = "the contract probe memory planted by contract_test.py"
PROBE_RULE = ("Ranch wombat telemetry is read from the zircon feed, never "
              "from the copper feed — the copper feed lags by a day.")
# The turn M4/M5 must serve the probe back for; deliberately not the slug.
PROBE_TURN = "where does ranch wombat telemetry come from, zircon or copper?"

TIMEOUT = 120
HARNESS_TIMEOUT = 240

results = []          # (property, status, detail) — status in PASS/FAIL/SKIP
faults = []           # every injected fault and its restoration, logged


def record(prop, status, detail=""):
    results.append((prop, status, detail))
    tag = {"PASS": "ok", "FAIL": "FAIL", "SKIP": "SKIP"}[status]
    print(f"  {tag}: {prop}" + (f" — {detail}" if detail else ""))


# Ambient variables that could move the store, event log or provenance log
# out from under a sandboxed run; scrubbed from every child environment.
SCRUB_PREFIXES = ("MESH_", "MEMORY_WRITE_", "SESSION_PROVENANCE_")


def scrubbed_environ():
    return {k: v for k, v in os.environ.items()
            if not k.startswith(SCRUB_PREFIXES)}


def _claude_oauth_env():
    """{'CLAUDE_CODE_OAUTH_TOKEN': ...} from _lib.secrets, or {} if already set
    or unavailable. Needed when no interactive login exists (ssh/scheduler)."""
    if os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "").strip():
        return {}
    try:
        sys.path.insert(0, str(WORKSPACE))
        from _lib.secrets import load_secret  # noqa: PLC0415
        tok = load_secret("CLAUDE_CODE_OAUTH_TOKEN", "~/.key/claude_code_oauth_token.key",
                          what="the Claude Code OAuth token",
                          required=False, exit_on_error=False)
    except Exception:  # noqa: BLE001
        return {}
    return {"CLAUDE_CODE_OAUTH_TOKEN": tok} if tok else {}


def run(cmd, env=None, cwd=None, timeout=TIMEOUT, stdin=None):
    e = scrubbed_environ()
    e.update(env or {})
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                          env=e, cwd=cwd, timeout=timeout, input=stdin)


# ── the door ────────────────────────────────────────────────────────────────
# Candidate writer locations. They are counted, not searched in order: more
# than one existing is two doors, which M1 fails.
DOOR_CANDIDATES = (
    HERE / "memory_write.py",
    Path.home() / ".claude" / "skills" / "improve" / "memory_write.py",
)


def doors():
    """Every writer that exists on this host, deduped by real path."""
    seen, out = set(), []
    for c in DOOR_CANDIDATES:
        try:
            real = c.resolve()
        except OSError:
            continue
        if real.is_file() and real not in seen:
            seen.add(real)
            out.append(c)
    return out


class Sandbox:
    """A disposable install — a fake HOME — and the env every probe runs under.

    A fake HOME (not a MESH_ROOT override) moves the mesh root, store_dir()
    and the writer's store together, so paths resolve normally and
    harness_store()'s sandbox guard (which refuses to publish under a
    MESH_ROOT override) stays armed as in production.
    """

    def __init__(self, root):
        self.root = Path(root)
        self.home = self.root / "home"
        self.mesh_root = self.home / "memory-events"
        (self.mesh_root / "events").mkdir(parents=True)
        (self.mesh_root / "events" / ".keep").write_text("")
        # Derived state stays out of history — the single-writer invariant.
        (self.mesh_root / ".gitignore").write_text("views/\nstate/\nview.version\n")
        for cmd in (["git", "init", "-q", str(self.mesh_root)],
                    ["git", "-C", str(self.mesh_root), "config", "user.email", "mesh@test"],
                    ["git", "-C", str(self.mesh_root), "config", "user.name", "contract"],
                    ["git", "-C", str(self.mesh_root), "add", "-A"],
                    ["git", "-C", str(self.mesh_root), "commit", "-qm", "contract seed"]):
            run(cmd, env={"HOME": str(self.home)})
        # The store the harness keys to this workspace, under the fake HOME.
        self.store = (self.home / ".claude" / "projects"
                      / str(WORKSPACE).replace("/", "-") / "memory")
        self.store.mkdir(parents=True)
        # The fold only generates MEMORY.md for a store that has opted in.
        (self.store / ".mesh-generated").write_text("contract_test\n")
        # A sandboxed child's environment must carry no override but the
        # sandbox's own.
        composed = dict(scrubbed_environ(), **self.env)
        leaked = sorted(k for k in composed if k.startswith(SCRUB_PREFIXES)
                        and k not in self.env)
        assert not leaked, (
            f"a sandboxed child would inherit live overrides: {leaked} — "
            f"SCRUB_PREFIXES does not cover them")
        stray = sorted(k for k in self.env if k.startswith(SCRUB_PREFIXES)
                       and k not in ("MESH_HOST", "MESH_SESSION_ID"))
        assert not stray, f"the sandbox itself sets unexpected overrides: {stray}"
        for cmd in (["git", "init", "-q", str(self.store)],
                    ["git", "-C", str(self.store), "config", "user.email", "mesh@test"],
                    ["git", "-C", str(self.store), "config", "user.name", "contract"]):
            run(cmd, env={"HOME": str(self.home)})

    @property
    def env(self):
        # No store override: M2 asserts mesh_lib and memory_write.py derive
        # the same store on their own.
        return {"HOME": str(self.home), "MESH_HOST": "contract",
                "MESH_SESSION_ID": "contract-test"}

    def fold(self):
        return run([sys.executable, str(HERE / "fold.py"), "--project"],
                   env=self.env)


# ── what was tested ─────────────────────────────────────────────────────────
def tested_identity():
    """(tested_root, package_sha) from this install's receipt, or (None, None).

    contract_evidence.record() refuses evidence whose package sha does not
    match the dist being published.
    """
    doc = install_receipt()
    if doc is None:
        return None, None
    return str(HERE.parent), (doc.get("install") or {}).get("package_sha")


def install_receipt():
    """This install's receipt, or None when these bytes are not an install."""
    try:
        return json.loads((HERE.parent / ".cc-seed" / "receipt.json")
                          .read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


# ── M0: what actually ran ───────────────────────────────────────────────────
# Measures whether the bytes under test match what the install wrote.
#
# installed_sha() must stay behaviourally identical to install.py's function
# of the same name (this file cannot import the installer);
# cc-seed/tools/selftest_installed_sha.py asserts they agree. Algorithm:
# sorted relative posix path, then sha256 of each file's bytes.
#
# Prefixes shipped tools write into at runtime; excluded from the hash.
RUNTIME_WRITABLE_PREFIXES = ("observability/data/", "session-brief/briefs/")

INSTALLED_SHA_EXEMPT = {
    "scheduler/manifest.yml",
    # user-editable config (install.py OPERATOR_EDITABLE_CONFIG)
    "observability/freshness.json",
    "memory-mesh/mesh.toml",
}


def installed_sha(target, components, root_files):
    """One hash over every shipped file as it exists in the TARGET."""
    import hashlib
    h = hashlib.sha256()
    paths = []
    for comp in components:
        base = Path(target) / comp
        if not base.is_dir():
            continue
        paths.extend(p for p in base.rglob("*") if p.is_file())
    for f in root_files:
        p = Path(target) / f
        if p.is_file():
            paths.append(p)
    for p in sorted(paths):
        rel = p.relative_to(Path(target)).as_posix()
        # .git and __pycache__ churn independently of shipped bytes.
        if "__pycache__" in p.parts or ".git" in p.parts \
                or rel in INSTALLED_SHA_EXEMPT:
            continue
        if rel.startswith(RUNTIME_WRITABLE_PREFIXES):
            continue
        h.update(rel.encode())
        h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


ROOT_FILES = ["PRINCIPLES.md", "PROPOSALS.md", "CLAUDE.md.template",
              "README.md.template", "VERSION"]


def m0_package_integrity():
    """Do the bytes under test still match what the install wrote?

    Recorded only inside an install (a working copy has no receipt, so no M0
    row and no recordable evidence). Returns the live hash, or None.
    """
    doc = install_receipt()
    if doc is None:
        return None
    root = HERE.parent
    components = (doc.get("install") or {}).get("components", [])
    live = installed_sha(root, components, ROOT_FILES)
    claimed = (doc.get("install") or {}).get("installed_sha")
    if not claimed:
        record("M0-package-integrity", "FAIL",
               "the receipt does not say what this install wrote "
               "(no install.installed_sha) — nothing can be compared, so "
               "these bytes are unproven")
        return live
    if live != claimed:
        record("M0-package-integrity", "FAIL",
               f"the installed tree DRIFTED from its receipt: live "
               f"{live[:16]} != recorded {claimed[:16]} — the bytes running "
               f"here are not the bytes this install wrote")
        return live
    record("M0-package-integrity", "PASS",
           f"the installed tree still matches its receipt ({live[:16]})")
    return live


# ── M1 ──────────────────────────────────────────────────────────────────────
def m1_one_door(sb, harness):
    found = doors()
    if len(found) != 1:
        record("M1-door", "FAIL",
               f"{len(found)} writers exist, expected exactly 1: "
               + ", ".join(str(d) for d in found))
        return None
    door = found[0]
    record("M1-door", "PASS", str(door))

    r = run([sys.executable, str(door), "write",
             "--slug", PROBE_SLUG, "--type", "reference",
             "--description", PROBE_DESC, "--rule", PROBE_RULE,
             "--hook", "contract probe", "--lineage", "operator-direct",
             "--no-push", "--commit"], env=sb.env)
    landed = (sb.store / f"{PROBE_SLUG}.md").is_file()
    record("M1-write", "PASS" if landed else "FAIL",
           str(door.name) if landed else f"rc={r.returncode} {r.stdout[-400:]}{r.stderr[-400:]}")
    if not landed:
        return door

    # The door is only "the one door" if the guard shuts the other paths.
    guard = HERE / "hooks" / "memory-write-guard.py"
    if not guard.is_file():
        record("M1-guard", "FAIL", f"no write guard at {guard}")
        return door
    payload = json.dumps({
        "hook_event_name": "PreToolUse",
        "tool_name": "Write",
        "tool_input": {"file_path": str(sb.store / "bypass-probe.md"),
                       "content": "---\nname: bypass\n---\nwritten around the door\n"},
    })
    g = run([sys.executable, str(guard)], env=sb.env, stdin=payload)
    # rc==2 specifically (the hook "block" code), so a crash is not a denial.
    blocked = g.returncode == 2 and "memory-write-guard" in (g.stdout + g.stderr)
    record("M1-guard", "PASS" if blocked else "FAIL",
           "direct store write refused" if blocked
           else f"guard did not refuse a write around the door "
                f"(rc={g.returncode}) {(g.stdout + g.stderr)[-200:]}")
    m1_bash_guard(sb, guard)
    return door


def _guard_says(guard, sb, command):
    """The guard's decision on a Bash command (never executed)."""
    payload = json.dumps({
        "hook_event_name": "PreToolUse",
        "tool_name": "Bash",
        "tool_input": {"command": command},
    })
    return run([sys.executable, str(guard)], env=sb.env, stdin=payload)


def m1_bash_guard(sb, guard):
    """The guard must refuse shell-level bypasses of the door.

    Checks a healthy baseline first (a plain read is allowed) and that the
    real door still passes, so a deny-everything guard cannot score a pass.
    """
    target = sb.store / "bash-bypass-probe.md"
    healthy = _guard_says(guard, sb, f"grep -c foo {sb.store}/MEMORY.md")
    if healthy.returncode != 0:
        record("M1-bash-guard", "FAIL",
               f"the guard refused a plain READ (rc={healthy.returncode}); "
               "a guard that denies everything proves nothing")
        return
    # An impostor door must be a real file on disk, or the case proves only
    # that an unresolvable path denies.
    impostor_dir = Path(sb.mesh_root).parent / "impostor-door"
    impostor_dir.mkdir(parents=True, exist_ok=True)
    impostor = impostor_dir / "memory_write.py"
    impostor.write_text("#!/usr/bin/env python3\nraise SystemExit('not the door')\n")
    door = Path(guard).resolve().parent.parent / "memory_write.py"

    bypasses = [
        (f"printf poison > {target}  # memory_write.py",
         "a shell write wearing the writer's name in a comment"),
        (f"python3 {impostor} write --commit > {target}",
         "an impostor memory_write.py that is not this install's door"),
        (f"python3 -c \"import pathlib; pathlib.Path('{target}')"
         ".write_text('poison')\"",
         "inline code in a runtime writing the store with no redirect"),
    ]
    for cmd, what in bypasses:
        r = _guard_says(guard, sb, cmd)
        if not (r.returncode == 2 and
                "memory-write-guard" in (r.stdout + r.stderr)):
            record("M1-bash-guard", "FAIL",
                   f"ALLOWED (rc={r.returncode}): {what}")
            return
    # The real door must still get through.
    genuine = _guard_says(
        guard, sb,
        f"/usr/bin/python3 {door} write --slug x --text 'MEMORY.md' --commit")
    if genuine.returncode != 0:
        record("M1-bash-guard", "FAIL",
               f"the guard refused the REAL door (rc={genuine.returncode}); "
               "a guard that denies the writer too proves nothing")
        return
    record("M1-bash-guard", "PASS",
           "the comment bypass, an impostor door and inline runtime code are "
           "all refused; the install's own door is not")


def m1_harness(sb, door, harness):
    """A harness turn, given only a prompt, must write through the door.

    Runs under the real HOME (for CLI auth) with the store and event log
    overridden to the sandbox; asserts the sandbox store received the file.
    """
    spec = harness_spec(harness)
    if spec is None:
        record(f"M1-harness-{harness or 'none'}", "SKIP", harness_skip_reason(harness))
        return
    slug = f"contract-probe-harness-{harness}"
    prompt = (
        "Record one durable lesson in auto-memory. The workspace's memory "
        f"door is {door} and it is the only way in — never emit.py, never a "
        "hand-written file. Run exactly this, then stop:\n"
        f"/usr/bin/python3 {door} write --slug {slug} --type reference "
        f"--description 'the {harness} harness-turn contract probe' "
        f"--rule 'The contract probe records that the {harness} harness "
        "reached the door.' --hook 'harness probe' --lineage operator-direct "
        "--no-push --commit")
    env = {"MEMORY_WRITE_STORE": str(sb.store), "MESH_ROOT": str(sb.mesh_root),
           "MESH_HOST": "contract", "MESH_SESSION_ID": f"contract-{harness}"}
    if harness == "claude":
        env.update(_claude_oauth_env())
    before = set(p.name for p in sb.store.glob("*.md"))
    try:
        r = run(spec["argv"](prompt), env=env, cwd=str(WORKSPACE),
                timeout=HARNESS_TIMEOUT)
    except subprocess.TimeoutExpired:
        record(f"M1-harness-{harness}", "FAIL",
               f"the {harness} turn timed out at {HARNESS_TIMEOUT}s")
        return
    new_files = set(p.name for p in sb.store.glob("*.md")) - before
    # A door write leaves an event naming the slug; a bypass leaves none.
    log = sb.mesh_root / "events" / "contract.ndjson"
    evented = log.is_file() and slug in log.read_text(encoding="utf-8", errors="replace")
    ok = f"{slug}.md" in new_files and evented
    record(f"M1-harness-{harness}", "PASS" if ok else "FAIL",
           f"wrote {sorted(new_files) or 'nothing'}, door-stamped event={evented}"
           + ("" if ok else f" (rc={r.returncode}) {(r.stdout + r.stderr)[-400:]}"))


# ── M2 ──────────────────────────────────────────────────────────────────────
def m2_one_store(sb, door):
    probe = ("import sys; sys.path.insert(0, %r); import mesh_lib as M; "
             "print(M.store_dir())" % str(HERE))
    a = run([sys.executable, "-c", probe], env=sb.env)
    mesh_store = a.stdout.strip()
    b = run([sys.executable, "-c",
             "import importlib.util,sys;"
             "spec=importlib.util.spec_from_file_location('mw', %r);"
             "m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m);"
             "print(m.STORE)" % str(door)], env=sb.env)
    writer_store = b.stdout.strip()
    agree = mesh_store and mesh_store == writer_store == str(sb.store)
    record("M2-resolve", "PASS" if agree else "FAIL",
           f"mesh_lib={mesh_store} writer={writer_store} expected={sb.store}")

    # No shipped source may hardcode an absolute store or door path. Needles
    # are composed so this file does not itself contain the literal.
    literals = (str(Path.home() / ".claude" / "projects"), "/Users/",
                "/".join(("~/.claude", "skills", "improve", "memory_write.py")))
    hits = []
    for f in sorted(list(HERE.glob("*.py")) + list((HERE / "hooks").glob("*.py"))):
        if f.name == Path(__file__).name:
            continue
        text = f.read_text(encoding="utf-8", errors="replace")
        for n, line in enumerate(text.splitlines(), 1):
            s = line.strip()
            if s.startswith("#") or s.startswith('"') or s.startswith("'"):
                continue          # prose about the path is not a use of it
            for lit in literals:
                if lit in line:
                    hits.append(f"{f.name}:{n}")
    record("M2-no-literals", "PASS" if not hits else "FAIL",
           "none" if not hits else ", ".join(hits))


# ── M3 ──────────────────────────────────────────────────────────────────────
def m3_fold_projects(sb):
    r = sb.fold()
    memory = sb.store / f"{PROBE_SLUG}.md"
    index = sb.store / "MEMORY.md"
    exclude = sb.store / "_index-exclude.txt"
    # "Projected" means the index decided about it: a row in MEMORY.md or a
    # line in the on-demand list.
    decided = ((index.is_file() and PROBE_SLUG in index.read_text(encoding="utf-8"))
               or (exclude.is_file() and PROBE_SLUG in exclude.read_text(encoding="utf-8")))
    # The servable manifest lives in mesh state, not the store.
    manifest = sb.mesh_root / "state" / "servable.json"
    servable = False
    if manifest.is_file():
        try:
            servable = PROBE_SLUG in (json.loads(manifest.read_text()).get("slugs") or [])
        except Exception:
            servable = False
    ok = memory.is_file() and index.is_file() and decided and servable
    detail = (f"file={memory.is_file()} index={index.is_file()} "
              f"decided={decided} servable={servable}")
    if not ok:
        record("M3-fold", "FAIL",
               detail + f" rc={r.returncode} {r.stdout[-300:]}{r.stderr[-300:]}")
        return False

    # Delete the file and require the fold to re-project it from the event
    # log; M1 already wrote it, so presence alone proves nothing.
    faults.append("deleted the probe's store file to force a re-projection")
    memory.unlink()
    r2 = sb.fold()
    reprojected = memory.is_file()
    faults.append("the fold re-projected it from the event log"
                  if reprojected else "the fold did NOT re-project it")
    record("M3-fold", "PASS" if reprojected else "FAIL",
           detail + "; re-projected from the log after deletion"
           if reprojected else
           detail + f"; the fold could NOT rebuild {memory.name} from the "
                    f"event log (rc={r2.returncode}) "
                    f"{r2.stdout[-300:]}{r2.stderr[-300:]}")
    return reprojected


# ── M4 ──────────────────────────────────────────────────────────────────────
def _retrieve_hook_wired(settings_path):
    """Is this install's retrieve.py a parsed UserPromptSubmit hook in that
    settings file? disableAllHooks disqualifies the whole file."""
    if not settings_path.is_file():
        return False
    try:
        doc = json.loads(settings_path.read_text(encoding="utf-8") or "{}")
    except ValueError:
        return False
    if not isinstance(doc, dict) or doc.get("disableAllHooks") is True:
        return False
    want = str(HERE / "retrieve.py")
    for entry in (doc.get("hooks") or {}).get("UserPromptSubmit") or []:
        if not isinstance(entry, dict):
            continue
        for h in entry.get("hooks") or []:
            if isinstance(h, dict) and _command_runs(h.get("command") or "", want):
                return True
    return False


def _command_runs(command, want):
    """Does this hook command run `want` as its program (not merely mention it)?

    The program is argv0, or the first non-flag argument after a python
    interpreter, skipping leading VAR=val and `env`. Compared literally and by
    realpath.
    """
    import shlex
    try:
        argv = shlex.split(command)
    except ValueError:
        return False
    k = 0
    while k < len(argv) and (argv[k] == "env" or
                             re.match(r"^[A-Za-z_][A-Za-z0-9_]*=", argv[k])):
        k += 1
    if k >= len(argv):
        return False
    prog = argv[k]
    if re.match(r"^(?:python|python[0-9.]*|pypy[0-9]*)$", os.path.basename(prog)):
        k += 1
        while k < len(argv) and argv[k].startswith("-"):
            if argv[k] in ("-c", "-m"):
                return False          # inline code / a module, not our script
            k += 1
        if k >= len(argv):
            return False
        prog = argv[k]
    if prog == want:
        return True
    try:
        return os.path.realpath(prog) == os.path.realpath(want)
    except OSError:
        return False


def m4_reaches_session(sb, harness):
    payload = json.dumps({"hook_event_name": "UserPromptSubmit",
                          "prompt": PROBE_TURN})
    r = run([sys.executable, str(HERE / "retrieve.py")], env=sb.env, stdin=payload)
    served = PROBE_SLUG in r.stdout
    record("M4-serve", "PASS" if served else "FAIL",
           "the probe memory was served for a turn that never named it"
           if served else f"not served: {r.stdout[-200:]}{r.stderr[-200:]}")

    # Delivery wiring, per harness: only Claude Code has a hook event.
    if harness in (None, "claude"):
        # This install's settings first, then user scope; parsed, not grepped.
        local = WORKSPACE / ".claude" / "settings.json"
        user = Path.home() / ".claude" / "settings.json"
        hit = next((p for p in (local, user)
                    if _retrieve_hook_wired(p)), None)
        if hit is None:
            # Another install's retrieve.py does not count.
            looked = f"{local}, {user}"
            record("M4-wiring", "FAIL",
                   f"no UserPromptSubmit hook in {looked} runs "
                   f"{HERE}/retrieve.py — run install.py --approve memory-hooks")
        else:
            record("M4-wiring", "PASS", f"{HERE.name}/retrieve.py wired as a "
                                        f"UserPromptSubmit hook in {hit}")
    else:
        record("M4-wiring", "SKIP",
               f"{harness} has no pre-prompt hook event — accepted exception "
               "(RESOLUTION-v2 'Harness scope'); its memory is the always-on "
               "MEMORY.md/AGENTS.md layer, not per-turn retrieval")


# ── M5 ──────────────────────────────────────────────────────────────────────
def m5_recall_cites(sb):
    recall = HERE / "recall.py"
    if recall.is_file():
        r = run([sys.executable, str(recall), PROBE_TURN], env=sb.env)
        out = r.stdout
        surface = "recall.py"
    else:
        # Fallback recall surface when recall.py is absent; named in detail.
        r = run([sys.executable, str(HERE / "retrieve.py")], env=sb.env,
                stdin=json.dumps({"prompt": PROBE_TURN}))
        out = r.stdout
        surface = "retrieve.py (no recall.py CLI yet — Step 4b)"
    hit = PROBE_SLUG in out
    # A citation must appear in the output and resolve: the wiki-link form or
    # the store path.
    body = sb.store / f"{PROBE_SLUG}.md"
    citation = next((c for c in (f"[[{PROBE_SLUG}]]", str(body), body.name)
                     if c in out), None)
    cited = hit and citation is not None and body.is_file()
    record("M5-recall", "PASS" if cited else "FAIL",
           f"{surface}: hit={hit} citation={citation!r} resolvable={body.is_file()}"
           + ("" if cited else
              f" — the output names the memory but not where to read it: "
              f"{out[-240:]!r}"))


# ── M6 ──────────────────────────────────────────────────────────────────────
def _crashed(proc):
    """Did this instrument crash rather than report?"""
    err = (proc.stderr or "") + (proc.stdout or "")
    return ("Traceback (most recent call last)" in err
            or "ImportError" in err or "ModuleNotFoundError" in err
            or "SyntaxError" in err)


def m6_reports_breakage(sb):
    """Fault injection: the instruments must fail on a broken channel."""
    manifest = sb.mesh_root / "state" / "servable.json"
    saved = manifest.read_text(encoding="utf-8") if manifest.is_file() else None
    # Healthy baseline first, so a crash cannot pass as a fault report.
    base = run([sys.executable, str(HERE / "canary.py")], env=sb.env)
    if base.returncode != 0 or _crashed(base):
        record("M6-dark-corpus", "FAIL",
               f"canary.py does not pass on a HEALTHY corpus (rc="
               f"{base.returncode}) — nothing it says about a broken one "
               f"means anything: {(base.stdout + base.stderr)[-300:]}")
    else:
        try:
            faults.append("removed the servable manifest from the sandbox store")
            if manifest.is_file():
                manifest.unlink()
            c = run([sys.executable, str(HERE / "canary.py")], env=sb.env)
            out = c.stdout + c.stderr
            loud = c.returncode != 0 and not _crashed(c) and "dark" in out.lower()
            record("M6-dark-corpus", "PASS" if loud else "FAIL",
                   "canary.py exits non-zero on a dark corpus, naming it" if loud
                   else (f"canary.py CRASHED rather than reporting it "
                         f"(rc={c.returncode}): {out[-300:]}" if _crashed(c) else
                         f"canary.py rc={c.returncode} and did not name a dark "
                         f"corpus: {out[-300:]}"))
        finally:
            if saved is not None:
                manifest.write_text(saved, encoding="utf-8")
                faults.append("restored the servable manifest")

    # Something outside the fold must watch it: either the scheduler runs
    # fold_watch.py, or freshness.json registers the fold timer/mesh_watch.
    # Inside a seed install, check that install's observer, not the workspace's.
    observer_root = HERE.parent if (HERE.parent / ".cc-seed").is_dir() else WORKSPACE
    freshness = observer_root / "observability" / "freshness.json"
    sched = observer_root / "scheduler" / "manifest.yml"
    watchers = []
    if freshness.is_file():
        text = freshness.read_text(encoding="utf-8")
        if "memory-fold" in text or "memory_fold" in text:
            watchers.append(f"{freshness.name} registers the fold timer")
        if "mesh_watch" in text:
            watchers.append(f"{freshness.name} registers mesh_watch")
    if sched.is_file() and "fold_watch.py" in sched.read_text(encoding="utf-8"):
        watchers.append("the scheduler runs fold_watch.py")
    record("M6-outside-observer", "PASS" if watchers else "FAIL",
           "; ".join(watchers) + f" [{observer_root}]" if watchers
           else f"nothing outside the fold watches it ({freshness}, {sched})")

    # The watcher must be able to go red: backdate the fold state and check.
    watch = HERE / "fold_watch.py"
    state = sorted((sb.mesh_root / "state").glob("*.json"))
    if not watch.is_file():
        record("M6-watcher-can-fail", "SKIP", f"no {watch}")
    elif not state:
        record("M6-watcher-can-fail", "SKIP", "the sandbox fold wrote no state")
    else:
        stamps = {f: (f.stat().st_atime, f.stat().st_mtime) for f in state}
        healthy = run([sys.executable, str(watch)], env=sb.env)
        if healthy.returncode != 0 or _crashed(healthy):
            record("M6-watcher-can-fail", "FAIL",
                   f"fold_watch is not green on a HEALTHY sandbox "
                   f"(rc={healthy.returncode}) — a watcher that is always red "
                   f"proves nothing by going red: "
                   f"{(healthy.stdout + healthy.stderr)[-300:]}")
            return
        try:
            faults.append("backdated the sandbox fold's state files by 2h")
            old_ts = time.time() - 7200
            for f in state:
                os.utime(f, (old_ts, old_ts))
            r = run([sys.executable, str(watch)], env=sb.env)
            out = r.stdout + r.stderr
            loud = r.returncode != 0 and not _crashed(r) and "fold" in out.lower()
            record("M6-watcher-can-fail", "PASS" if loud else "FAIL",
                   "fold_watch goes red on a stopped fold timer, naming it"
                   if loud else
                   (f"fold_watch CRASHED instead of reporting: {out[-300:]}"
                    if _crashed(r) else
                    f"fold_watch rc={r.returncode} and did not name the fold: "
                    f"{out[-300:]}"))
        finally:
            for f, ts in stamps.items():
                os.utime(f, ts)
            faults.append("restored the state file timestamps")


# ── harnesses ───────────────────────────────────────────────────────────────
def harness_spec(name):
    """The non-interactive one-shot argv for a harness, or None.

    Only verified CLI shapes are listed; tool permission is as narrow as each
    CLI allows.
    """
    if not name or not shutil.which(name):
        return None
    if name == "claude":
        return {"argv": lambda p: ["claude", "-p", p, "--allowedTools", "Bash"]}
    if name == "codex":
        return {"argv": lambda p: ["codex", "exec", "--sandbox", "workspace-write",
                                   "--skip-git-repo-check", p]}
    if name == "grok":
        return {"argv": lambda p: ["grok", "-p", p, "--always-approve"]}
    return None


def harness_skip_reason(name):
    if name is None:
        return "no --harness given; the harness half was not attempted"
    if not shutil.which(name):
        return f"{name} is not on PATH on this host"
    return (f"{name} is installed but no non-interactive prompt shape is "
            f"verified here — guessing one would make this test lie")


# ── main ────────────────────────────────────────────────────────────────────
def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--harness", choices=["claude", "codex", "grok"],
                    help="also run the harness half of M1/M4/M5 against this CLI")
    ap.add_argument("--only", nargs="+", metavar="M",
                    help="run only these properties (M1 M2 …)")
    ap.add_argument("--json", metavar="PATH",
                    help="write the evidence file publish.sh gates on")
    args = ap.parse_args(argv)
    ALL = ["M1", "M2", "M3", "M4", "M5", "M6"]
    want = {m.upper() for m in (args.only or ALL)}
    # Reject unknown properties so an empty run cannot be reported green.
    unknown = sorted(want - set(ALL))
    if unknown:
        print(f"no such property: {', '.join(unknown)} — known: {', '.join(ALL)}",
              file=sys.stderr)
        return 2

    started = time.time()
    # Measure package integrity before any property runs.
    live_sha = m0_package_integrity()
    print(f"contract_test — memory-mesh, {WORKSPACE}"
          + (f", harness={args.harness}" if args.harness else ""))
    with tempfile.TemporaryDirectory(prefix="mesh-contract-") as tmp:
        sb = Sandbox(tmp)
        print(f"  sandbox: HOME={sb.home} store={sb.store}")
        door = None
        if "M1" in want:
            door = m1_one_door(sb, args.harness)
        if door is None:
            found = doors()
            door = found[0] if len(found) == 1 else None
        if "M2" in want:
            if door:
                m2_one_store(sb, door)
            else:
                record("M2-resolve", "SKIP", "no single door to resolve against")
        if "M3" in want:
            m3_fold_projects(sb)
        if "M4" in want:
            m4_reaches_session(sb, args.harness)
        if "M5" in want:
            m5_recall_cites(sb)
        if "M6" in want:
            m6_reports_breakage(sb)
        if "M1" in want and args.harness and door:
            m1_harness(sb, door, args.harness)
        elif "M1" in want and not args.harness:
            record("M1-harness", "SKIP", harness_skip_reason(None))

    fails = [r for r in results if r[1] == "FAIL"]
    skips = [r for r in results if r[1] == "SKIP"]
    print(f"\n{len([r for r in results if r[1] == 'PASS'])}/{len(results)} pass, "
          f"{len(fails)} fail, {len(skips)} skip  ({time.time() - started:.0f}s)")
    for name, _, detail in fails:
        print(f"  FAIL {name}: {detail}")
    for name, _, detail in skips:
        print(f"  SKIP {name}: {detail}")
    if faults:
        print("injected faults, all restored:")
        for f in faults:
            print(f"  - {f}")
    if args.json:
        tested_root, package_sha = tested_identity()
        Path(args.json).write_text(json.dumps({
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "workspace": str(WORKSPACE),
            "tested_root": tested_root,
            # tested_sha: measured hash of the installed tree.
            # tested_package_sha: the receipt's claimed dist sha.
            "tested_sha": live_sha,
            "tested_package_sha": package_sha,
            "selected": sorted(want),
            "harness": args.harness,
            "green": not fails and not skips,
            "results": [{"property": n, "status": s, "detail": d}
                        for n, s, d in results],
            "faults": faults,
        }, indent=2) + "\n", encoding="utf-8")
    # A SKIP does not fail the run, but it never makes it green.
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
