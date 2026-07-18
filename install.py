#!/usr/bin/env python3
"""AI-OS Seed installer — the deterministic byte-mover behind AGENT-INSTALL.md.

The agent (or a human) orchestrates and decides; this script is the only
thing that writes install content, so what lands in --target is
byte-identical to this repo, never transcribed by a model.

    install.py --detect                            # read-only: report prior installs on this machine
    install.py --target ~/ai-os-seed               # copy the substrate in
    install.py --target ~/ai-os-seed --enable-demo # add hello_fleet to the scheduler manifest
    install.py --target ~/ai-os-seed --uninstall   # de-schedule managed jobs, then remove the tree

Stdlib only. Refuses to overwrite a non-empty target; uninstall asks the
scheduler to drop its managed jobs before deleting anything, and refuses a
target that doesn't look like one of ours (degrade toward safety).
"""
import argparse
import filecmp
import re
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# What an install consists of — directories and files copied verbatim.
COMPONENTS = ["_lib", "keyvault", "scheduler", "observability", "demo", "skills", "memory", "views"]
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
    written = [c for c in COMPONENTS if c not in skipped]
    for comp in written:
        shutil.copytree(HERE / comp, target / comp)
    for f in ROOT_FILES:
        shutil.copy2(HERE / f, target / f)
    mode = "composed into your existing workspace at" if into else "->"
    print(f"installed {len(written)} components + {len(ROOT_FILES)} files {mode} {target}")
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
    for name in COMPONENTS + ROOT_FILES:
        p = target / name
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
        if p.is_dir():
            shutil.rmtree(p)
        elif p.exists():
            p.unlink()
    leftover = sorted(p.name for p in target.iterdir())
    if leftover:
        print(f"removed the seed's components from {target}.")
        print(f"left untouched (yours, not the seed's): {', '.join(leftover[:10])}"
              + (" …" if len(leftover) > 10 else ""))
    else:
        target.rmdir()
        print(f"removed {target}. That's the whole footprint — nothing else was installed.")
    return 0


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
    ap.add_argument("--uninstall", action="store_true",
                    help="de-schedule managed jobs and remove the install")
    args = ap.parse_args()

    if args.detect:
        if args.target or args.enable_demo or args.uninstall:
            return die("--detect takes no other flags (it's a read-only report)")
        return detect()
    if not args.target:
        return die("--target is required (or use --detect for a read-only survey)")

    target = Path(args.target).expanduser()
    if not target.is_absolute():
        return die(f"--target must be an absolute path, got {args.target!r}")
    if args.enable_demo and args.uninstall:
        return die("--enable-demo and --uninstall are mutually exclusive")
    if args.into and (args.enable_demo or args.uninstall):
        return die("--into only applies to the initial install")

    if args.uninstall:
        return uninstall(target)
    if args.enable_demo:
        return enable_demo(target)
    return install(target, into=args.into)


if __name__ == "__main__":
    sys.exit(main())
