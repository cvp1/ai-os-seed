#!/usr/bin/env python3
"""AI-OS Seed installer — the deterministic byte-mover behind AGENT-INSTALL.md.

The agent (or a human) orchestrates and decides; this script is the only
thing that writes install content, so what lands in --target is
byte-identical to this repo, never transcribed by a model.

    install.py --detect                            # read-only: report prior installs on this machine
    install.py --target ~/ai-os-seed               # copy the substrate in
    install.py --target ~/ai-os-seed --enable-demo # add hello_fleet to the scheduler manifest
    install.py --target ~/ai-os-seed --approve claude-md       # apply a staged CLAUDE.md addition
    install.py --target ~/ai-os-seed --approve mesh-bootstrap  # run memory-mesh/install.sh, recorded
    install.py --target ~/ai-os-seed --audit --package <clone> # deterministic post-install auditor
    install.py --target ~/ai-os-seed --uninstall   # de-schedule managed jobs, then remove the tree

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
import filecmp
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent

# What an install consists of — directories and files copied verbatim.
COMPONENTS = ["_lib", "keyvault", "scheduler", "observability", "demo", "skills", "memory", "memory-mesh", "views"]
# Opt-in only (SEED-065): governance/ never ships via the default COMPONENTS
# copy — a default `install.py --target <ROOT>` is byte-for-byte unchanged
# by this wave. --enable-governance is the explicit "governance: none is
# NOT the default, but activation IS" opt-in, run only when the recipient
# says yes in AGENT-INSTALL.md's governance phase.
OPTIONAL_COMPONENTS = ["governance"]
ROOT_FILES = ["PRINCIPLES.md", "CLAUDE.md.template", "README.md.template", "VERSION"]
# Components whose EXISTING presence in an --into workspace satisfies the
# requirement instead of colliding (see the compose-mode comment in install()).
SATISFIED_BY_EXISTING = {"memory"}

DEMO_MANIFEST_ENTRY = """\
jobs:
  - name: hello_fleet
    schedule: "*/15 * * * *"
    command: >-
      /usr/bin/python3 {root}/observability/log_run.py --job hello_fleet --
      /usr/bin/python3 {root}/demo/hello_fleet.py
"""

# --- Wave 2H: receipt / baseline / gated-write constants -------------------
CC_SEED_DIR = ".cc-seed"
RECEIPT_NAME = "receipt.json"
STAGED_DIR = "staged"
GATED_WRITES = {"claude-md", "mesh-bootstrap"}
MARKER_START = "<!-- cc-seed:start -->"
MARKER_END = "<!-- cc-seed:end -->"
_MAX_HASH_BYTES = 200 * 1024 * 1024  # Principle 8: bound the loop — don't hash unbounded files


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
    for cand in ["~/ai-os-seed", "~/aios", "~/tools/ai-os-seed", "~/ai-os"]:
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
def _anchor_path(target: Path) -> Path:
    slug = hashlib.sha256(str(target).encode()).hexdigest()[:16]
    return Path.home() / ".cache" / "cc-seed" / "receipt-anchors" / f"{slug}.json"


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
        "schema": 1,
        "install": {
            "target": str(target), "mode": mode,
            "installer_version": _installer_version(), "installer_commit": _installer_commit(),
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
    receipt["install"]["components"] = written
    _save_receipt(target, receipt)

    mode = "composed into your existing workspace at" if into else "->"
    print(f"installed {len(written)} components + {len(ROOT_FILES)} files {mode} {target}")
    print(f"install receipt: {target}/{CC_SEED_DIR}/{RECEIPT_NAME}")
    print("next: run the Phase 3 verify commands from AGENT-INSTALL.md")
    return 0


def enable_demo(target: Path):
    manifest = target / "scheduler" / "manifest.yml"
    if not manifest.exists():
        return die(f"{manifest} not found — is {target} an AI-OS Seed install?")
    text = manifest.read_text()
    # Line-wise, comments excluded — the scaffold's commented example also
    # contains "name: hello_fleet" and must not read as already-enabled
    # (caught live: substring check made --enable-demo a silent no-op).
    if any(line.strip().startswith("- name: hello_fleet")
           for line in text.splitlines() if not line.strip().startswith("#")):
        print("hello_fleet already in the scheduler manifest — nothing to do.")
        return 0
    if "jobs: []" not in text:
        return die("scheduler/manifest.yml already has its own jobs — add the "
                   "hello_fleet entry by hand (see the commented example in the "
                   "file) rather than letting me rewrite your manifest.")
    manifest.write_text(text.replace("jobs: []", DEMO_MANIFEST_ENTRY.format(root=target)))
    print(f"hello_fleet (every 15 min) written to {manifest}")
    print(f"next: bash {target}/scheduler/sync.sh")
    return 0


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

def approve(target: Path, which: str):
    receipt = _load_receipt(target)
    if receipt is None:
        return die(f"{target}/{CC_SEED_DIR}/{RECEIPT_NAME} not found — was this target "
                   f"installed with this install.py?")
    if which == "claude-md":
        return _approve_claude_md(target, receipt)
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
        return die(f"memory-mesh/install.sh exited {r.returncode} — not recorded as approved.")
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
    "ROOT>/memory/, outside <ROOT> but a single named path, not a scan). It "
    "did not scan your shell rc files, SSH config, other applications' "
    "config, or anything else outside <ROOT>. A confused install session "
    "can still write there; this audit cannot see it."
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

    runtime_writable_prefixes = ("observability/data/",)
    for live_p in sorted(target.rglob("*")):
        rel = live_p.relative_to(target).as_posix()
        if rel in checked_rel:
            continue
        if rel == CC_SEED_DIR or rel.startswith(CC_SEED_DIR + "/"):
            continue  # install.py's own receipt/staged scaffold
        if rel == "CLAUDE.md":
            continue  # owned by check 3
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


def do_audit(target: Path, package: Path, as_json: bool) -> int:
    receipt = _load_receipt(target)
    if receipt is None:
        print(f"install.py --audit: no receipt at {target}/{CC_SEED_DIR}/{RECEIPT_NAME} — was "
              f"this target installed with this install.py? Nothing to audit.", file=sys.stderr)
        return 2

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
                         "mesh-bootstrap runs memory-mesh/install.sh itself")
    ap.add_argument("--audit", action="store_true",
                    help="deterministic post-install auditor — compares live state against "
                         "the install receipt and --package's pristine manifest")
    ap.add_argument("--package", help="path to the pristine clone dir to audit against (required with --audit)")
    ap.add_argument("--json", action="store_true", help="with --audit, emit the report as JSON")
    ap.add_argument("--uninstall", action="store_true",
                    help="de-schedule managed jobs and remove the install")
    args = ap.parse_args()

    if args.detect:
        if args.target or args.enable_demo or args.enable_governance or args.uninstall or args.approve or args.audit:
            return die("--detect takes no other flags (it's a read-only report)")
        return detect()
    if not args.target:
        return die("--target is required (or use --detect for a read-only survey)")

    target = Path(args.target).expanduser()
    if not target.is_absolute():
        return die(f"--target must be an absolute path, got {args.target!r}")
    exclusive = [args.enable_demo, args.enable_governance, args.uninstall, args.approve, args.audit]
    if sum(bool(x) for x in exclusive) > 1:
        return die("--enable-demo, --enable-governance, --uninstall, --approve, and --audit are mutually exclusive")
    if args.into and any(exclusive):
        return die("--into only applies to the initial install")
    if args.json and not args.audit:
        return die("--json only applies to --audit")
    if args.package and not args.audit:
        return die("--package only applies to --audit")

    if args.uninstall:
        return uninstall(target)
    if args.enable_demo:
        return enable_demo(target)
    if args.enable_governance:
        return enable_governance(target)
    if args.approve:
        return approve(target, args.approve)
    if args.audit:
        if not args.package:
            return die("--audit requires --package <clone-dir>")
        return do_audit(target, Path(args.package).expanduser(), args.json)
    return install(target, into=args.into)


if __name__ == "__main__":
    sys.exit(main())
