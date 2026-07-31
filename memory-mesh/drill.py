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


def check(name, cond, detail=""):
    tag = "ok" if cond else "FAIL"
    print(f"  {tag}: {name}" + ("" if cond else f" — {detail}"))
    if not cond:
        FAILS.append(name)


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

    def emit(self, h, *args):
        return run([sys.executable, str(CODE / "emit.py"), "--no-nudge",
                    "--session", f"drill-{h}", *args], env=self.env(h))

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
        print(f"  SKIP: signing unavailable ({(r.stderr or r.stdout).strip()[:60]})")
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
        print("  SKIP: no ssh-agent on this host")
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
        print(f"  SKIP: could not load the probe key into an agent ({out})")
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
    check("the pin overlay is applied to exactly the subjects pinned by a "
          "TRUSTED pin event, and nothing else",
          set(unt["pinned"]) <= {"lesson/pin-boundary"}, str(unt["pinned"]))


def main():
    sel = sys.argv[1:] or ["1", "2", "3", "5", "7", "8", "9", "10", "11", "12", "6", "4"]  # 4 last (diverges hosta)
    with tempfile.TemporaryDirectory() as root:
        m = Mesh(root)
        for d in sel:
            globals()[f"drill_{d}"](m)
    if FAILS:
        print(f"\nDRILLS FAILED: {len(FAILS)} — " + ", ".join(FAILS))
        return 1
    print("\n✓ all selected drills pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
