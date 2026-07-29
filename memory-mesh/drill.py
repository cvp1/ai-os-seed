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


def main():
    sel = sys.argv[1:] or ["1", "2", "3", "5", "7", "8", "6", "4"]  # 4 last (diverges hosta)
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
