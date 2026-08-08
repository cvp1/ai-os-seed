#!/usr/bin/env python3
"""memory-mesh drills — the SPEC's proof obligations, run against a REAL
three-node mesh built in a temp dir (three clones, git transport, the same
emit/fold code paths — only the ssh hop is replaced by local paths, which
exercises identical git mechanics via file:// remotes).

    drill.py            # run all
    drill.py 1 5        # run selected

Nothing here touches the live ~/memory-events repo.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

CODE = Path(__file__).resolve().parent
FAILS = []
# A skipped drill is an UNMET PROOF OBLIGATION, not a pass. Drills 6 and 11 —
# the two that prove signature authority, i.e. that an agent cannot sign as
# Craig — return early when signing or ssh-agent is unavailable. They did so
# silently, and main() then printed "all selected drills pass": on a host with a
# broken signing environment the suite reported green precisely where its most
# security-relevant proof had not run. Found by an outside review, 2026-07-31.
SKIPS = []


def check(name, cond, detail=""):
    tag = "ok" if cond else "FAIL"
    print(f"  {tag}: {name}" + ("" if cond else f" — {detail}"))
    if not cond:
        FAILS.append(name)


def skip(name, why):
    """Record a proof obligation that could not be attempted here."""
    print(f"  SKIP: {name} — {why}")
    SKIPS.append(f"{name} ({why})")


def run(cmd, env=None, cwd=None, check_rc=True):
    e = dict(os.environ)
    e.update(env or {})
    r = subprocess.run(cmd, capture_output=True, text=True, env=e, cwd=cwd, timeout=120)
    if check_rc and r.returncode != 0:
        raise RuntimeError(f"{' '.join(map(str, cmd))}\n{r.stdout}{r.stderr}")
    return r


class Mesh:
    """Three 'hosts' as three clones; peers wired as local-path remotes."""
    HOSTS = ["hosta", "hostb", "hostc"]

    def __init__(self, root):
        self.root = Path(root)
        self.dirs = {}
        # The mesh bootstraps from ONE seed clone — a shared root commit is
        # what makes "unrelated histories" a meaningful alarm (a peer whose
        # history shares no ancestor is a wiped/recreated repo, and the fold
        # must REFUSE it, not quietly adopt it). First drill run proved git
        # enforces this for us: independent inits refused to merge.
        seed = self.root / "_seed"
        seed.mkdir(parents=True)
        run(["git", "init", "-q", str(seed)])
        run(["git", "-C", str(seed), "config", "user.email", "mesh@test"])
        run(["git", "-C", str(seed), "config", "user.name", "seed"])
        (seed / "events").mkdir()
        (seed / "events" / ".keep").write_text("")
        # Derived state stays out of history (single-writer invariant —
        # only events/ is shared truth).
        (seed / ".gitignore").write_text("views/\nstate/\nview.version\n")
        run(["git", "-C", str(seed), "add", "-A"])
        run(["git", "-C", str(seed), "commit", "-qm", "mesh seed"])
        for h in self.HOSTS:
            d = self.root / h
            run(["git", "clone", "-q", str(seed), str(d)])
            run(["git", "-C", str(d), "config", "user.email", "mesh@test"])
            run(["git", "-C", str(d), "config", "user.name", h])
            run(["git", "-C", str(d), "remote", "remove", "origin"])
            self.dirs[h] = d
        for h in self.HOSTS:
            for p in self.HOSTS:
                if p != h:
                    run(["git", "-C", str(self.dirs[h]), "remote", "add", p,
                         str(self.dirs[p])])
        # Per-drill mesh.toml is not consulted: peers come from remotes; we
        # monkey-patch via env-driven peers file instead — simplest: fold
        # discovers peers from `git remote`, drill mode.
        self.toml = self.root / "mesh-drill.toml"
        self.toml.write_text("\n".join(
            f'[[hosts]]\nname = "{h}"\nssh = "unused-{h}"\nrepo = "unused"\n'
            for h in self.HOSTS))

    def env(self, h):
        e = {"MESH_ROOT": str(self.dirs[h]), "MESH_HOST": h,
             "MESH_DRILL_LOCAL": "1"}
        key = self.root / "drillkey"
        if key.exists():
            e["MESH_SIGNING_KEY"] = str(key)
            e["MESH_ALLOWED_SIGNERS"] = str(self.root / "drill_allowed_signers")
        return e

    def make_signing_key(self):
        """Throwaway passphrase-less key + registry — the REAL key is
        passphrase-protected on purpose (that passphrase is why an agent can
        never sign), so drills cannot use it."""
        key = self.root / "drillkey"
        if key.exists():
            return          # idempotent: more than one drill needs the fixture
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "drill",
             "-f", str(key)])
        pub = (self.root / "drillkey.pub").read_text().strip()
        (self.root / "drill_allowed_signers").write_text(f"craig@fleet {pub}\n")

    def emit(self, h, *args, check_rc=True):
        return run([sys.executable, str(CODE / "emit.py"), "--no-nudge",
                    "--session", f"drill-{h}", *args], env=self.env(h),
                   check_rc=check_rc)

    def fold(self, h):
        return run([sys.executable, str(CODE / "fold.py")], env=self.env(h))

    def fold_all(self, times=2):
        # Two rounds so second-hop knowledge (a's events via b) also settles.
        for _ in range(times):
            for h in self.HOSTS:
                self.fold(h)

    def git(self, h, *args):
        return run(["git", "-C", str(self.dirs[h]), *args], check_rc=False)

    def parked(self, h):
        f = self.dirs[h] / "views" / "operator" / "CONFLICTS.md"
        return f.read_text() if f.exists() else ""

    def version(self, h):
        f = self.dirs[h] / "view.version"
        return f.read_text().strip() if f.exists() else None


def drill_1(m):
    print("drill 1 — split-brain replay (the 14:30/15:19 incident)")
    m.emit("hosta", "--kind", "correct", "--subject", "ssh-route/hostb",
           "--polarity", "exists", "--content", "route verified live by operator",
           "--home", "FLEET.md#reachability", "--confidence", "operator-stated")
    m.emit("hostb", "--kind", "assert", "--subject", "ssh-route/hostb",
           "--polarity", "absent", "--content", "permission denied, no route",
           "--home", "FLEET.md#reachability")
    m.fold_all()
    for h in m.HOSTS:
        c = m.parked(h)
        check(f"{h} parked the subject", "ssh-route/hostb" in c, c[:120])
        check(f"{h} holds BOTH claims", "exists" in c and "absent" in c)
    r = m.fold("hosta")
    check("steady-state fold is silent (edge-trigger)", r.stdout.strip() == "",
          r.stdout[:120])


def drill_2(m):
    print("drill 2 — partition: emit everywhere, converge after")
    for h in m.HOSTS:
        m.emit(h, "--kind", "lesson", "--subject", f"lesson/partition-{h}",
               "--content", f"lesson emitted on {h} during partition")
    # 'Partition' = simply not folding; then reconnect = fold_all.
    m.fold_all()
    idx = {h: (m.dirs[h] / "views" / "operator" / "INDEX.md").read_text()
           for h in m.HOSTS}
    for h in m.HOSTS:
        ok = all(f"partition-{src}" in idx[h] for src in m.HOSTS)
        check(f"{h} sees all three partition lessons", ok, idx[h][:200])


def drill_3(m):
    print("drill 3 — kill test: duplicate emit is deduped by id")
    # Same host+session+ts+content → same id. Force identical ts via env? The
    # id is content-derived; emit twice quickly with identical args and check
    # the fold serves ONE. (If ts differs across the second boundary, ids
    # differ — retry once on the rare boundary hit.)
    for attempt in range(2):
        m.emit("hostc", "--kind", "lesson", "--subject", "lesson/dup-test",
               "--content", "duplicate emission test")
        m.emit("hostc", "--kind", "lesson", "--subject", "lesson/dup-test",
               "--content", "duplicate emission test")
        log = (m.dirs["hostc"] / "events" / "hostc.ndjson").read_text()
        ids = [json.loads(l)["id"] for l in log.splitlines()
               if "dup-test" in l]
        if len(set(ids)) == 1:
            break
    m.fold("hostc")
    idx = (m.dirs["hostc"] / "views" / "operator" / "INDEX.md").read_text()
    check("duplicate ids collapse to one served item",
          idx.count("duplicate emission test") == 1, f"ids={ids}")
    # Torn line: append garbage half-line; fold must hold it out, not die.
    with open(m.dirs["hostc"] / "events" / "hostc.ndjson", "a") as f:
        f.write('{"id":"torn","ts":"2026-')
    run(["git", "-C", str(m.dirs["hostc"]), "commit", "-qam", "torn"])
    r = m.fold("hostc")
    check("torn line held out, fold survives",
          "unparseable" in r.stdout, r.stdout[:200])


def drill_4(m):
    print("drill 4 — non-fast-forward attack is refused")
    m.emit("hosta", "--kind", "lesson", "--subject", "lesson/pre-rebase",
           "--content", "event before history rewrite")
    m.fold_all()
    run(["git", "-C", str(m.dirs["hosta"]), "reset", "-q", "--hard", "HEAD~1"])
    m.emit("hosta", "--kind", "lesson", "--subject", "lesson/post-rebase",
           "--content", "history was rewritten under this event")
    r = m.fold("hostb")
    check("peer refuses the rewritten history",
          "NON-FAST-FORWARD" in r.stdout, r.stdout[:200])


def drill_5(m):
    print("drill 5 — replay determinism: identical view.version everywhere")
    m.fold_all()
    versions = {h: m.version(h) for h in m.HOSTS}
    # hosta rewrote history in drill 4, so b and c legitimately differ from a
    # if run after; on a fresh mesh all three must match.
    vals = set(versions.values())
    check("all hosts computed the same folded state", len(vals) == 1,
          str(versions))


def drill_6(m):
    print("drill 6 — signed truth defends itself; forged signature is caught")
    import json as _j
    m.make_signing_key()
    # Operator signs truth on hosta.
    r = run([sys.executable, str(CODE / "sign.py"),
             "--subject", "policy/drill-signed", "--content", "operator says A",
             "--home", "SPEC.md", "--polarity", "exists"], env=m.env("hosta"),
            check_rc=False)
    if r.returncode != 0:
        skip("drill 6 signed-truth authority",
             f"signing unavailable ({(r.stderr or r.stdout).strip()[:60]})")
        return
    # An unsigned session on hostb disagrees.
    m.emit("hostb", "--kind", "assert", "--subject", "policy/drill-signed",
           "--polarity", "exists", "--content", "poisoned session says B",
           "--home", "SPEC.md")
    m.fold_all()
    c = m.parked("hostc")
    check("unsigned contradiction of signed truth parks fleet-wide",
          "policy/drill-signed" in c, c[:150])
    r2 = m.fold("hosta")
    # Forge: tamper a signed event's content, keeping its signature.
    log = m.dirs["hosta"] / "events" / "hosta.ndjson"
    lines = log.read_text().splitlines()
    for i, l in enumerate(lines):
        d = _j.loads(l)
        if d.get("sig") and d["subject"] == "policy/drill-signed":
            d["content"] = "FORGED: attacker rewrote signed content"
            lines[i] = _j.dumps(d, separators=(",", ":"))
            break
    log.write_text("\n".join(lines) + "\n")
    run(["git", "-C", str(m.dirs["hosta"]), "commit", "-qam", "forge"])
    out = m.fold("hosta").stdout
    check("forged signature is detected and alarmed",
          "DOES NOT verify" in out, out[:200])


def drill_11(m):
    print("drill 11 — an ssh-agent cannot mint the operator's signature")
    # The seam this drill exists for: every OTHER drill fixture uses a
    # passphrase-less throwaway key (see make_signing_key), which can never
    # exercise the agent path — so for the whole life of the v1.4 "time-boxed
    # authority" design, mesh_lib signed through ssh-agent while this suite's
    # own fixture comment asserted "an agent can never sign". The tests could
    # not see the drift because the fixture removed the property under test.
    # This drill restores it: a passphrase-protected key, held in a real
    # agent, must NOT be signable without a human.
    import shutil
    if not shutil.which("ssh-agent"):
        skip("drill 11 agent-held key needs a human", "no ssh-agent on this host")
        return
    d = Path(m.root) / "agentprobe"
    d.mkdir(exist_ok=True)
    key, pw = d / "probekey", "drill-throwaway-passphrase"
    if not key.exists():
        run(["ssh-keygen", "-q", "-t", "ed25519", "-N", pw, "-C", "drill-agent",
             "-f", str(key)])
    askpass = d / "askpass.sh"
    askpass.write_text(f"#!/bin/sh\necho '{pw}'\n")
    askpass.chmod(0o700)
    msg = d / "msg.txt"
    msg.write_text("drill payload\n")

    script = f"""
      eval "$(ssh-agent -s)" >/dev/null
      SSH_ASKPASS='{askpass}' SSH_ASKPASS_REQUIRE=force ssh-add '{key}' >/dev/null 2>&1
      loaded=$(ssh-add -l 2>/dev/null | grep -c drill-agent)
      rm -f '{msg}.sig'
      # unattended: no askpass, no tty. Only an agent could satisfy this.
      unset SSH_ASKPASS SSH_ASKPASS_REQUIRE
      env -u SSH_AUTH_SOCK ssh-keygen -Y sign -f '{key}' -n mesh '{msg}' </dev/null >/dev/null 2>&1
      stripped=$?
      rm -f '{msg}.sig'
      echo "loaded=$loaded stripped=$stripped"
      ssh-agent -k >/dev/null 2>&1
    """
    out = run(["bash", "-c", script], check_rc=False).stdout.strip()
    vals = dict(p.split("=", 1) for p in out.split() if "=" in p)
    if vals.get("loaded") != "1":
        skip("drill 11 agent-held key needs a human",
             f"could not load the probe key into an agent ({out})")
        return
    # The control that matters: with the socket stripped, signing must fail
    # even though the agent is running and holds the key.
    check("agent is running and holds a passphrase-protected key", True, out)
    check("with SSH_AUTH_SOCK stripped, unattended signing is REFUSED",
          vals.get("stripped") != "0", out)
    # And mesh_lib must be the thing doing the stripping, not the caller.
    # Assert against EXECUTABLE code only — docstring and comments here talk
    # about ssh-agent at length (explaining why it is refused), so a
    # text-search over the whole function would fail on its own rationale.
    import ast
    tree = ast.parse((CODE / "mesh_lib.py").read_text())
    fn = next(n for n in ast.walk(tree)
              if isinstance(n, ast.FunctionDef) and n.name == "sign_event")
    stmts = fn.body[1:] if ast.get_docstring(fn) else fn.body
    code = "\n".join(ast.unparse(s) for s in stmts)   # comments/docstring gone
    check("mesh_lib.sign_event strips SSH_AUTH_SOCK itself",
          "SSH_AUTH_SOCK" in code and "env=env" in code, code[:200])
    check("mesh_lib.sign_event has no ssh-agent signing path",
          "'-U'" not in code and '"-U"' not in code, code[:200])
    check("SSH_AUTH_SOCK is only ever excluded, never read as a condition",
          code.count("SSH_AUTH_SOCK") == 1 and "!=" in code, code[:200])


def drill_7(m):
    print("drill 7 — lesson revisions chain explicitly; blind collision parks")
    idx = lambda h: (m.dirs[h] / "views" / "operator" / "INDEX.md").read_text()
    # Revision on one host: v2 must supersede v1 (explicit chain, invariant 4).
    m.emit("hosta", "--kind", "lesson", "--subject", "lesson/rev-test",
           "--content", "rule v1")
    m.fold_all()
    m.emit("hosta", "--kind", "lesson", "--subject", "lesson/rev-test",
           "--content", "rule v2 replaces v1 wording")
    m.fold_all()
    for h in m.HOSTS:
        check(f"{h} serves only the revision", "rule v2" in idx(h)
              and "rule v1\n" not in idx(h) + "\n", idx(h)[:200])
        check(f"{h} did not park the clean chain",
              "lesson/rev-test" not in m.parked(h))
    # Blind collision: two hosts revise one subject without seeing each other.
    m.emit("hostb", "--kind", "lesson", "--subject", "lesson/collide",
           "--content", "belief B")
    m.emit("hostc", "--kind", "lesson", "--subject", "lesson/collide",
           "--content", "belief C")
    m.fold_all()
    for h in m.HOSTS:
        check(f"{h} parked the blind collision", "lesson/collide" in m.parked(h))
    # Resolution: one re-emit after the fetch supersedes BOTH sides.
    m.emit("hostb", "--kind", "lesson", "--subject", "lesson/collide",
           "--content", "belief reconciled")
    m.fold_all()
    for h in m.HOSTS:
        check(f"{h} cleared the park via the chain",
              "lesson/collide" not in m.parked(h) and "belief reconciled" in idx(h))


def drill_8(m):
    print("drill 8 — N concurrent writers on ONE host do not lose a commit")
    # SPEC invariant 1 calls events/<host>.ndjson single-writer, meaning one
    # writer per HOST. Several agents in one shell are several writers to one
    # file. Measured 2026-07-28 BEFORE repo_lock(): 5 concurrent emits landed
    # 5 intact lines but only 3 commits — two lost index.lock. Nothing was lost
    # only because a later commit happened to sweep the earlier lines up, which
    # does not cover the last writer. An uncommitted event never folds:
    # read_all_events() reads committed state only.
    import concurrent.futures as cf
    N = 8
    host = m.HOSTS[0]

    def one(i):
        # check_rc=False: a lost commit must be OBSERVED and asserted on, not
        # raised as a harness error that hides which writers failed.
        return run([sys.executable, str(CODE / "emit.py"), "--no-nudge",
                    "--session", f"drill-conc-{i}",
                    "--kind", "lesson", "--subject", f"lesson/conc-{i}",
                    "--content", f"concurrent writer {i}"],
                   env=m.env(host), check_rc=False)

    with cf.ThreadPoolExecutor(max_workers=N) as ex:
        results = list(ex.map(one, range(N)))

    rc = [r.returncode for r in results]
    check(f"all {N} concurrent emits exited 0", all(c == 0 for c in rc), str(rc))

    log = m.dirs[host] / "events" / f"{host}.ndjson"
    lines = [x for x in log.read_text().splitlines() if x.strip()]
    check(f"all {N} lines on disk", len(lines) >= N, f"{len(lines)} lines")

    import json as _json
    bad = 0
    for x in lines:
        try:
            _json.loads(x)
        except ValueError:
            bad += 1
    check("no torn/interleaved line", bad == 0, f"{bad} unparseable")

    # The real assertion: every event is COMMITTED, not merely on disk.
    committed = m.git(host, "show", f"HEAD:events/{host}.ndjson").stdout
    missing = [i for i in range(N) if f"concurrent writer {i}" not in committed]
    check("every concurrent event reached HEAD", not missing, f"missing {missing}")

    status = m.git(host, "status", "--porcelain").stdout.strip()
    dirty = [l for l in status.splitlines() if "events/" in l]
    check("no event left uncommitted in the worktree", not dirty, str(dirty))


def drill_9(m):
    """The delivery gate: the harness index NEVER publishes over the loader's
    ceilings, and the on-demand tier never goes silent to make room.

    Regression drill for 2026-07-29: the writer bounded a SECTION and then
    appended an unmetered appendix, shipping 25,973 B against a 24,986 B
    ceiling every 5 minutes while reporting success. The promise "we bound our
    output" was asserted by nothing, so it was free to become false.
    """
    print("drill 9 — the harness index cannot be published over the ceiling")
    import mesh_lib as M

    head = ["# MEMORY — GENERATED", "# drill", ""]

    def rows(n, width=180):
        return [{"subject": f"lesson/s{i}", "content": "x" * width, "home": None,
                 "ts": "2026-07-29T00:00:00Z", "_suspect": False}
                for i in range(n)]

    # 1. An input far past the ceiling on BOTH axes still fits after the fit.
    text, rep = M.fit_harness_memory(head, rows(600), [f"slug-{i}" for i in range(600)])
    check("oversized input is fitted under the byte+line ceilings",
          not M.delivery_breach(text), str(M.delivery_breach(text)))

    # 2. The on-demand tier survives as an existence stub, never silence.
    check("on-demand tier keeps an existence stub when compacted",
          "on-demand" in text.lower(), text[-300:])

    # 3. Multibyte content is measured in BYTES, not characters.
    wide = [{"subject": "lesson/x", "content": "—" * 200, "home": None,
             "ts": "2026-07-29T00:00:00Z", "_suspect": False} for _ in range(400)]
    text2, _ = M.fit_harness_memory(head, wide, [])
    check("multibyte content is measured in bytes",
          not M.delivery_breach(text2), str(M.delivery_breach(text2)))

    # 4. A report never claims ok while the artifact breaches.
    check("report.ok is never true for a breaching artifact",
          not (rep["ok"] and M.delivery_breach(text)), str(rep))

    # 0. THE DRILL ITSELF must not be able to write the operator's brain.
    #    store_dir() derives from where the code lives, not from MESH_ROOT, so
    #    before the sandbox guard a drill fold published its test fixtures as
    #    the live always-on MEMORY.md until the next real fold repaired it.
    real = M.Path.home() / ".claude/projects"
    before = None
    for p in real.glob("*/memory/MEMORY.md"):
        before = (p, p.read_bytes())
        break
    check("a sandboxed fold refuses the operator's harness store",
          M.harness_store() is None if str(M.MESH_ROOT) != str(M.DEFAULT_MESH_ROOT)
          else True, f"MESH_ROOT={M.MESH_ROOT}")
    if before:
        check("the operator's MEMORY.md is byte-identical after the drill",
              before[0].read_bytes() == before[1], str(before[0]))

    # 5. The demotion note cannot push a section past its own cap.
    r = M.budgeted_rows(rows(400), head, cap=4000)
    check("the overflow note is reserved for, not appended past, the cap",
          sum(M.line_bytes(x) for x in r) <= 4000,
          str(sum(M.line_bytes(x) for x in r)))

    # 6. The alarm discriminates: trimming the SLUG LIST is healthy degradation
    #    and must stay quiet (it re-trims on every added memory), while dropping
    #    an always-on INDEX ROW must fire. A pager that cannot fire, and one
    #    that fires every run, fail the same way.
    _, quiet = M.fit_harness_memory(head, rows(20), [f"slug-{i}" for i in range(4000)])
    check("slug-list trimming alone does not raise an alarm",
          quiet["rows"] == quiet["rows_total"] and quiet["slugs"] < quiet["slugs_total"],
          str(quiet))
    _, loud = M.fit_harness_memory(head, rows(600), [])
    check("dropping always-on index rows is alarm-worthy",
          loud["rows"] < loud["rows_total"], str(loud))


def drill_10(m):
    """Lineage quarantine: an untrusted-lineage fact is never served, cannot
    disturb a served one, and only the operator's key promotes it.

    Regression drill for 2026-07-30: `contains-untrusted` was enforced at write
    time and IGNORED at fold time for the whole life of the feature. SPEC.md and
    the write-guard hook both documented a QUARANTINE.md that did not exist, and
    the first untrusted fact ever written went straight into the always-on index.
    The gate was prose on the half nobody tested.
    """
    print("drill 10 — untrusted lineage is quarantined, not served")
    m.make_signing_key()

    # Two scenarios at once:
    #   lesson/quar-probe — a trusted fact plus an untrusted one CONTRADICTING it
    #   lesson/quar-fresh — an untrusted fact on a subject nothing else claims
    m.emit("hosta", "--kind", "lesson", "--subject", "lesson/quar-probe",
           "--content", "the trusted version of the lesson")
    m.emit("hostb", "--kind", "lesson", "--subject", "lesson/quar-probe",
           "--lineage", "contains-untrusted",
           "--content", "the UNTRUSTED version, injected")
    m.emit("hostb", "--kind", "lesson", "--subject", "lesson/quar-fresh",
           "--lineage", "contains-untrusted",
           "--content", "a FRESH untrusted lesson, no rival claim")
    m.fold_all()

    def fold_of(h):
        env = m.env(h)
        out = run([sys.executable, "-c",
                   "import json,sys;sys.path.insert(0,%r);import mesh_lib as M;"
                   "e,_=M.read_all_events();f=M.fold_events(e,M.load_registry());"
                   "print(json.dumps({'live':[x['subject']+'|'+x['content'] for x in f['live']],"
                   "'quar':[x['id']+'|'+x['content'] for x in f['quarantined']],"
                   "'parked':sorted(f['parked']),'alarms':f['alarms']}))" % str(CODE)],
                  env=env)
        return json.loads(out.stdout)

    f = fold_of("hosta")
    served = [s for s in f["live"] if s.startswith("lesson/quar-")]
    check("neither untrusted fact is in the served set",
          all("UNTRUSTED" not in s and "FRESH" not in s for s in served), str(served))
    check("the trusted fact on the contested subject is still served",
          any("trusted version" in s for s in served), str(served))
    check("both untrusted facts ARE in the quarantine set",
          len(f["quar"]) == 2 and any("UNTRUSTED" in q for q in f["quar"])
          and any("FRESH" in q for q in f["quar"]), str(f["quar"]))
    # The security property: an untrusted claim must not be able to park real
    # doctrine. If it could, one crafted page would silence any fact by
    # disagreeing with it — poisoning by denial of service.
    check("an untrusted claim cannot park the subject it contradicts",
          "lesson/quar-probe" not in f["parked"], str(f["parked"]))
    check("the contradiction still raises an alarm",
          any("QUARANTINED" in a and "disagrees" in a for a in f["alarms"]),
          str(f["alarms"]))

    # The rendered views: held out of INDEX, present in QUARANTINE, and the
    # promotion command is stated where the operator will read it.
    idx = (m.dirs["hosta"] / "views" / "operator" / "INDEX.md").read_text()
    quar_f = m.dirs["hosta"] / "views" / "operator" / "QUARANTINE.md"
    check("QUARANTINE.md is materialized", quar_f.exists(), str(quar_f))
    check("neither untrusted fact appears in INDEX.md",
          "UNTRUSTED" not in idx and "FRESH" not in idx)
    qtext = quar_f.read_text() if quar_f.exists() else ""
    check("the untrusted facts are present in QUARANTINE.md",
          "UNTRUSTED" in qtext and "FRESH" in qtext)
    check("QUARANTINE.md states the promotion command",
          "--promote" in qtext, qtext[:200])

    # Promotion requires the key. sign.py is the only caller, and in the drill it
    # runs against a THROWAWAY key precisely because the real one is
    # passphrase-gated — which is what makes "an agent cannot promote" true.
    fresh = next(q for q in f["quar"] if "FRESH" in q).split("|")[0]
    before = m.version("hosta")
    run([sys.executable, str(CODE / "sign.py"), "--promote", fresh,
         "--session", "drill-sign"], env=m.env("hosta"))
    m.fold_all()
    f2 = fold_of("hosta")
    check("after promotion the content IS served",
          any("FRESH" in s for s in f2["live"]), str(f2["live"]))
    check("promotion clears only the promoted event",
          len(f2["quar"]) == 1 and any("UNTRUSTED" in q for q in f2["quar"]),
          str(f2["quar"]))
    check("promotion moves view.version (folded state changed)",
          m.version("hosta") != before)

    # And it propagates: promotion is an event, so every host serves it.
    fc = fold_of("hostc")
    check("the promotion propagates to a peer",
          any("FRESH" in s for s in fc["live"]), str(fc["live"]))

    # Promoting INTO a served subject must warn rather than silently replace.
    contested = next(q for q in f["quar"] if "UNTRUSTED" in q).split("|")[0]
    r = run([sys.executable, str(CODE / "sign.py"), "--promote", contested,
             "--session", "drill-sign"], env=m.env("hosta"))
    check("promoting over a served fact warns about the clash",
          "WARNING" in r.stderr and "not being superseded" in r.stderr.lower(),
          r.stderr[:300])
    m.fold_all()
    f3 = fold_of("hosta")
    check("the unsuperseded clash PARKS the subject rather than winning quietly",
          "lesson/quar-probe" in f3["parked"], str(f3["parked"]))


def drill_12(m):
    """The pin overlay: an ALREADY-WRITTEN memory can be made resident without
    rewriting it, a pin that protects nothing says so, and the naive fix stays
    proven-broken.

    Built 2026-07-31. Residency is decided by score_for_index, whose first key
    is `pin` — a flag that could only ever be set at emit. So the hard prompt-
    only boundaries (no-auto-MFA, never-announce-session-endings) held their
    always-on slots by ranking luck, and nothing would have announced their
    eviction. Check 4 is the load-bearing one: it pins the reason this is an
    overlay and not the obvious re-emit.
    """
    print("drill 12 — pin overlay: residency without rewriting the memory")

    def fold_of(h):
        out = run([sys.executable, "-c",
                   "import json,sys;sys.path.insert(0,%r);import mesh_lib as M;"
                   "e,_=M.read_all_events();f=M.fold_events(e,M.load_registry());"
                   "r=M.ranked_index(f,'operator');"
                   # The pin FLAG, not the ranking position. Position is a
                   # confounded instrument here: drill emits land in the same
                   # second, so ts ties and sorted() falls back to log order —
                   # the first version of this drill read that artifact as both
                   # a broken control and a security failure that did not exist.
                   "print(json.dumps({'ranked':[x['subject'] for x in r],"
                   "'pinned':sorted({x['subject'] for x in f['live'] "
                   "if x.get('_pin') or x.get('pin')}),"
                   "'rows':[M.index_row(x) for x in r],"
                   "'live':[x['subject']+'|'+x['content'] for x in f['live']],"
                   "'ts':{x['subject']:x['ts'] for x in f['live']},"
                   "'ids':{x['subject']:x['id'] for x in f['live']},"
                   "'pinned_bytes':f['pinned_bytes'],"
                   "'pin_cap':int(M.DELIVERY_BYTES*M.PIN_DELIVERY_SHARE),"
                   "'alarms':f['alarms']}))" % str(CODE)], env=m.env(h))
        return json.loads(out.stdout)

    # A boundary lesson written days ago, and a pile of newer lessons that would
    # out-rank it on recency — the real shape, where the tail is evicted by
    # arithmetic every fold.
    # A boundary lesson, and a pile of lessons that out-rank it the way the
    # audit found real ones do — on CORRECTION HISTORY, i.e. how often a lesson
    # had to be re-corrected, which is not the same thing as how load-bearing it
    # is. Recency cannot be the fixture's lever: drill emits share a timestamp.
    m.emit("hosta", "--kind", "lesson", "--subject", "lesson/pin-boundary",
           "--content", "never read an OTP from the inbox to complete a login")
    for i in range(6):
        m.emit("hosta", "--kind", "lesson", "--subject", f"lesson/pin-noise{i}",
               "--content", f"a lesson {i} that had to be re-corrected")
        m.emit("hosta", "--kind", "correct", "--subject", f"lesson/pin-noise{i}",
               "--content", f"correction {i} — raises this subject's rank")
    m.fold_all()
    before = fold_of("hosta")
    check("unpinned, the boundary is out-ranked by re-corrected lessons",
          before["ranked"][0] != "lesson/pin-boundary", str(before["ranked"][:3]))
    check("the boundary starts unpinned", "lesson/pin-boundary" not in before["pinned"],
          str(before["pinned"]))

    # 1. The pin promotes it to the top tier.
    m.emit("hosta", "--kind", "pin", "--subject", "lesson/pin-boundary",
           "--content", "prompt IS the mechanism; eviction must never be silent")
    m.fold_all()
    after = fold_of("hosta")
    check("a pin event promotes an already-written memory to the top tier",
          after["ranked"][0] == "lesson/pin-boundary", str(after["ranked"][:3]))
    check("the overlay marks exactly the pinned subject",
          after["pinned"] == ["lesson/pin-boundary"], str(after["pinned"]))

    # 2. The memory is UNTOUCHED: same id, same ts, same content. This is the
    #    whole reason for an overlay — a supersede-and-replace would have
    #    restamped a 07-28 lesson as learned today, corrupting the one signal
    #    index_row's date exists to carry.
    check("the pinned memory keeps its original id",
          after["ids"]["lesson/pin-boundary"] == before["ids"]["lesson/pin-boundary"],
          f'{before["ids"]["lesson/pin-boundary"]} -> {after["ids"]["lesson/pin-boundary"]}')
    check("the pinned memory keeps its original timestamp",
          after["ts"]["lesson/pin-boundary"] == before["ts"]["lesson/pin-boundary"],
          f'{before["ts"]["lesson/pin-boundary"]} -> {after["ts"]["lesson/pin-boundary"]}')

    # 3. The pin event itself is bookkeeping, never a served row.
    check("the pin event never renders as an index row",
          not any("prompt IS the mechanism" in r for r in after["rows"]),
          str([r for r in after["rows"] if "mechanism" in r]))

    # 4. REGRESSION — why this is an overlay and not the obvious "re-emit the
    #    lesson with --pin". All three re-emit paths are exercised against the
    #    fold directly, because routing them through emit.py cannot show it:
    #    event_id() hashes (host, session, ts, content, kind, subject) and
    #    EXCLUDES pin and supersedes, so a same-second re-emit is deduped on
    #    append and the check would pass without the fold ever deciding
    #    anything. The first version of this drill did exactly that and read
    #    the dedup as proof of collapse-to-earliest.
    reemit = run([sys.executable, "-c", """
import sys, json; sys.path.insert(0, %r)
import mesh_lib as M
reg = M.load_registry(); C = "a boundary worth keeping resident"
def ev(**kw):
    d = dict(kind="lesson", subject="lesson/pin-probe", content=C,
             session="s1", ts="2026-07-28T00:00:00Z", pin=False, supersedes=None)
    d.update(kw)
    e, _ = M.make_event(d.pop("kind"), d.pop("subject"), d.pop("content"), **d)
    return e
orig = ev()
def live(evs):
    f = M.fold_events(evs, reg)
    return [x for x in f["live"] if x["subject"] == "lesson/pin-probe"], sorted(f["parked"])
a, _ = live([orig, ev(session="s2", ts="2026-07-31T00:00:00Z", pin=True)])
b, _ = live([orig, ev(session="s2", ts="2026-07-31T00:00:00Z", pin=True,
                      supersedes=[orig["id"]])])
_, cpark = live([orig, ev(session="s2", ts="2026-07-31T00:00:00Z", pin=True,
                          content=C + " (pinned)")])
print(json.dumps({
    "a_pinned": bool(a and (a[0].get("pin") or a[0].get("_pin"))),
    "b_pinned": bool(b and (b[0].get("pin") or b[0].get("_pin"))),
    "b_ts": b[0]["ts"] if b else None, "orig_ts": orig["ts"],
    "b_id_changed": bool(b and b[0]["id"] != orig["id"]),
    "c_parked": "lesson/pin-probe" in cpark}))
""" % str(CODE)], env=m.env("hosta"))
    r = json.loads(reemit.stdout)
    check("re-emit WITHOUT supersede is silently discarded — the pin does "
          "nothing (collapse-to-earliest)", not r["a_pinned"], str(r))
    check("re-emit WITH supersede pins, but forges the record: the memory is "
          "restamped to today and gets a new id",
          r["b_pinned"] and r["b_ts"] != r["orig_ts"] and r["b_id_changed"],
          str(r))
    check("re-emit with DIFFERING content PARKS the subject — the boundary "
          "would leave the served index entirely", r["c_parked"], str(r))

    # And the overlay does none of those three things — proven by the id/ts
    # checks above, which is what makes this the mechanism rather than a taste
    # preference over the re-emit.

    # 5. A pin naming a subject nobody serves must alarm, not fail silent. A pin
    #    that protects nothing is indistinguishable from one that works.
    m.emit("hosta", "--kind", "pin", "--subject", "lesson/pin-typoo",
           "--content", "pin against a subject that does not exist")
    m.fold_all()
    dangle = fold_of("hosta")
    check("a pin that protects nothing raises an alarm",
          any("protects nothing" in a and "pin-typoo" in a for a in dangle["alarms"]),
          str(dangle["alarms"]))

    # 6. Unpin needs no new verb: retract the pin event and the overlay lifts.
    pin_id = run([sys.executable, "-c",
                  "import sys;sys.path.insert(0,%r);import mesh_lib as M;"
                  "e,_=M.read_all_events();"
                  "print([x['id'] for x in e if x['kind']=='pin' and "
                  "x['subject']=='lesson/pin-boundary'][0])" % str(CODE),
                  ], env=m.env("hosta")).stdout.strip()
    m.emit("hosta", "--kind", "retract", "--subject", "lesson/pin-boundary",
           "--supersedes", pin_id, "--content", "unpin: no longer prompt-only")
    m.fold_all()
    unpinned = fold_of("hosta")
    check("retracting the pin event lifts the overlay",
          "lesson/pin-boundary" not in unpinned["pinned"], str(unpinned["pinned"]))
    check("the memory itself survives the unpin",
          any(s.startswith("lesson/pin-boundary|") for s in unpinned["live"]),
          str([s for s in unpinned["live"] if "pin-boundary" in s]))

    # 7. An untrusted-lineage pin cannot buy residency. Pinning does not make
    #    content trusted, and it must not be a side door around the quarantine:
    #    otherwise one crafted page could hold a permanent always-on slot.
    m.emit("hostb", "--kind", "lesson", "--subject", "lesson/pin-untrusted",
           "--content", "a lesson distilled from an ingested page")
    m.emit("hostb", "--kind", "pin", "--subject", "lesson/pin-untrusted",
           "--lineage", "contains-untrusted",
           "--content", "pin emitted with untrusted lineage")
    m.fold_all()
    unt = fold_of("hostb")
    check("an untrusted-lineage pin does not take effect",
          "lesson/pin-untrusted" not in unt["pinned"], str(unt["pinned"]))
    # Exact, not `<=`. A subset assertion passes on the empty set and would also
    # have passed if pin-boundary were still pinned after its retract — it locks
    # nothing. Everything pinned in this drill has been retracted or refused by
    # now, so the expected set is empty and saying so is the whole check.
    check("the pin overlay marks exactly the expected set, no more",
          unt["pinned"] == [], str(unt["pinned"]))

    # 8. THE CAP IS A GATE, NOT AN ALARM. Pins are uncontested residency and an
    #    agent can emit one, so the first version's alarm-only bound left an
    #    unmetered write path into the file every session loads. Flood the tier
    #    and assert that excess pins are REFUSED, that the refusal is loud, and
    #    — the security property — that the flood does not displace a boundary
    #    that was already resident.
    m.emit("hosta", "--kind", "lesson", "--subject", "lesson/pin-incumbent",
           "--content", "the boundary that was here first and must stay")
    m.emit("hosta", "--kind", "pin", "--subject", "lesson/pin-incumbent",
           "--content", "pinned before the flood")
    m.fold_all()
    seated = fold_of("hosta")
    check("the incumbent boundary is pinned before the flood",
          "lesson/pin-incumbent" in seated["pinned"], str(seated["pinned"]))

    for i in range(120):
        m.emit("hosta", "--kind", "lesson", "--subject", f"lesson/pin-flood{i}",
               "--content", "x" * 200)
        m.emit("hosta", "--kind", "pin", "--subject", f"lesson/pin-flood{i}",
               "--content", "an agent pinning its own lesson " + "y" * 100)
    m.fold_all()
    flood = fold_of("hosta")
    check("excess unsigned pins are REFUSED, not merely alarmed about",
          any("REFUSED" in a for a in flood["alarms"]), str(flood["alarms"])[:400])
    check("the flood does not displace the incumbent boundary (oldest-first "
          "admission is the security property)",
          "lesson/pin-incumbent" in flood["pinned"],
          str(flood["pinned"])[:300])
    check("the pinned tier is held under its byte cap",
          flood["pinned_bytes"] <= flood["pin_cap"],
          f'{flood["pinned_bytes"]} B vs cap {flood["pin_cap"]} B')
    check("not every flood pin was admitted",
          len(flood["pinned"]) < 120, str(len(flood["pinned"])))

    # 9. The pin state is discoverable: PINS.md names the id needed to retract.
    pins_md = (m.dirs["hosta"] / "views" / "operator" / "PINS.md")
    check("PINS.md exists and names a pin event id for the operator to retract",
          pins_md.exists() and "pin id" in pins_md.read_text()
          and "lesson/pin-incumbent" in pins_md.read_text(),
          pins_md.read_text()[:200] if pins_md.exists() else "missing")


def drill_13(m):
    """SPEC v4 — residency, projection, and the confidentiality boundary.

    Every check here pairs a REFUSAL with the write that must still succeed:
    a gate that refuses everything passes a one-sided test while breaking the
    system, and 'no bad thing happened' is only evidence once the instrument
    is shown able to let the good thing through.
    """
    print("\n== drill 13: SPEC v4 residency + projection ==")
    sys.path.insert(0, str(CODE))
    import mesh_lib as M

    # --- the unsigned cap is applied at READ time, not just at write time
    ev, _ = M.make_event("lesson", "lesson/x", "c", session="s",
                         residency="doctrine", hook="h", body="b")
    remote = dict(ev, host="some-other-host")
    check("13.1 REMOTE unsigned doctrine READS as state",
          M.effective_residency(remote) == "state", M.effective_residency(remote))
    check("13.2 and is reported as capped, not silently downgraded",
          M.residency_capped(remote))
    check("13.3 POSITIVE CONTROL: LOCAL unsigned doctrine is allowed "
          "(the local door is the trust boundary)",
          M.effective_residency(ev) == "doctrine", M.effective_residency(ev))
    check("13.3b POSITIVE CONTROL: signed remote doctrine travels",
          M.effective_residency(dict(remote, _signed=True)) == "doctrine")
    pin_remote = dict(ev, residency="pinned", host="some-other-host")
    check("13.3c unsigned PINNED caps even locally (signature or nothing)",
          M.effective_residency(dict(ev, residency="pinned")) == "state"
          and M.effective_residency(pin_remote) == "state")

    # --- hook bound: refuse, never truncate
    r = m.emit("hosta", "--kind", "lesson", "--subject", "lesson/hooktest",
               "--content", "c", "--body", "b", "--hook", "x" * 141,
               check_rc=False)
    check("13.4 over-long hook REFUSED (not truncated)",
          r.returncode != 0 and "over the 140" in (r.stdout + r.stderr))
    r = m.emit("hosta", "--kind", "lesson", "--subject", "lesson/hooktest",
               "--content", "c", "--body", "b", "--hook", "x" * 140,
               check_rc=False)
    check("13.5 POSITIVE CONTROL: a 140-char hook is accepted",
          r.returncode == 0, (r.stdout + r.stderr)[:120])

    # --- A2: a family-audience body must never enter the replicated log
    r = m.emit("hosta", "--kind", "lesson", "--subject", "lesson/privatetest",
               "--content", "c", "--audience", "family", "--body", "SECRETBODY",
               "--hook", "h", check_rc=False)
    check("13.6 family-audience body REFUSED at the producer",
          r.returncode != 0 and "may not carry a body" in (r.stdout + r.stderr))
    log = m.dirs["hosta"] / "events" / "hosta.ndjson"
    blob = log.read_text() if log.exists() else ""
    check("13.7 and the secret is byte-level absent from the log",
          "SECRETBODY" not in blob)

    # --- ghost hole. Tested against the DECISION function with a real temp
    # store: routing this through emit.py would exercise a gate the sandbox
    # guard has switched off, and pass while proving nothing.
    gstore = m.root / "store13g"
    gstore.mkdir()
    check("13.8 body-less lesson with no store file is refused (ghost)",
          "GHOST" in (M.ghost_refusal_reason("lesson", "lesson/ghosttest",
                                             None, gstore) or ""))
    (gstore / "ghosttest.md").write_text("body lives here")
    check("13.8b POSITIVE CONTROL: allowed once the store file exists",
          M.ghost_refusal_reason("lesson", "lesson/ghosttest", None, gstore) is None)
    check("13.8c POSITIVE CONTROL: allowed when the event carries a body",
          M.ghost_refusal_reason("lesson", "lesson/other", "b", gstore) is None)
    check("13.8d sandbox guard: no store resolved => gate stands down",
          M.ghost_refusal_reason("lesson", "lesson/other", None, None) is None)

    # --- projection asymmetry: create from grandfather, never overwrite
    store = m.root / "store13"
    store.mkdir()
    grand, _ = M.make_event("lesson", "lesson/grand", "grandfathered text",
                            session="s")
    withbody, _ = M.make_event("lesson", "lesson/withbody", "desc",
                               session="s", hook="h", body="BODY-IS-TRUTH")
    fake = {"live": [grand, withbody], "quarantined": [], "parked": []}
    keep = store / "grand.md"
    keep.write_text("A FULL HAND-WRITTEN BODY")
    out = M.project_store(fake, store, apply=True)
    check("13.9 grandfathered event does NOT overwrite an existing file",
          keep.read_text() == "A FULL HAND-WRITTEN BODY", keep.read_text()[:40])
    check("13.10 POSITIVE CONTROL: body-carrying event DOES create its file",
          (store / "withbody.md").read_text() == "BODY-IS-TRUTH")
    # ghost repair: grandfather with NO file becomes a reconstructed stub
    (store / "grand.md").unlink()
    out = M.project_store(fake, store, apply=True)
    check("13.11 grandfathered event with no file IS reconstructed",
          (store / "grand.md").exists()
          and M.RECONSTRUCTED_MARK in (store / "grand.md").read_text())
    # OVERWRITE is the destructive branch, so it needs authority. An unsigned
    # event must NOT be able to replace a memory's body: that would make "the
    # event is canonical" a forge primitive (append a lesson on a victim slug,
    # supersede the priors, wait for --project). Found by Grok's review of the
    # implementation, 2026-07-31.
    (store / "withbody.md").write_text("HAND-WRITTEN, NOT THE EVENT")
    out = M.project_store(fake, store, apply=True)
    check("13.12 UNSIGNED divergent event does NOT overwrite the file",
          (store / "withbody.md").read_text() == "HAND-WRITTEN, NOT THE EVENT")
    check("13.13 and the refusal ALARMS with both resolutions named",
          any("LEFT ALONE" in a and "adopt" in a for a in out["alarms"]),
          str(out["alarms"])[:160])
    # 13.13a — the advice must name a command that EXISTS and ACCEPTS this
    # case. From 2026-07-31 to 2026-08-01 the alarm told the operator to run
    # `adopt <slug>`, which refuses any slug whose event already carries a
    # body — i.e. every divergence it was printed for. Four alarms repeated
    # every fold with a resolution that could not work, while the only other
    # path (sign the event) would have overwritten a correction with the stale
    # text it corrected. An alarm whose remedy is untested is a rumour.
    _sp = subprocess
    _mw = Path.home() / "{{REDACTED}}/cc-skills/improve/memory_write.py"
    _advice = next((a for a in out["alarms"] if "LEFT ALONE" in a), "")
    _flags = [t for t in _advice.split() if t.startswith("--")]
    _help = _sp.run([sys.executable, str(_mw), "adopt", "--help"],
                    capture_output=True, text=True, timeout=60).stdout
    check("13.13a every flag the alarm prescribes is real in `adopt --help`",
          bool(_flags) and all(f.rstrip(",.") in _help for f in _flags),
          f"prescribed {_flags}, adopt accepts "
          f"{[t for t in _help.split() if t.startswith('--')]}")
    withbody["_signed"] = True
    out = M.project_store(fake, store, apply=True)
    check("13.13b POSITIVE CONTROL: a SIGNED tip does repair the file",
          (store / "withbody.md").read_text() == "BODY-IS-TRUTH")
    check("13.13c and the repair is alarmed, not silent",
          any("diverged" in a for a in out["alarms"]))
    withbody["_signed"] = False
    # A2 at the projector: a family-audience body must never reach the
    # operator store even if such an event somehow exists in the log.
    leak = dict(withbody, subject="lesson/leaky", audience="family",
                body="PRIVATE")
    out = M.project_store({"live": [leak]}, store, apply=True)
    check("13.16 projector REFUSES a body from a non-fleet audience",
          not (store / "leaky.md").exists()
          and any("refusing to project a body" in a for a in out["alarms"]))
    # path safety: a subject is a slug, but this is where it becomes a path
    esc = dict(withbody, subject="lesson/../escaped", body="X")
    out = M.project_store({"live": [esc]}, store, apply=True)
    check("13.17 projector REFUSES a slug that would escape the store",
          not (store.parent / "escaped.md").exists()
          and any("unsafe slug" in a for a in out["alarms"]))
    (store / "orphan.md").write_text("keep me")
    out = M.project_store(fake, store, apply=True)
    check("13.14 projection NEVER deletes a file with no event",
          (store / "orphan.md").exists()
          and (store / "orphan.md").read_text() == "keep me")
    check("13.15 the orphan is REPORTED as an inverse ghost, not removed",
          "orphan" in out["inverse_ghosts"], str(out["inverse_ghosts"]))


def drill_14(m):
    """The residency gate and the admission gate — both halves, both directions.

    A gate is only evidence when it is shown REFUSING the bad case AND passing
    the good one; each check here is paired for that reason.
    """
    print("\n== drill 14: residency gate + admission gate ==")
    sys.path.insert(0, str(CODE))
    import mesh_lib as M

    live = Path(m.root) / "MEMORY.md"
    live.write_text("# h\n- [lesson/a] one\n- [lesson/b] two\n")
    same = "# h\n- [lesson/a] one\n- [lesson/b] two\n"
    check("14.1 identical row set is NOT a residency delta",
          M.residency_delta(live, same) is None)
    check("14.2 reworded row is NOT a residency delta (else the gate is routine)",
          M.residency_delta(live, same.replace("one", "REWORDED")) is None)
    check("14.3 an ADDED row is a residency delta",
          (M.residency_delta(live, same + "- [lesson/c] three\n") or {}
           ).get("added") == ["lesson/c"])
    check("14.4 a DROPPED row is a residency delta",
          (M.residency_delta(live, "# h\n- [lesson/a] one\n") or {}
           ).get("dropped") == ["lesson/b"])
    check("14.5 a fresh host with no live index is not blocked",
          M.residency_delta(Path(m.root) / "absent.md", same) is None)

    # --- admission: the refusals, and the write that must still get through
    check("14.6 a rule that fits is ADMITTED",
          M.admission_reject("a rule short enough to bind") is None)
    check("14.7 content trailing off mid-sentence is REFUSED",
          M.admission_reject("a rule that runs out of…") is not None)
    check("14.8 ASCII ellipsis is refused too (the stumps used both)",
          M.admission_reject("a rule that runs out of...") is not None)
    check("14.9 content over the renderer's cut is REFUSED, not silently cut",
          M.admission_reject("x" * (M.INDEX_CONTENT_CHARS + 1)) is not None)
    check("14.10 content exactly at the cut is admitted (off-by-one)",
          M.admission_reject("x" * M.INDEX_CONTENT_CHARS) is None)
    check("14.11 empty content is REFUSED", M.admission_reject("") is not None)

    # --- the producer gate at the funnel: refusal, carve-out, and scope
    def _mk(content, **kw):
        try:
            M.make_event("lesson", "lesson/t", content, session="s", **kw)
            return "made"
        except ValueError:
            return "refused"
    check("14.12 make_event REFUSES a stumped lesson", _mk("trails off…") == "refused")
    check("14.13 carry_forward=True re-emits a legacy stump (retag carve-out)",
          _mk("trails off…", carry_forward=True) == "made")
    check("14.14 clean lesson content still passes the funnel",
          _mk("a rule short enough to bind") == "made")
    check("14.15 non-lesson kinds are NOT gated (state/correct flow free)",
          (lambda: (M.make_event("assert", "s/t", "x" * 300, session="s",
                                 home="FLEET.md") and True))() is True)

    # --- projection drift: all three classes, and the clean case
    droot = Path(m.root) / "dstore"; droot.mkdir()
    def _mem(slug, desc):
        (droot / f"{slug}.md").write_text(
            f"---\nname: {slug}\ndescription: {desc}\n---\nbody\n")
    _mem("clean", "same essence")
    _mem("richer", "Short stump plus the tail the derivative lost")
    _mem("poorer", "Short")
    _mem("forked", "an entirely different story")
    fake = {"live": [
        {"kind": "lesson", "subject": "lesson/clean", "content": "same essence"},
        {"kind": "lesson", "subject": "lesson/richer", "content": "Richer: Short stump…"},
        {"kind": "lesson", "subject": "lesson/poorer", "content": "Short plus more than the file has"},
        {"kind": "lesson", "subject": "lesson/forked", "content": "the event's own tale"},
        {"kind": "lesson", "subject": "lesson/v4", "content": "stump…", "body": "full"},
        {"kind": "lesson", "subject": "lesson/ghost", "content": "no file at all"},
    ]}
    d = M.projection_drift(fake, droot)
    check("14.16 identical essence is NOT drift", "lesson/clean" not in sum(d.values(), []))
    check("14.17 file extending the event = FILE-RICHER (even past a Title: head)",
          d["file_richer"] == ["lesson/richer"])
    check("14.18 event extending the file = EVENT-RICHER",
          d["event_richer"] == ["lesson/poorer"])
    check("14.19 two stories = DISJOINT", d["disjoint"] == ["lesson/forked"])
    check("14.20 v4 body-carrying tips are exempt (projection repairs those)",
          "lesson/v4" not in sum(d.values(), []))
    check("14.21 a missing file is the ghost repair's problem, not drift",
          "lesson/ghost" not in sum(d.values(), []))

    # --- the claim made to the operator 2026-07-31 and then corrected: verify
    # the CORRECTED version stays true. project_store must never replace an
    # existing file from a grandfathered (body-less) event — that file may be
    # the only lossless copy ([[assert-every-promise-not-the-convenient-one]]).
    before = (droot / "richer.md").read_text()
    M.project_store({"live": [{"kind": "lesson", "subject": "lesson/richer",
                               "content": "Richer: Short stump…", "id": "x",
                               "ts": "2026-07-31T00:00:00Z"}]}, droot, apply=True)
    check("14.22 projection leaves a grandfathered tip's existing file ALONE",
          (droot / "richer.md").read_text() == before)


def drill_15(m):
    """The DELIVERY seam: what retrieval actually serves.

    Every other drill stops at the fold's verdict. This one runs the production
    consumer, because the verdict being right is not the property that matters —
    the property is that nothing the fold retired reaches a turn.

    That gap was not hypothetical. Until 2026-07-31 `retrieve.py` globbed the
    store and consulted no lifecycle state at all: quarantined untrusted-lineage
    facts and superseded doctrine both rode in labelled "STANDING RULES", and a
    live session was served a quarantined subject while reviewing this very
    system. Every drill passed throughout, because `grep -c retrieve drill.py`
    was 0 — the suite proved the fold and never proved the delivery.

    Deliberately NOT using Mesh.env: those subprocesses get a temp MESH_ROOT so
    `harness_store()` returns None and the real store is unreachable by
    construction. That sandbox guard is correct, and it is also exactly why this
    seam was untestable. So the store is passed in directly instead.
    """
    print("drill 15 — retrieval serves ONLY what the fold still stands behind")
    sys.path.insert(0, str(CODE))
    import retrieve as R

    droot = Path(m.root) / "store15"
    droot.mkdir(exist_ok=True)
    body = ("---\nname: {n}\ndescription: {d}\n---\n\n{d}\n")
    # Same distinctive term in all four, so scoring cannot be what separates
    # them — only the manifest can. Without this the test could pass by luck.
    TERM = "zorbfeed"
    for name, desc in [
            ("live-rule", f"{TERM} handling is governed by this live rule"),
            ("quarantined-rule", f"{TERM} handling per an untrusted ingested page"),
            ("superseded-rule", f"{TERM} handling by a rule that was corrected"),
            ("parked-rule", f"{TERM} handling, contradicted and parked")]:
        (droot / f"{name}.md").write_text(body.format(n=name, d=desc))

    # The manifest the fold would publish: only the live subject survives.
    # Passed EXPLICITLY: `servable()` must not fall back to the host's real
    # manifest, or 15.6 would pass for the wrong reason (this query matches
    # nothing in the real corpus either, so "empty" would prove nothing).
    man = droot / "servable.json"
    man.write_text(json.dumps(
        {"version": 1, "view_version": "drill", "generated": "2026-07-31T00:00:00Z",
         "slugs": ["live-rule"]}))

    hits = R.retrieve(f"how should I handle {TERM} today", store=droot, k=5,
                      manifest=man)
    slugs = {s for _, s, _ in hits}
    check("15.1 the live rule IS served (positive control)", "live-rule" in slugs,
          f"got {slugs}")
    check("15.2 a QUARANTINED rule is never served", "quarantined-rule" not in slugs,
          f"got {slugs}")
    check("15.3 a SUPERSEDED rule is never served", "superseded-rule" not in slugs,
          f"got {slugs}")
    check("15.4 a PARKED rule is never served", "parked-rule" not in slugs,
          f"got {slugs}")

    # Held-out docs must not reach the SCORER either: idf is computed over the
    # corpus, so a doc that is merely dropped at render time still perturbs
    # ranking and can displace a legitimate hit without ever appearing.
    docs = R.corpus(droot, R.servable(path=man))
    check("15.5 held-out docs are absent from the scored corpus",
          {d[0] for d in docs} == {"live-rule"}, f"got {[d[0] for d in docs]}")

    # No manifest => the fold's verdict is UNKNOWN. Serving unfiltered is the
    # defect this drill exists to prevent, so the closed direction is the safe
    # one here — unlike the module's outer exception handler, which fails open.
    man.unlink()
    check("15.6 a MISSING manifest suppresses recall (fails closed)",
          R.retrieve(f"how should I handle {TERM} today", store=droot, k=5,
                     manifest=man) == [])

    # And the suppression must be legible: a silently-empty channel and a
    # deliberately-withheld one must not look identical.
    check("15.7 suppression is advertised, not silent",
          R.servable(path=man) is None)


def main():
    sel = sys.argv[1:] or ["1", "2", "3", "5", "7", "8", "9", "10", "11", "12", "13", "14", "15", "6", "4"]  # 4 last (diverges hosta)
    with tempfile.TemporaryDirectory() as root:
        m = Mesh(root)
        for d in sel:
            globals()[f"drill_{d}"](m)
    if FAILS:
        print(f"\nDRILLS FAILED: {len(FAILS)} — " + ", ".join(FAILS))
        return 1
    if SKIPS:
        # NOT a pass. An unattempted proof is an open obligation, and saying so
        # is the whole difference between "verified" and "nothing went wrong".
        print(f"\nDRILLS INCOMPLETE: {len(SKIPS)} proof obligation(s) not "
              "attempted on this host —")
        for s in SKIPS:
            print(f"  · {s}")
        return 2
    print("\n✓ all selected drills pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
