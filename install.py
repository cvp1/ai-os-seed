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

Stdlib only. Refuses to overwrite a non-empty target; uninstall de-schedules
managed jobs before deleting anything and refuses a target that doesn't look
like one of ours.

Every install writes <ROOT>/.cc-seed/receipt.json (O_EXCL-created) with a
pre-write baseline and approval records for gated writes. --approve is the
only path that performs a gated write; --audit compares live state against
the receipt and the package manifest (see docs/install-audit.md).
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
              "session-brief", "friction-miner", "mcp-guard"]
# Opt-in only: governance/ is installed only by --enable-governance.
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

# Default job: daily hygiene sweep of this workspace. --findings-exit0 makes
# found work exit 0 (scheduler/CONVENTIONS.md rule 1).
JOB_REPO_HYGIENE = """\
  - name: repo_hygiene
    schedule: "30 6 * * *"
    command: >-
      /usr/bin/python3 {root}/observability/log_run.py --job repo_hygiene --
      /usr/bin/python3 {root}/observability/repo_hygiene.py --root {root} --findings-exit0
"""

# Default job: the freshness backstop over every other job. Runs after
# repo_hygiene so that result is already in runs.db; --write-findings leaves a
# report the agent reads at session start.
JOB_FRESHNESS = """\
  - name: freshness
    schedule: "15 7 * * *"
    command: >-
      /usr/bin/python3 {root}/observability/log_run.py --job freshness --
      /usr/bin/python3 {root}/observability/freshness.py --write-findings
"""


# Default job: the fold runs on its own timer outside the scheduler, so this
# job (run under the scheduler) watches the fold, served index, hook wiring
# and peers.
JOB_MESH_WATCH = """\
  - name: mesh_watch
    schedule: "25 * * * *"
    command: >-
      /usr/bin/python3 {root}/observability/log_run.py --job mesh_watch --
      /usr/bin/python3 {root}/memory-mesh/fold_watch.py
"""


def _add_job(manifest: Path, job_name: str, block: str) -> bool:
    """Add one job's YAML block to scheduler/manifest.yml, idempotently.

    Presence is an exact, comment-excluded line match (so `repo_hygiene_backup`
    doesn't read as `repo_hygiene`). Appends after existing jobs and writes
    atomically. Returns True if the job was newly added."""
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

# --- receipt / baseline / gated-write constants ----------------------------
CC_SEED_DIR = ".cc-seed"
RECEIPT_NAME = "receipt.json"
STAGED_DIR = "staged"
GATED_WRITES = {"claude-md", "mesh-bootstrap", "import-pack", "memory-hooks"}
REVOCABLE_WRITES = {"memory-hooks"}
MARKER_START = "<!-- cc-seed:start -->"
MARKER_END = "<!-- cc-seed:end -->"
_MAX_HASH_BYTES = 200 * 1024 * 1024  # don't hash unbounded files

# --- cc-pack import: the gated write path ----------------------------------
# A pack (verified by pack/import_pack.py) is applied outside --target's git
# tree, into _pack_delivery_root(). CLAUDE.md gets at most one pointer line,
# at the START of the file, so the cc-seed claude-md region stays last.
PACKS_MARKER_START = "<!-- cc-pack:start -->"
PACKS_MARKER_END = "<!-- cc-pack:end -->"
# \A/\Z, not ^/$: Python's $ also matches just before a trailing newline.
_PACK_SAFE_COMPONENT_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def _pack_is_safe_relpath(rel):
    """Duplicate of cc-pack/pack_lib.py's is_safe_relpath (cc-pack isn't
    shipped). Must run before any path join: Path / discards the left side
    when the right side is absolute."""
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
    this machine. Always exits 0 — it reports, never decides."""
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


# --- small primitives ------------------------------------------------------

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
    """Escape C0/C1 control characters so a filename can't forge output lines."""
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
    """Write `data` to `path` atomically via a same-directory temp file.

    O_EXCL|O_NOFOLLOW on a pid-qualified temp name refuses to write through a
    pre-planted symlink."""
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


# --- TOCTOU hardening for apply_proposal/revert_proposal --------------------
# _atomic_write only protects the final path component. These helpers open
# each directory component relative to its verified parent fd with
# O_NOFOLLOW, so a symlinked-directory swap has no check-then-use window.
def _opendir_nofollow_at(dir_fd: int, name: str) -> int:
    return os.open(name, os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)


def _resolve_target_dir_fd(target: Path, rel_target: str):
    """Open every directory component of `rel_target` but the last, each
    relative to its verified parent with O_NOFOLLOW. Returns (dir_fd,
    filename); the caller must os.close(dir_fd)."""
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
    """Read `name` relative to a verified directory fd, refusing a symlink.
    Missing reads as empty."""
    try:
        fd = os.open(name, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0), dir_fd=dir_fd)
    except FileNotFoundError:
        return b""
    except OSError as e:
        raise RuntimeError(f"refusing to read {name} — {e} (possible symlink)") from e
    with os.fdopen(fd, "rb") as f:
        return f.read()


def _atomic_write_at(dir_fd: int, name: str, data: bytes):
    """_atomic_write(), but relative to an already-verified directory fd."""
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
        os.unlink(tmp, dir_fd=dir_fd)  # plain leftover from a crashed run — retry once
        fd = os.open(tmp, flags, 0o600, dir_fd=dir_fd)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)


@contextlib.contextmanager
def _proposal_lock(target: Path):
    """Serialize apply_proposal/revert_proposal across concurrent install.py
    runs (advisory flock). No-op if fcntl is unavailable."""
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


# --- out-of-target receipt anchor --------------------------------------------
# receipt.json lives under --target, so an agent scoped there could tamper a
# file and forge its baseline entry. A mirror copy outside --target gives
# --audit check 7 something to cross-check. It does not defend against an
# unrestricted same-uid process.
def _target_slug(target: Path) -> str:
    # Full digest: scopes both the receipt anchor and the pack delivery root.
    return hashlib.sha256(str(target).encode()).hexdigest()


def _package_sha(package: Path) -> str:
    """One hash over every shipped path and its content in the source package.
    Same construction as tools/contract_evidence.dist_sha()."""
    h = hashlib.sha256()
    try:
        for p in sorted(Path(package).rglob("*")):
            # Skip .git so a checkout-delivered package hashes like dist/.
            if not p.is_file() or "__pycache__" in p.parts or ".git" in p.relative_to(package).parts:
                continue
            h.update(p.relative_to(package).as_posix().encode())
            h.update(hashlib.sha256(p.read_bytes()).digest())
    except OSError:
        return "unknown"
    return h.hexdigest()


# --- installed_sha: what is actually on disk, not what the receipt claims ---
# Same algorithm as contract_evidence.dist_sha() (sorted relative posix path,
# then each file's sha256), run over the shipped files in the TARGET. Recorded
# at install time and recomputed by the contract test. contract_test.py holds
# a byte-identical copy of installed_sha(); tools/selftest_installed_sha.py
# asserts they agree.
#
# Prefixes shipped tools write into at runtime (run log, brief store). Nothing
# ships under them, so they are excluded from the hash and from check 1.
RUNTIME_WRITABLE_PREFIXES = ("observability/data/", "session-brief/briefs/")
# Roots the operator owns outright (private add-ons the package never ships).
# --audit skips them; --update and --rollback never touch them.
OPERATOR_OWNED_PREFIXES = ("fleet/",)
# Shipped files the operator is told to edit. Not byte-compared by --audit,
# not in installed_sha, never auto-written by --update.
OPERATOR_EDITABLE_CONFIG = ("observability/freshness.json", "memory-mesh/mesh.toml")
# The operator's own list of other paths that are theirs, one per line (a
# trailing / is a directory prefix). An entry covering a shipped path is
# refused and reported; honored entries are listed in the audit result.
OPERATOR_OWNED_FILE = "operator-owned"


def _operator_declared(target: Path, package_paths):
    """(honored_entries, problems) from <target>/.cc-seed/operator-owned."""
    f = target / CC_SEED_DIR / OPERATOR_OWNED_FILE
    if not f.is_file():
        return [], []
    honored, problems = [], []
    for raw in f.read_text(encoding="utf-8", errors="replace").splitlines():
        e = raw.strip()
        if not e or e.startswith("#"):
            continue
        if e.startswith("/") or ".." in e.split("/"):
            problems.append(f"operator-owned: {_escape_path(e)!s} refused — must be a relative path inside the install")
            continue
        covers = [p for p in package_paths
                  if p == e.rstrip("/") or (e.endswith("/") and p.startswith(e))]
        if covers:
            problems.append(f"operator-owned: {_escape_path(e)} refused — it covers a path "
                            f"the package ships ({_escape_path(covers[0])})")
            continue
        honored.append(e)
    return honored, problems

INSTALLED_SHA_EXEMPT = {
    *OPERATOR_EDITABLE_CONFIG,
    # --enable-demo rewrites this file in place.
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
        # Skip .git internals (they churn on every fetch) and __pycache__.
        if "__pycache__" in p.parts or ".git" in p.parts \
                or rel in INSTALLED_SHA_EXEMPT:
            continue
        if rel.startswith(RUNTIME_WRITABLE_PREFIXES):
            continue
        h.update(rel.encode())
        h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()


def _refresh_installer(target: Path, new_tree: Path) -> bool:
    """Replace the target's own install.py with the fetched tree's.

    install.py is not a shipped path, so --update would otherwise leave the
    original installer in place forever. Safe mid-run: this module is already
    compiled and the replace is atomic.
    """
    src = new_tree / "install.py"
    dst = target / "install.py"
    # Only refresh an existing copy: a fresh install doesn't place install.py
    # in the target, and adding one would show up as unexpected in --audit.
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
    """Where imported-pack content lands — outside the repo. Shares
    _target_slug with _anchor_path so the two never use different slugs."""
    xdg_state = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(xdg_state) / "cc-pack" / _target_slug(target)


def _expected_pack_dest(target: Path, pack_id: str) -> Path:
    """The only trustworthy delivery path for a pack id — always re-derived.

    The receipt's `delivery_path` is agent-editable and is display-only; any
    delete or hash must use this function, never that field."""
    if not _pack_is_safe_component(pack_id):
        raise ValueError(f"pack id {pack_id!r} is not a safe path component")
    return _pack_delivery_root(target) / "packs" / pack_id


def _save_anchor(target: Path, receipt: dict):
    """Best-effort mirror write; never blocks the real receipt (check 7 then
    reports SKIPPED)."""
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
    """Create the receipt with O_EXCL, refusing if one already exists (no
    pre-seeded fake baseline; a stale receipt is surfaced, not overwritten)."""
    d = target / CC_SEED_DIR
    d.mkdir(parents=True, exist_ok=True)
    p = d / RECEIPT_NAME
    fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(fd)
    receipt = {
        # Schema 2 adds install.refused_skills, install.package_sha, and
        # memory-hooks entries/already_present/adopted/prior_bytes_len
        # (replacing prior_bytes, which copied settings.json). Readers use
        # .get() defaults, so schema-1 receipts still work. See
        # docs/install-audit.md.
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
    """lstat-based inventory of everything already under target, taken before
    any component is written. Symlinks are recorded, never followed; files
    over _MAX_HASH_BYTES are listed but not hashed."""
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
        # Compose mode: every component and root file the seed would write
        # must be ABSENT; everything else is the user's and untouched. One
        # collision refuses the whole install before any byte moves.
        #
        # Exception: an existing memory/ satisfies the memory component and is
        # skipped whole. Functional components get no such pass.
        skipped = [c for c in SATISFIED_BY_EXISTING if (target / c).is_dir()]
        collisions = [c for c in COMPONENTS + ROOT_FILES
                      if (target / c).exists() and c not in skipped]
        # .claude/skills/<name> isn't a COMPONENTS entry, so pre-check it here
        # before anything is written.
        if "skills" not in skipped:
            # exists() OR is_symlink(): a dangling link must still collide,
            # matching _register_skills()'s backstop.
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

    # The receipt and pre-write baseline come first, before any component.
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
    # Merge before the _save_receipt below so the deferrals aren't clobbered.
    _merge_deferred(receipt, deferred_skills)
    _merge_refused(receipt, refused_skills)
    receipt["install"]["default_jobs"] = default_jobs
    # Snapshot exactly what this install wrote, so --update has per-file
    # history from day one.
    receipt["shipped"] = _shipped_snapshot(target, written + ROOT_FILES)
    # Measure the bytes that landed so the contract can prove they're unchanged.
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


# --- skill registration is decided by ORIGIN, not by name -------------------
# If a skill of the same name exists at user scope (~/.claude/skills):
#   same body  -> DEFER: record it, register nothing; the user-scope copy wins.
#   different  -> REFUSE loudly, naming both paths; never overwrite or shadow.
#   absent     -> register.
# The hash is computed at install time from both files; nothing is injected
# into the shipped SKILL.md, which must stay byte-identical to its source.
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
    # Record the refusal so it stays visible to --audit.
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
    """Yield (name, SKILL.md path) for each skill directory under skills_root."""
    if not skills_root.is_dir():
        return
    for entry in sorted(skills_root.iterdir()):
        canonical = entry / "SKILL.md"
        if entry.is_dir() and canonical.is_file():
            yield entry.name, canonical


def _shipped_skill_names() -> list:
    """Skill names this clone would install, read from the source tree (for
    the pre-write --into collision check)."""
    return [name for name, _ in _iter_skill_dirs(HERE / "skills")]


def _merge_deferred(receipt: dict, deferred: list):
    """Merge deferrals into the caller's in-memory receipt.

    A deferral is a live dependency on a file outside this install, re-checked
    by _verify_deferred_skills at audit time. Deliberately in-memory only: both
    callers save the receipt themselves afterwards, so a load-and-save here
    would be lost (overwritten) by that later save."""
    if not deferred:
        return
    existing = {d["name"]: d for d in receipt["install"].get("deferred_skills", [])}
    for d in deferred:
        existing[d["name"]] = d
    receipt["install"]["deferred_skills"] = [existing[k] for k in sorted(existing)]


def _merge_refused(receipt: dict, refused: list, seen: list = None):
    """Merge refusals into the caller's in-memory receipt (same rule as
    _merge_deferred).

    A name in `seen` but not in `refused` has been resolved; its record is
    dropped."""
    existing = {d["name"]: d for d in receipt["install"].get("refused_skills", [])}
    for name in (seen or []):
        existing.pop(name, None)
    for d in refused or []:
        existing[d["name"]] = d
    if existing or "refused_skills" in receipt["install"]:
        receipt["install"]["refused_skills"] = [existing[k] for k in sorted(existing)]


def _verify_refused_skills(target: Path, receipt: dict) -> list:
    """Flag every recorded refused skill on each audit until it is resolved."""
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
    """Re-run the origin check at audit time against each deferred-to file as
    it is now."""
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
    """Idempotent skill registration for --update.

    Skips skills already correctly linked, registers missing ones, and reports
    (rather than overwrites) a link that exists but points elsewhere.
    Returns (registered, deferred, refused, seen)."""
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
    """Register shipped skills at project level (.claude/skills/<name>).

    Claude Code discovers skills only at ~/.claude/skills or a project-level
    .claude/skills; project level needs no global write. Relative symlinks keep
    skills/ the single source of truth. Collisions are refused earlier by
    install(); the assert is only a backstop.
    Returns (registered, deferred, refused)."""
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
    """Write the default jobs into a fresh install's manifest (hello_fleet stays
    opt-in). Returns the names newly added."""
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
    """Copy governance/ into an existing install (opt-in only). Refuses if it
    already exists, since policy.yml may be customized."""
    if not (target / "PRINCIPLES.md").exists():
        return die(f"{target} doesn't look like an AI-OS Seed install — is --target correct?")
    dest = target / "governance"
    if dest.exists():
        print(f"{dest} already exists — nothing to do (if you want to reset it, "
              f"remove it yourself first; policy.yml may be customized).")
        return 0
    src = HERE / "governance"
    if not src.exists():
        # Withheld from this release, not missing — don't tell the user to re-clone.
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
        return False  # can't prove pristine -> keep
    ours = sorted(f.name for f in shipped.iterdir() if f.is_file())
    theirs = sorted(f.name for f in p.iterdir())
    if ours != theirs:
        return False
    return all(filecmp.cmp(shipped / n, p / n, shallow=False) for n in ours)


def _tree_is_pristine(shipped: Path, installed: Path) -> bool:
    """Recursive byte-identical check (governance/'s policy.yml is often
    customized)."""
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
    # Remove exactly the skill symlinks this installer registered (per the
    # receipt) before skills/ goes; .claude/skills/ may also hold the user's own.
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
    # Remove only the seed's own names: the root may also hold user content.
    for name in COMPONENTS + OPTIONAL_COMPONENTS + ROOT_FILES:
        p = target / name
        if not p.exists() and name in OPTIONAL_COMPONENTS:
            continue  # never enabled
        if name == "memory" and p.is_dir() and not _memory_is_pristine(p):
            # memory/ is deleted only when byte-identical to the shipped
            # scaffold; otherwise it is the user's and is kept.
            print(f"kept {p} — it differs from the shipped scaffold, so it's "
                  f"yours, not the seed's; delete it yourself if you're sure.")
            continue
        if name == "governance" and p.is_dir() and not _tree_is_pristine(HERE / "governance", p):
            # Same rule as memory/: policy.yml is likely customized.
            print(f"kept {p} — it differs from the shipped scaffold (likely a "
                  f"customized policy.yml), so it's yours; delete it yourself if you're sure.")
            continue
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
    # install.py's own bookkeeping — always removed.
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
    # Remove the out-of-target receipt anchor too.
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


# --- --approve: install.py, not the agent, performs gated writes -----------
# The agent stages (claude-md) or shows the command (mesh-bootstrap); a human
# runs --approve, which hashes what it applies, records the hash in the
# receipt and writes the bytes in the same step.

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
        pass  # non-fatal — the receipt is the record of truth
    print(f"CLAUDE.md region approved and written — hash {proposed_hash}")
    print(f"recorded in {target}/{CC_SEED_DIR}/{RECEIPT_NAME}")
    return 0


def _mesh_store_dir(target: Path):
    """The workspace's Claude Code auto-memory store, as computed by the
    target's own mesh_lib.store_dir() — not <ROOT>/memory/. This is the path
    install.sh mutates."""
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
        # Record the failed attempt so --audit shows bootstrap was tried.
        receipt.setdefault("gated_writes", {})["mesh-bootstrap"] = {
            "approved_at": _now(), "written": False,
            "error": f"memory-mesh/install.sh exited {r.returncode}",
        }
        _save_receipt(target, receipt)
        return die(f"memory-mesh/install.sh exited {r.returncode} — not recorded "
                   f"as approved (the failing step is named above).")
    store = _mesh_store_dir(target)  # re-derive: install.sh may have made mesh_lib importable
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


# --- --approve memory-hooks -------------------------------------------------
# Wiring hooks modifies the agent's own harness, so it is a gated write: shown
# as an exact settings.json diff, recorded as applied, and revocable via
# --revoke memory-hooks, which removes exactly the recorded entries.
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
    """The settings.json fragment this verb writes, using absolute paths into
    this install (a relative command would resolve against the agent's cwd)."""
    out = {}
    for event, matcher, tail in MEMORY_HOOKS:
        parts = tail.split(" ", 1)
        # Quoted so a --target containing spaces still works.
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
    # Dedup on (event, matcher, command), not on substring presence.
    present = _wired_hook_keys(doc)
    added, already_present = {}, {}
    for event, entries in adding.items():
        bucket = hooks.setdefault(event, [])
        for entry in entries:
            cmd = entry["hooks"][0]["command"]
            if (entry.get("matcher") or "", cmd) in present.get(event, ()):
                already_present.setdefault(event, []).append(cmd)
                continue
            bucket.append(entry)
            added.setdefault(event, []).append(cmd)
    for event in [e for e, v in hooks.items() if not v]:
        del hooks[event]
    after = (json.dumps(doc, indent=2) + "\n").encode()
    if after == before:
        # Nothing new to write. If there is no record, adopt the existing
        # wiring (empty `entries`) so --revoke and --audit have a subject.
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
        # Only what this install actually added, so revoke removes nothing else.
        "entries": added, "already_present": already_present,
        # Identity of the prior file, never its content: settings.json can
        # hold API keys, and the receipt is mirrored outside the target.
        "prior_sha": _sha256_bytes(before), "prior_bytes_len": len(before),
        "after_sha": _sha256_bytes(after),
    }
    _save_receipt(target, receipt)
    print(f"memory hooks approved and written to {settings}; recorded in the receipt.")
    print("M1 (one door, enforced) and M4 (memory reaches the session) do not hold "
          "without these — check with: install.py --target ... --contract")
    return 0


def _confirm_hook_write() -> bool:
    """Interactive y/N gate for hook writes and revokes.

    Non-interactive only with CI=true AND --apply ("I have seen the staged
    diff and affirm it"); there is deliberately no --yes flag."""
    if os.environ.get("CI") == "true" and "--apply" in sys.argv:
        print("CI=true with --apply — applying the staged diff non-interactively.")
        return True
    try:
        return input("write these entries? [y/N] ").strip().lower() in ("y", "yes")
    except EOFError:
        # Say why, so automation sees something actionable.
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
    # Remove per event and per hook item, so the user's own hooks in the same
    # group survive.
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
    # `recorded` is a set, so this counts distinct commands (two events can
    # share one), which may be fewer than the recorded entries.
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


# --- human-applied exact-diff proposal loop ----------------------------------
# The agent writes a canonical proposal (exact bytes, a hash binding what it
# saw, a rationale) and stops; a human applies it with one command, and
# install.py performs the write. Targets are an allowlist of NAMED files, never
# a class. Each must be reversible through this mechanism, system-owned (not
# human-owned state), and validatable before the write (_PROPOSAL_CHECKS).
#
# settings.json is allowed only on the condition that changes are shown before
# they are made. Enforced in mechanism: it is excluded from --apply-proposals
# (PROPOSAL_SINGLE_APPLY_ONLY); its full diff prints in --review-proposals and
# at --apply-proposal; and the apply requires --confirm TOKEN, a prefix of the
# after-content hash printed only beside that diff — so the approval is bound
# to the exact bytes shown.
PROPOSAL_ALLOWED_TARGETS = {
    "scheduler/manifest.yml",
    "observability/freshness.json",
    ".claude/settings.json",
}

# Targets the batch walker refuses: each apply is its own slug-named command,
# with the full diff shown first.
PROPOSAL_SINGLE_APPLY_ONLY = {".claude/settings.json"}

# Bound output: a runaway diff truncates.
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
    """The --confirm token for a single-apply-only target: a prefix of
    after_content's hash. Printed only beside the full diff, and it changes if
    the proposal's bytes change."""
    return _sha256_bytes(after_content.encode("utf-8")).split(":", 1)[-1][:_CONFIRM_LEN]


def _check_json_object(text: str):
    """Refuse after_content that isn't a JSON object."""
    try:
        parsed = json.loads(text)
    except ValueError as e:
        return f"after_content is not valid JSON ({e})"
    if not isinstance(parsed, dict):
        return "after_content parses but is not a JSON object"
    return None


def _check_manifest_yaml(text: str):
    """Stdlib structural check for scheduler/manifest.yml: the jobs key
    survives, no tabs, no duplicate job names."""
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


# Validity check per allowlisted target, run before apply: returns None (pass)
# or a reason (refuse). A fixed registry — proposals never name their own check.
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
    """Reject any slug that isn't a plain filename-safe token before it is
    joined onto a path."""
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
        # component) before validating after_content.
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

        # The checks above ran against the live file, so this diff is exactly
        # what a confirmed re-run will write.
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

        # Archive, don't delete: the receipt stores only hashes, so revert needs
        # before_content from this file. A failed archive makes revert impossible.
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
    """Undo one --apply-proposal call, only if nothing has touched the target
    since.

    Re-runs apply's checks in reverse: target still allowlisted, the archived
    proposal's target matches the receipt's, and before_content hashes to the
    value recorded at apply time. The read/write uses the same descriptor-
    relative resolution and lock as apply_proposal."""
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


# --- batch review + apply ---------------------------------------------------
# --review-proposals is read-only (exit 0 always). --apply-proposals loops over
# the same per-slug apply_proposal(), so every guard runs per item; a refusal
# skips that item, and the batch exits nonzero if anything was refused.

def _staged_proposal_slugs(target: Path):
    """Valid-slug staged proposals, sorted. Junk filenames are returned
    separately, not fatal."""
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
            # settings.json must be shown before it is changed, so leave it for
            # a slug-named single apply.
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


# --- cc-pack import -----------------------------------------------------------
# An agent may inspect/verify a pack freely (pack/import_pack.py, read-only);
# only a human running --approve import-pack moves bytes.

def _verify_pack_dir(pack_dir: Path):
    """Verify a pack by shelling out to the shipped pack/import_pack.py rather
    than re-implementing its checks."""
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


# Signature state, parsed from import_pack.py --verify output. Classification
# lives there; enforcement lives here.
_SIG_LINE_RE = re.compile(r"^signature:\s*(\S+)(?:\s+principal=(\S+))?", re.MULTILINE)

# Duplicate of import_pack.py's SIG_STATES (it is shelled out to, not
# imported; keep in sync). Any parsed word outside this set fails closed to
# 'verification-error'.
_KNOWN_SIG_STATES = ("unsigned", "invalid", "unknown-signer", "verification-error", "verified")


def _verify_pack_signature(pack_dir: Path, allowed_signers):
    """Run import_pack.py --verify (with --allowed-signers if given) and parse
    its 'signature: <state>[ principal=<x>]' line.

    Returns (state, principal_or_None, raw_output). Fails closed to
    'verification-error' if the line is missing or the state is unknown."""
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
    """Copy pack_dir into a fresh private staging directory before
    verification, so the bytes verified are exactly the bytes applied (no
    swap window while a human decides). Refuses anything but regular files
    and directories."""
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
    """Copy every declared part file to dest_root/parts/<id>/<relfile>,
    re-validating each path. Returns {"parts/<id>/<relfile>": sha256}."""
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
    """Render PACKS.md from receipt['imported_packs'] — deterministic (sorted by
    pack id), one section per pack, never hand-edited."""
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
        # PACKS.md is @-imported into CLAUDE.md, so escape every field.
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
    """Strip an approved cc-pack pointer region (and its separator) from the
    start of `data`, so check 3 sees only what the cc-seed region owns."""
    gw = receipt.get("gated_writes", {}).get("import-pack-pointer")
    if not gw or not gw.get("written"):
        return data
    s_marker, e_marker = PACKS_MARKER_START.encode(), PACKS_MARKER_END.encode()
    if not data.startswith(s_marker):
        return data
    e_idx = data.find(e_marker)
    if e_idx == -1:
        return data
    end = e_idx + len(e_marker) + 1  # past the end marker and its trailing \n
    # _write_packs_pointer's "\n\n" separator is consumed as a pair.
    if data[end:end + 2] == b"\n\n":
        return data[end + 2:]
    return data[end:]


def _write_packs_pointer(target: Path, receipt: dict, delivery_root: Path) -> bool:
    """Write the one-line @<delivery_root>/PACKS.md pointer into
    target/CLAUDE.md, once per target. Returns True if written, False if
    already present.

    Raises RuntimeError if cc-pack markers are already on disk but the
    receipt has no record of them (e.g. a crash before the receipt save), so
    a retry never prepends a second region."""
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
    # Same "\n\n" separator as _approve_claude_md, so
    # _strip_pack_pointer_prefix counts it correctly in either order.
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

        # Signature policy, checked before any state-changing step.
        #
        # invalid / unknown-signer / verification-error refuse unconditionally,
        # for every audience — the audience field is in pack.json and could be
        # relabeled without breaking the integrity chain.
        #
        # unsigned: a replica pack refuses unless --allow-unsigned (recorded in
        # the receipt); a shareable pack imports. The signature state is
        # recorded for every pack.
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

        # Engagement scoping: a pack built with --tag <slug> must not cross into
        # a different engagement. The authorization source is the target's
        # recorded engagement (set via --set-engagement), never the CLI --tag,
        # which is only a confirmation. Fails closed when the target has no
        # engagement. An untagged pack always imports.
        #
        # Inspect the raw tags value: only None means "no tags"; any other
        # non-list-of-strings shape is refused (a bare string would turn
        # `in` into a substring match).
        raw_tags = manifest.get("tags")
        if raw_tags is None:
            pack_tags = []
        elif isinstance(raw_tags, list) and all(isinstance(t, str) for t in raw_tags):
            pack_tags = raw_tags
        else:
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
            # Signature state at import, and whether --allow-unsigned was used.
            "signature_state": sig_state,
            "signature_principal": sig_principal,
            "imported_unsigned": sig_state == "unsigned" and manifest.get("audience") == "replica",
        }

        try:
            pointer_written = _write_packs_pointer(target, receipt, delivery_root)
        except RuntimeError as e:
            # Content landed but the pointer write refused; surface it rather
            # than retrying blind.
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
    """Record which engagement this target operates under — the authorization
    source _approve_import_pack checks (a CLI --tag alone is self-asserted).
    Refuses to silently switch an existing engagement without --force."""
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
    # dest is re-derived, never rec['delivery_path']; surface any mismatch.
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


# --- --audit -----------------------------------------------------------------
# Deterministic post-install auditor, run by a human in a fresh shell. Compares
# live state against the receipt and --package's own manifest (never the
# installed tree). See docs/install-audit.md for the check table and the
# residuals it does not close.

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
            return None  # too large to hash at baseline — can't verify, don't false-flag
        if oct(stat.S_IMODE(st.st_mode)) != entry["mode"]:
            return f"mode changed ({entry['mode']} -> {oct(stat.S_IMODE(st.st_mode))})"
        return None if _sha256_file(live_p) == entry["hash"] else "content differs"
    return None


def _verify_registered_skill_link(target: Path, package: Path, name: str):
    """Verify a registered .claude/skills/<name>/SKILL.md is a symlink to the
    shipped skill file."""
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

    The matcher is part of when a hook runs, so the same command under a
    different matcher is a different hook. A missing and an empty matcher
    both mean "match everything" and normalise to "".
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
    """Check settings.json structurally against the approved memory-hooks
    record: valid JSON, hooks not disabled, every approved command wired as a
    runnable hook under its event, and no drift from the recorded hash."""
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
            if rel in OPERATOR_EDITABLE_CONFIG:
                continue  # the operator's own config
            if rel.split("/", 1)[0] in UPDATE_MANUAL_ONLY_COMPONENTS:
                continue  # memory/ is the operator's once it has content
            if rel == "scheduler/manifest.yml":
                continue  # owned by check 2 (--enable-demo rewrites it in place)
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
        # A refused or deferred skill has no project-scope link by design.
        checked_rel.add(f".claude/skills/{d['name']}")
    for d in receipt["install"].get("deferred_skills", []) or []:
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

    # Paths shipped tools write into at runtime (run log, brief store).
    runtime_writable_prefixes = RUNTIME_WRITABLE_PREFIXES
    pkg_paths = ({p.relative_to(package).as_posix() for c in written
                  for p in (package / c).rglob("*")} | set(ROOT_FILES)) if package else set()
    declared, decl_problems = _operator_declared(target, pkg_paths)
    problems.extend(decl_problems)
    for live_p in sorted(target.rglob("*")):
        rel = live_p.relative_to(target).as_posix()
        if rel in checked_rel:
            continue
        if rel == CC_SEED_DIR or rel.startswith(CC_SEED_DIR + "/"):
            continue  # install.py's own receipt/staged scaffold
        if rel == "install.py" and package and (package / "install.py").is_file():
            # The install's own installer copy: compared, not skipped.
            if live_p.read_bytes() != (package / "install.py").read_bytes():
                problems.append("install.py: the install's own installer differs from the package's")
            continue
        if rel == "CLAUDE.md":
            continue  # owned by check 3
        hook_record = (receipt.get("gated_writes") or {}).get("memory-hooks")
        if rel == ".claude" and hook_record:
            continue
        if rel == ".claude/settings.json" and hook_record:
            # settings.json is the product of an approved gated write: exempt
            # from UNEXPECTED only while that record is active, with its hooks
            # checked structurally.
            if hook_record.get("written"):
                problems.extend(_verify_hook_wiring(target, hook_record))
                continue
            # A revoked record falls through to the baseline/UNEXPECTED checks.
        if rel.split("/", 1)[0] in UPDATE_MANUAL_ONLY_COMPONENTS:
            continue  # the operator's component
        if rel.rsplit("/", 1)[-1] == ".DS_Store":
            continue  # macOS Finder metadata
        if "__pycache__" in rel.split("/") or rel.endswith((".pyc", ".pyo")):
            continue  # bytecode cache
        if any(rel == p.rstrip("/") or rel.startswith(p) for p in runtime_writable_prefixes):
            continue
        if any(rel == p.rstrip("/") or rel.startswith(p) for p in OPERATOR_OWNED_PREFIXES):
            continue
        if any(rel == e.rstrip("/") or (e.endswith("/") and rel.startswith(e)) for e in declared):
            continue
        if rel in baseline:
            reason = _compare_baseline_entry(live_p, baseline[rel])
            if reason:
                problems.append(f"{rel}: pre-existing content changed without a gated-write record ({reason})")
            continue
        problems.append(f"{_escape_path(rel)}: UNEXPECTED — not shipped by the package, not in "
                         f"the pre-install baseline, not a declared runtime path")

    # A baseline path that has since been deleted is a change too.
    for rel in sorted(baseline):
        if not os.path.lexists(str(target / rel)):
            problems.append(f"{_escape_path(rel)}: pre-existing path recorded in "
                            f"the baseline is GONE — it was removed after the "
                            f"install without a gated-write record")

    note = (f"{len(declared)} operator-declared path(s) left to the operator "
            f"(.cc-seed/{OPERATOR_OWNED_FILE}): " + ", ".join(declared[:40])) if declared else None
    if problems:
        return _flagged("1", "package trace", problems + ([note] if note else []))
    return _pass("1", "package trace", note)


def _check_2(target: Path, package: Path) -> dict:
    sync = target / "scheduler" / "sync.py"
    if not sync.exists():
        return _flagged("2", "scheduler", ["scheduler/sync.py missing — can't verify"])
    # sync.py lives under the untrusted --target: verify it is byte-identical
    # to the package copy before executing it.
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
    # Skip an approved cc-pack pointer region so check 3 sees only its own.
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
            pass  # too large to hash at baseline — can't verify
        elif baseline_entry["hash"] == _sha256_bytes(b""):
            # An empty pre-existing CLAUDE.md gets no "\n\n" separator
            # (mirrors _approve_claude_md), so `before` must be empty.
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
    """Check the workspace's real Claude Code memory store (_mesh_store_dir):
    approval on record, MEMORY.md carries the GENERATED header, and
    MEMORY.md.pre-mesh matches the hash --approve took before install.sh.
    Not full fold-transform equality (see docs/install-audit.md residuals)."""
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
    # Placeholder: live ~/.key migration state is outside <ROOT> and not
    # audited; shipped keyvault scripts are covered by check 1.
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
    """Cross-check the live receipt against its out-of-target anchor. A
    divergence means receipt.json was edited by something other than
    install.py."""
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
    """Verify the cc-pack pointer region at the START of CLAUDE.md (check 3
    owns whatever follows it)."""
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
        # The byte after the start marker is skipped when slicing `region`,
        # so it must be the expected \n or it would escape the hash check.
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
    """Audit imported-pack content in the out-of-repo delivery root (which
    check 1 never walks): every recorded file still hash-matches, nothing
    extra appeared, PACKS.md matches a fresh render, and no orphaned pack
    directories exist. Paths are re-derived via _expected_pack_dest, never
    taken from receipt['delivery_path']."""
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

    # PACKS.md is a pure function of the receipt: re-render and compare bytes.
    packs_md = delivery_root / "PACKS.md"
    if imported or packs_md.exists():
        if packs_md.is_symlink():
            problems.append(f"PACKS.md: {packs_md} is a symlink, not a plain file")
        elif not packs_md.exists():
            problems.append(f"PACKS.md: missing at {packs_md} despite {len(imported)} pack(s) on record")
        elif packs_md.read_bytes() != _render_packs_md(receipt):
            problems.append(f"PACKS.md: {packs_md} does not match what the current receipt "
                            f"would render — edited outside install.py, or stale")

    # Orphaned pack directories: on disk under packs/ but not in the receipt.
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
    """Run a check, converting an unexpected exception into an ERROR result.
    Used for checks 8/9, which read receipt data an agent could shape
    adversarially."""
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


# --- --update: let an existing install adopt later cc-seed content ---------
#   - fetch is pinned to an immutable git tag, never a mutable branch.
#   - `--from` accepts only a local path (or nothing, for the pinned
#     default); URLs are refused, since the fetched content is executable.
#   - installs with no recorded per-file "shipped" hash are never assumed
#     pristine: bootstrap from the recorded historical commit if resolvable,
#     otherwise report every existing path as SKIP.
#   - detection is free; writing anything requires `--apply`.
#   - tar extraction validates every member stays under the destination
#     (path traversal) before anything touches disk.
#   - a target-scoped lock serializes concurrent --update runs (not yet
#     against --approve/--apply-proposal).

# One "owner/repo" literal (URLs are built against several domains). It is a
# build_seed.py PUBLIC_EXCEPTIONS entry so the build scrub leaves it intact;
# splitting it would let the scrub corrupt the value.
UPDATE_SOURCE_REPO_SLUG = "cvp1/ai-os-seed"
UPDATE_SOURCE_OWNER, UPDATE_SOURCE_REPO = UPDATE_SOURCE_REPO_SLUG.split("/", 1)
_UPDATE_MAX_BYTES = 50 * 1024 * 1024  # bound the fetch
_UPDATE_FETCH_TIMEOUT = 30
SHIPPED_PATHS = COMPONENTS + ROOT_FILES  # the exact surface install() itself writes
# Paths install() mutates after copying (the manifest gets the live job
# list). --update reports these but never auto-writes them.
UPDATE_MANUAL_ONLY_PATHS = {"scheduler/manifest.yml", *OPERATOR_EDITABLE_CONFIG}
# Components that become the operator's once they have content (memory/
# starts as an empty scaffold). --update never writes them.
UPDATE_MANUAL_ONLY_COMPONENTS = {"memory"}


def _update_lock_path(target: Path) -> Path:
    return target / CC_SEED_DIR / ".update.lock"


@contextlib.contextmanager
def _update_lock(target: Path):
    """O_EXCL advisory lock for one --update run. Serializes --update against
    itself only, not against --approve / --apply-proposal."""
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
    """GET url with a size cap and timeout. Raises RuntimeError on any
    failure; never returns a partial or unbounded body."""
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
    """Extract a .tar.gz into dest, refusing any member that resolves outside
    dest (path traversal) and any symlink/hardlink/device member. Returns the
    single top-level directory; an ambiguous root fails rather than guessing."""
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
    """Best-effort ordering for 'X.Y.Z[-suffix]': numeric prefix first, and a
    pre-release suffix sorts below no suffix (0.2.6-alpha < 0.2.6). Only used
    to refuse accidental downgrades."""
    m = re.match(r"^(\d+(?:\.\d+)*)(.*)$", v.strip())
    if not m:
        return ((), v)  # unparseable — sorts by raw string, never crashes
    nums = tuple(int(x) for x in m.group(1).split("."))
    suffix = m.group(2)
    return (nums, 0 if not suffix else -1)


def _fetch_ref_tree(ref: str, tmp_parent: Path) -> Path:
    """Fetch and extract an immutable ref (tag or commit sha) from the pinned
    source repo. Refuses branch names."""
    if ref in ("main", "master", "HEAD") or "/" in ref:
        raise RuntimeError(f"refusing to fetch update source by mutable/unsafe ref {ref!r}")
    url = (f"https://github.com/{UPDATE_SOURCE_OWNER}/{UPDATE_SOURCE_REPO}"
           f"/archive/{ref}.tar.gz")
    data = _fetch_url(url)
    dest = tmp_parent / f"extract-{ref.replace('/', '_')}"
    return _safe_extract_tar(data, dest)


def _resolve_update_source(explicit_from: str, tmp_parent: Path):
    """Returns (tree: Path, version: str, source_desc: str).

    `explicit_from` is a local path only, never a URL. Otherwise read VERSION
    from `main` (text only) and fetch that version's immutable tag archive;
    a missing tag fails clearly rather than falling back to main."""
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
    `paths` (default SHIPPED_PATHS) in `tree`. install() passes its `written`
    list so a skipped component is never recorded as shipped. Symlinks are
    recorded, not followed; over-cap files get hash=None."""
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
    """For an install with no receipt["shipped"]: snapshot the historical
    commit recorded in receipt["install"]["installer_commit"]. Returns None if
    unknown or unfetchable (the caller then treats every path as dirty)."""
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
    """Three-way plan for every path the new tree ships: create / update /
    skip_dirty / manual_only / unchanged. `shipped_now` is what we last
    shipped here (receipt["shipped"], a legacy reconstruction, or {})."""
    plan = {"create": [], "update": [], "skip_dirty": [], "manual_only": [], "unchanged": []}
    new_snap = _shipped_snapshot(new_tree)
    for rel, new_entry in sorted(new_snap.items()):
        live = target / rel
        if not live.exists() and not live.is_symlink():
            if _is_update_manual_only(rel):
                continue  # absent and manual-only: nothing to create
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
    """Copy every create/update path in, then refresh receipt["shipped"] for
    those paths (and unchanged ones). skip_dirty paths keep their old record."""
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
    # "unchanged" paths already equal the new package, so record the new entry.
    for rel in plan["create"] + plan["update"] + plan["unchanged"]:
        shipped[rel] = new_snap[rel]
    receipt["shipped"] = shipped


# --- --rollback: undo the most recent --update --apply, byte for byte -------
# --update can't serve as the way back (it refuses downgrades and never
# deletes paths it created), so every --update --apply saves what it is about
# to replace and --rollback restores it.
ROLLBACK_DIR = "rollback"
ROLLBACK_KEEP = 3  # only the last few updates are undoable


def _rollback_root(target: Path) -> Path:
    return target / CC_SEED_DIR / ROLLBACK_DIR


def _rollback_points(target: Path, include_used: bool = False) -> list:
    """Saved points, oldest first. Used points (*.rolled-back) are never
    offered again."""
    root = _rollback_root(target)
    if not root.is_dir():
        return []
    return sorted(p for p in root.iterdir()
                  if p.is_dir() and (p / "rollback.json").is_file()
                  and (include_used or not p.name.endswith(".rolled-back")))


def _snapshot_for_rollback(target: Path, plan: dict, new_snap: dict,
                           from_version, to_version) -> Path:
    """Save what _apply_update will overwrite, before it runs. Created paths
    are recorded with their new hash so rollback can tell "as the update left
    it" (removable) from "edited since" (left alone)."""
    snap = _rollback_root(target) / datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    files = snap / "files"
    files.mkdir(parents=True)
    updated = []
    for rel in plan["update"]:
        live = target / rel
        entry = {"rel": rel, "type": _lstat_type(live.lstat()),
                 "new": new_snap.get(rel, {})}
        if entry["type"] == "symlink":
            entry["symlink_target"] = os.readlink(live)
        elif entry["type"] == "file":
            (files / rel).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(live, files / rel)
        updated.append(entry)
    created = [{"rel": rel, "new": new_snap.get(rel, {})} for rel in plan["create"]]
    receipt_p = target / CC_SEED_DIR / RECEIPT_NAME
    if receipt_p.is_file():
        shutil.copy2(receipt_p, snap / RECEIPT_NAME)
    if (target / "install.py").is_file():
        shutil.copy2(target / "install.py", snap / "install.py")
    (snap / "rollback.json").write_text(json.dumps({
        "at": _now(), "from_version": from_version, "to_version": to_version,
        "updated": updated, "created": created}, indent=1))
    older = _rollback_points(target, include_used=True)
    for old in older[:-ROLLBACK_KEEP]:
        shutil.rmtree(old)
    return snap


def _still_as_updated(live: Path, new_entry: dict) -> bool:
    if not live.exists() and not live.is_symlink():
        return False
    t = _lstat_type(live.lstat())
    if t != new_entry.get("type"):
        return False
    if t == "file":
        return _sha256_file(live) == new_entry.get("hash")
    if t == "symlink":
        return os.readlink(live) == new_entry.get("symlink_target")
    return True


def do_rollback(target: Path, assume_yes: bool) -> int:
    """Undo the most recent --update --apply after showing every path and
    getting a typed yes. Paths edited since the update are never touched."""
    snaps = _rollback_points(target)
    if not snaps:
        return die(f"{target}: no rollback point — only an --update --apply made by an "
                   f"installer with --rollback support leaves one")
    snap = snaps[-1]
    meta = json.loads((snap / "rollback.json").read_text())
    restore, remove, keep = [], [], []
    for e in meta["updated"]:
        (restore if _still_as_updated(target / e["rel"], e["new"]) else keep).append(e)
    for e in meta["created"]:
        (remove if _still_as_updated(target / e["rel"], e["new"]) else keep).append(e)
    print(f"rollback {target}: undo update {meta['from_version']} -> "
          f"{meta['to_version']} ({meta['at']})")
    for e in restore:
        print(f"  RESTORE {e['rel']}")
    for e in remove:
        print(f"  REMOVE  {e['rel']}  (created by that update)")
    for e in keep:
        print(f"  KEEP    {e['rel']}  (changed since the update — left as is)")
    print("  plus: the receipt and install.py return to their pre-update bytes.")
    if not assume_yes:
        try:
            if input("  roll back? [y/N] ").strip().lower() not in ("y", "yes"):
                print("  not rolled back.")
                return 1
        except EOFError:
            return die("--rollback needs a typed yes (or --assume-yes for an automated "
                       "caller that has already shown this list)")
    with _update_lock(target):
        for e in restore:
            live = target / e["rel"]
            live.unlink()
            if e["type"] == "symlink":
                os.symlink(e["symlink_target"], live)
            elif e["type"] == "file":
                shutil.copy2(snap / "files" / e["rel"], live)
        # Deepest first, so a created dir is emptied before it is considered.
        for e in sorted(remove, key=lambda e: e["rel"].count("/"), reverse=True):
            live = target / e["rel"]
            if not live.exists() and not live.is_symlink():
                continue  # already pruned as the empty parent of a removed path
            if live.is_dir() and not live.is_symlink():
                try:
                    live.rmdir()          # only if empty: never remove operator files
                except OSError:
                    print(f"  KEEP    {e['rel']}/  (not empty)")
            else:
                live.unlink()
            # A created component leaves its now-empty parents behind; prune
            # upward, never past the target, never a non-empty dir.
            parent = live.parent
            while parent != target and parent.is_dir() and not any(parent.iterdir()):
                parent.rmdir()
                parent = parent.parent
        if (snap / RECEIPT_NAME).is_file():
            shutil.copy2(snap / RECEIPT_NAME, target / CC_SEED_DIR / RECEIPT_NAME)
        if (snap / "install.py").is_file() and (target / "install.py").is_file():
            shutil.copy2(snap / "install.py", target / "install.py")
        snap.rename(snap.with_name(snap.name + ".rolled-back"))
    print(f"  rolled back to {meta['from_version']}. Registered skill links outside "
          f"the install (if the update added any) are not touched.")
    return 0


def do_reseal(target: Path, assume_yes: bool) -> int:
    """Re-record installed_sha as the operator-approved baseline.

    installed_sha is otherwise refreshed only by install and update, so an
    install that diverges for a good reason would stay red forever. This is
    the explicit way back: it lists every shipped file that differs, needs a
    typed yes, and changes exactly one field.
    """
    receipt = _load_receipt(target)
    if receipt is None:
        return die(f"{target} has no {CC_SEED_DIR}/{RECEIPT_NAME} — "
                   f"--reseal re-approves an EXISTING baseline; an install that "
                   f"predates receipts entirely wants --adopt-baseline.")
    old = receipt["install"].get("installed_sha")
    live = installed_sha(target, receipt["install"].get("components", []), ROOT_FILES)
    if old == live:
        print(f"{target}: already sealed — the tree matches its receipt "
              f"({live[:16]}). Nothing to do.")
        return 0
    shipped = receipt.get("shipped") or {}
    changed, gone = [], []
    for rel, rec in sorted(shipped.items()):
        want = (rec or {}).get("hash") or ""
        # Only files carry a content hash; skip dir/symlink entries.
        if not want.startswith("sha256:"):
            continue
        p = target / rel
        if not p.is_file():
            gone.append(rel)
            continue
        if "sha256:" + hashlib.sha256(p.read_bytes()).hexdigest() != want:
            changed.append(rel)
    print(f"reseal {target}")
    print(f"  recorded baseline : {old or '(none)'}")
    print(f"  live measurement  : {live}")
    if changed or gone:
        print(f"  shipped files that no longer match what this install wrote "
              f"({len(changed) + len(gone)}):")
        for rel in (changed + [g + "  (MISSING)" for g in gone])[:40]:
            print(f"    - {rel}")
        if len(changed) + len(gone) > 40:
            print(f"    … and {len(changed) + len(gone) - 40} more")
    else:
        print("  no shipped file differs — the change is in what the hash "
              "counts, or in files added beside the shipped set.")
    print("  Resealing ACCEPTS these bytes as the reference point. Only do "
          "this if you know why they differ.")
    if not assume_yes:
        try:
            if input("  reseal? [y/N] ").strip().lower() not in ("y", "yes"):
                print("  not resealed.")
                return 1
        except EOFError:
            return die("--reseal needs a typed yes (or --assume-yes for an "
                       "automated caller that has already shown this list)")
    receipt["install"]["installed_sha"] = live
    reseals = receipt.setdefault("reseals", [])
    reseals.append({"at": _now(), "from": old, "to": live,
                    "shipped_changed": changed, "shipped_missing": gone,
                    "assumed_yes": bool(assume_yes)})
    _save_receipt(target, receipt)
    print(f"  resealed: {live[:16]} is now the baseline; recorded in the receipt.")
    return 0


def do_adopt_baseline(target: Path) -> int:
    """Record the current tree as the operator-approved baseline for an
    install that predates receipts entirely (no history to fetch).

    Makes no claim about where the content came from. Run only by name —
    --update never calls it — and refuses a target that already has a
    receipt."""
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
        # Same schema as _init_receipt.
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
    """Re-run the origin check for every shipped skill not already correctly
    registered, and reconcile the receipt. Returns how many were newly
    registered. This is the repair path for a refused skill."""
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
            # A version string doesn't identify a beta build; "current" also
            # requires the package bytes to match.
            same_bytes = (receipt["install"].get("package_sha")
                          == _package_sha(new_tree))
            if new_version != "unknown" and new_version == current_version and same_bytes:
                print(f"update: already current (version {current_version}).")
                if apply:
                    _redecide_skill_origins(target, receipt)
                    # Still repair what an old installer couldn't provide: the
                    # baseline measurement and the installer itself.
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
                # No files to write is not nothing to do: re-run the skill
                # origin check (repairs refused skills).
                if bootstrapped:
                    receipt["shipped"] = shipped_now
                # An empty plan means shipped bytes match the fetched tree, so
                # record a baseline if one is absent. Never re-baseline an
                # existing one.
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

            snap = _snapshot_for_rollback(target, plan, new_snap, current_version, new_version)
            print(f"rollback point saved: {snap.relative_to(target)} "
                  f"(undo with: install.py --target {target} --rollback)")
            _apply_update(target, receipt, new_tree, plan, new_snap)
            newly_registered, newly_deferred, newly_refused, seen_names = \
                _register_new_skills(target)
            _merge_deferred(receipt, newly_deferred)
            _merge_refused(receipt, newly_refused, seen_names)
            # Extend install.components and registered_skills so check 1 audits
            # what arrived by update.
            comps = receipt["install"].setdefault("components", [])
            # "unchanged" counts too: those bytes are the package's.
            for rel in sorted(set(plan["create"]) | set(plan["update"]) | set(plan["unchanged"])):
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
            # Name the dist this tree now came from (soak evidence binds to it).
            receipt["install"]["package_sha"] = _package_sha(new_tree)
            updates[-1]["package_sha"] = receipt["install"]["package_sha"]
            # An update legitimately rewrites shipped bytes, so re-measure.
            # Only install and update refresh this; --audit/--approve never do.
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
    ap.add_argument("--rollback", action="store_true",
                    help="undo the most recent --update --apply: restore the bytes it "
                         "replaced, remove paths it created, and return the receipt and "
                         "installer to their pre-update state. Paths edited since the "
                         "update are left alone. Needs a typed yes (--assume-yes for an "
                         "automated caller).")
    ap.add_argument("--reseal", action="store_true",
                    help="re-approve the CURRENT bytes as this install's baseline "
                         "after a divergence you meant (a component kept under "
                         "version control, a file you patched). Prints every shipped "
                         "file that differs and needs a typed yes; --assume-yes for "
                         "an automated caller. Never fetches, never writes shipped "
                         "bytes.")
    ap.add_argument("--assume-yes", action="store_true",
                    help="skip --reseal's typed confirmation (the caller has already "
                         "shown the operator what it is accepting)")
    ap.add_argument("--adopt-baseline", action="store_true",
                    help="SEED-076: for an install that predates the receipt system "
                         "entirely (no .cc-seed/receipt.json at all) — records current "
                         "on-disk content as the operator-approved baseline so --update "
                         "has real history to compare against, going forward. Refuses if "
                         "a receipt already exists. Never called implicitly by --update.")
    args = ap.parse_args()

    if args.detect:
        # Enumerated from the parser, so any new flag is rejected too.
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
                 args.adopt_baseline, args.reseal, bool(args.revoke),
                 args.contract, args.rollback]
    if sum(bool(x) for x in exclusive) > 1:
        return die("--enable-demo, --enable-governance, --uninstall, --approve, --audit, "
                   "--list-packs, --remove-pack, --set-engagement, --apply-proposal, "
                   "--revert-proposal, --review-proposals, --apply-proposals, --update, "
                   "--adopt-baseline, --revoke, --contract and --rollback are mutually exclusive")
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
    if args.reseal:
        return do_reseal(target, args.assume_yes)
    if args.rollback:
        return do_rollback(target, args.assume_yes)
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
