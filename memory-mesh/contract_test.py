#!/usr/bin/env python3
"""contract_test — the six properties that define "the memory works".

SEED-080. Before this file, "the memory mesh works" had no checkable meaning,
so a seed install could ship a store with no writer, a writer no skill names,
and a retrieval hook nothing wires — and every test in the tree stayed green.
Each fix shipped without a definition of done. These are the six:

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

Run against a DISPOSABLE mesh and a DISPOSABLE store, never the operator's:
the sandbox is `drill.Mesh` (three clones, real git transport) plus a temp
store wired through MESH_STORE_DIR/MEMORY_WRITE_STORE. `harness_store()`'s
sandbox guard means a run here can never publish over the live MEMORY.md.

A property that cannot be attempted here is SKIP, reported by name, and is
NOT a pass (drill.py's 2026-07-31 lesson: a silent skip reported green
exactly where the proof mattered).

    contract_test.py                     # script half, every property
    contract_test.py --harness claude    # + the harness half of M1/M4/M5
    contract_test.py --only M1 M3
    contract_test.py --json evidence.json

Harness scope (2026-09-18): `--harness` takes claude, codex or grok. The
codex and grok lanes have no hook event, so their M4 is an accepted
exception, and a harness whose non-interactive prompt shape this file has
not verified is SKIPped by name rather than guessed at.

Stdlib only; targets /usr/bin/python3.
"""
import argparse
import json
import os
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
# The turn M4/M5 must serve the probe back for. Deliberately NOT the slug:
# retrieval that only works when you already know the slug is a lookup, not
# recall.
PROBE_TURN = "where does ranch wombat telemetry come from, zircon or copper?"

TIMEOUT = 120
HARNESS_TIMEOUT = 240

results = []          # (property, status, detail) — status in PASS/FAIL/SKIP
faults = []           # every injected fault and its restoration, logged


def record(prop, status, detail=""):
    results.append((prop, status, detail))
    tag = {"PASS": "ok", "FAIL": "FAIL", "SKIP": "SKIP"}[status]
    print(f"  {tag}: {prop}" + (f" — {detail}" if detail else ""))


def run(cmd, env=None, cwd=None, timeout=TIMEOUT, stdin=None):
    e = dict(os.environ)
    e.update(env or {})
    return subprocess.run([str(c) for c in cmd], capture_output=True, text=True,
                          env=e, cwd=cwd, timeout=timeout, input=stdin)


# ── the door ────────────────────────────────────────────────────────────────
# Resolved, never assumed. Before Step 2 the engine lives in the vault's
# skills-core (installed at ~/.claude/skills/improve/); after it, in
# memory-mesh/. BOTH existing is not a tolerable transition state — it is two
# doors, which is the whole failure this property exists to catch — so the
# candidates are counted, not searched in priority order.
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
    """A disposable INSTALL — a fake HOME — and the env every probe runs under.

    Not a MESH_ROOT override. `harness_store()` refuses to publish whenever
    MESH_ROOT is not the default (the 2026-07-29 sandbox guard: a drill run
    had published test fixtures over the operator's live MEMORY.md), so under
    a MESH_ROOT override the fold writes no index, no quarantine list and no
    servable manifest — M3, M4 and M5 could only ever fail, and they would
    fail for the guard's reason rather than the property's.

    A fake HOME moves DEFAULT_MESH_ROOT, store_dir() and the writer's own
    store together, so every path resolves normally inside the sandbox and
    the guard stays armed exactly as it is in production. It is also the
    honest shape of the thing being proven: a fresh seed install.
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
        for cmd in (["git", "init", "-q", str(self.store)],
                    ["git", "-C", str(self.store), "config", "user.email", "mesh@test"],
                    ["git", "-C", str(self.store), "config", "user.name", "contract"]):
            run(cmd, env={"HOME": str(self.home)})

    @property
    def env(self):
        # Deliberately NO store override: mesh_lib and memory_write.py each
        # derive the store their own way, and M2 is the assertion that they
        # agree. Handing both the same env var would make the test the thing
        # making them agree.
        return {"HOME": str(self.home), "MESH_HOST": "contract",
                "MESH_SESSION_ID": "contract-test"}

    def fold(self):
        return run([sys.executable, str(HERE / "fold.py"), "--project"],
                   env=self.env)


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
             "--hook", "contract probe", "--lineage", "craig-direct",
             "--no-push", "--commit"], env=sb.env)
    landed = (sb.store / f"{PROBE_SLUG}.md").is_file()
    record("M1-write", "PASS" if landed else "FAIL",
           str(door.name) if landed else f"rc={r.returncode} {r.stdout[-400:]}{r.stderr[-400:]}")
    if not landed:
        return door

    # The door is only "the one door" if the other paths are shut. The guard
    # is what makes that technical rather than prose.
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
    blocked = g.returncode != 0 or "deny" in (g.stdout + g.stderr).lower()
    record("M1-guard", "PASS" if blocked else "FAIL",
           "direct store write refused" if blocked
           else f"guard allowed a write around the door (rc={g.returncode})")
    return door


def m1_harness(sb, door, harness):
    """The incident shape: a harness turn, with only the skill's own text,
    must reach the door — not emit.py, not a hand-written file.

    Runs under the REAL HOME (the CLI's own auth lives there) with the store
    and the event log diverted to the sandbox by explicit override, so the
    turn can never write the operator's memory. Checked, not assumed: the
    probe asserts the sandbox store received the file.
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
        "reached the door.' --hook 'harness probe' --lineage craig-direct "
        "--no-push --commit")
    env = {"MEMORY_WRITE_STORE": str(sb.store), "MESH_ROOT": str(sb.mesh_root),
           "MESH_HOST": "contract", "MESH_SESSION_ID": f"contract-{harness}"}
    before = set(p.name for p in sb.store.glob("*.md"))
    try:
        r = run(spec["argv"](prompt), env=env, cwd=str(WORKSPACE),
                timeout=HARNESS_TIMEOUT)
    except subprocess.TimeoutExpired:
        record(f"M1-harness-{harness}", "FAIL",
               f"the {harness} turn timed out at {HARNESS_TIMEOUT}s")
        return
    new_files = set(p.name for p in sb.store.glob("*.md")) - before
    # WHICH writer ran. A door write leaves an event whose subject names the
    # slug; a direct emit.py call or a hand-written file leaves the store
    # file with no matching event, which is the exact bypass shape.
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

    # A hardcoded absolute store or door path is one host's truth shipped to
    # every host. Scan the group's own source, not the whole tree.
    # Composed, not written out: a contiguous copy of the old door path in
    # this file would itself be the fleet literal the seed build refuses to
    # ship (build_seed.fleet_literal_audit). The store needle is derived from
    # THIS host's home, so it catches whatever absolute store path a given
    # host would have hardcoded, not just the one that bit us.
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
    # The door files a new memory on-demand by default (admission policy E),
    # so "projected" means the index DECIDED about it — a row in MEMORY.md or
    # a line in the on-demand list — not that it rode the always-on tier.
    decided = ((index.is_file() and PROBE_SLUG in index.read_text(encoding="utf-8"))
               or (exclude.is_file() and PROBE_SLUG in exclude.read_text(encoding="utf-8")))
    # The delivery manifest is mesh state, not store content (mesh_lib
    # write_servable_manifest: "derived state belongs with the deriver").
    manifest = sb.mesh_root / "state" / "servable.json"
    servable = False
    if manifest.is_file():
        try:
            servable = PROBE_SLUG in (json.loads(manifest.read_text()).get("slugs") or [])
        except Exception:
            servable = False
    ok = memory.is_file() and index.is_file() and decided and servable
    record("M3-fold", "PASS" if ok else "FAIL",
           f"file={memory.is_file()} index={index.is_file()} decided={decided} "
           f"servable={servable}"
           + ("" if ok else f" rc={r.returncode} {r.stdout[-300:]}{r.stderr[-300:]}"))
    return ok


# ── M4 ──────────────────────────────────────────────────────────────────────
def m4_reaches_session(sb, harness):
    payload = json.dumps({"hook_event_name": "UserPromptSubmit",
                          "prompt": PROBE_TURN})
    r = run([sys.executable, str(HERE / "retrieve.py")], env=sb.env, stdin=payload)
    served = PROBE_SLUG in r.stdout
    record("M4-serve", "PASS" if served else "FAIL",
           "the probe memory was served for a turn that never named it"
           if served else f"not served: {r.stdout[-200:]}{r.stderr[-200:]}")

    # Delivery wiring: scoring the right memory is worthless if nothing calls
    # the scorer. Per harness, because only Claude Code has a hook event.
    if harness in (None, "claude"):
        # THIS install's settings first. Falling straight through to the user
        # scope made a seed install built on a fleet host pass on the FLEET's
        # wiring — a green for the wrong reason, which is the failure this
        # whole contract exists to make impossible.
        local = WORKSPACE / ".claude" / "settings.json"
        user = Path.home() / ".claude" / "settings.json"
        hit = next((p for p in (local, user)
                    if p.is_file() and str(HERE) in p.read_text(encoding="utf-8")), None)
        if hit is None:
            # Nothing names THIS mesh. A hook naming some other install's
            # retrieve.py does not deliver this install's memory.
            looked = f"{local}, {user}"
            record("M4-wiring", "FAIL",
                   f"no hook in {looked} runs {HERE}/retrieve.py — run "
                   f"install.py --approve memory-hooks")
        else:
            record("M4-wiring", "PASS", f"{HERE.name}/retrieve.py wired in {hit}")
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
        # Until Step 4b's CLI exists, the recall surface IS retrieve + the
        # store. Say so in the detail rather than letting the fallback pass
        # as if the CLI were proven.
        r = run([sys.executable, str(HERE / "retrieve.py")], env=sb.env,
                stdin=json.dumps({"prompt": PROBE_TURN}))
        out = r.stdout
        surface = "retrieve.py (no recall.py CLI yet — Step 4b)"
    hit = PROBE_SLUG in out
    # A citation is "where this came from", resolvable by the reader.
    cited = hit and (sb.store / f"{PROBE_SLUG}.md").is_file()
    record("M5-recall", "PASS" if cited else "FAIL",
           f"{surface}: hit={hit} resolvable={cited}")


# ── M6 ──────────────────────────────────────────────────────────────────────
def m6_reports_breakage(sb):
    """Fault injection: the instruments must FAIL on a broken channel. An
    instrument that stays green on a dark corpus is the failure it was built
    to catch."""
    manifest = sb.mesh_root / "state" / "servable.json"
    saved = manifest.read_text(encoding="utf-8") if manifest.is_file() else None
    try:
        faults.append("removed the servable manifest from the sandbox store")
        if manifest.is_file():
            manifest.unlink()
        c = run([sys.executable, str(HERE / "canary.py")], env=sb.env)
        loud = c.returncode != 0
        record("M6-dark-corpus", "PASS" if loud else "FAIL",
               "canary.py exits non-zero on a dark corpus" if loud
               else "canary.py reported health with no servable manifest")
    finally:
        if saved is not None:
            manifest.write_text(saved, encoding="utf-8")
            faults.append("restored the servable manifest")

    # The fold cannot certify its own liveness (PRINCIPLES 21): something
    # outside its failure domain has to watch the timer, and be watched in
    # turn. Two shapes are legitimate — a seed install schedules fold_watch.py
    # as mesh_watch under its own scheduler; the fleet registers the fold's
    # systemd timer with the freshness checker directly.
    freshness = WORKSPACE / "observability" / "freshness.json"
    sched = WORKSPACE / "scheduler" / "manifest.yml"
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
           "; ".join(watchers) if watchers
           else f"nothing outside the fold watches it ({freshness}, {sched})")

    # And the watcher has to be able to FAIL — a green watcher that cannot go
    # red is a decoration. Drive it against an impossible freshness window.
    watch = HERE / "fold_watch.py"
    state = sorted((sb.mesh_root / "state").glob("*.json"))
    if not watch.is_file():
        record("M6-watcher-can-fail", "SKIP", f"no {watch}")
    elif not state:
        record("M6-watcher-can-fail", "SKIP", "the sandbox fold wrote no state")
    else:
        stamps = {f: (f.stat().st_atime, f.stat().st_mtime) for f in state}
        try:
            faults.append("backdated the sandbox fold's state files by 2h")
            old_ts = time.time() - 7200
            for f in state:
                os.utime(f, (old_ts, old_ts))
            r = run([sys.executable, str(watch)], env=sb.env)
            record("M6-watcher-can-fail", "PASS" if r.returncode != 0 else "FAIL",
                   "fold_watch goes red on a stopped fold timer"
                   if r.returncode != 0 else
                   f"fold_watch stayed green with a 2h-old fold: {r.stdout[-200:]}")
        finally:
            for f, ts in stamps.items():
                os.utime(f, ts)
            faults.append("restored the state file timestamps")


# ── harnesses ───────────────────────────────────────────────────────────────
def harness_spec(name):
    """The non-interactive one-shot shape for a harness, or None.

    Only shapes read out of the installed CLI's own --help are here
    (2026-09-18: `claude -p`, `codex exec`, `grok -p/--single`). A guessed
    flag that silently opened a REPL and timed out would report FAIL for the
    harness when the truth is "this file does not know how to drive it" — the
    instrument lying about its subject. Tool permission is granted as
    narrowly as each CLI allows, never blanket bypass: this runs on the
    operator's own machine.
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
    want = {m.upper() for m in (args.only or ["M1", "M2", "M3", "M4", "M5", "M6"])}

    started = time.time()
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
        Path(args.json).write_text(json.dumps({
            "generated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "workspace": str(WORKSPACE),
            "harness": args.harness,
            "green": not fails and not skips,
            "results": [{"property": n, "status": s, "detail": d}
                        for n, s, d in results],
            "faults": faults,
        }, indent=2) + "\n", encoding="utf-8")
    # A SKIP is an unmet proof obligation: it does not fail the run, but it
    # can never make it green either (see --json `green`).
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
