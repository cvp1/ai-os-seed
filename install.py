#!/usr/bin/env python3
"""AI-OS Seed installer — the deterministic byte-mover behind AGENT-INSTALL.md.

The agent (or a human) orchestrates and decides; this script is the only
thing that writes install content, so what lands in --target is
byte-identical to this repo, never transcribed by a model.

    install.py --target ~/aios                # copy the substrate in
    install.py --target ~/aios --enable-demo  # add hello_fleet to the scheduler manifest
    install.py --target ~/aios --uninstall    # de-schedule managed jobs, then remove the tree

Stdlib only. Refuses to overwrite a non-empty target; uninstall asks the
scheduler to drop its managed jobs before deleting anything, and refuses a
target that doesn't look like one of ours (degrade toward safety).
"""
import argparse
import shutil
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

# What an install consists of — directories and files copied verbatim.
COMPONENTS = ["_lib", "keyvault", "scheduler", "observability", "demo", "skills"]
ROOT_FILES = ["PRINCIPLES.md", "CLAUDE.md.template", "README.md.template", "VERSION"]

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


def install(target: Path):
    if target.exists() and any(target.iterdir()):
        return die(f"target {target} exists and is not empty — refusing to "
                   f"overwrite. Pick an empty/new directory.")
    missing = [c for c in COMPONENTS + ROOT_FILES if not (HERE / c).exists()]
    if missing:
        return die(f"this clone is incomplete (missing: {', '.join(missing)}) — "
                   f"re-clone rather than installing from a partial tree.")
    target.mkdir(parents=True, exist_ok=True)
    for comp in COMPONENTS:
        shutil.copytree(HERE / comp, target / comp)
    for f in ROOT_FILES:
        shutil.copy2(HERE / f, target / f)
    print(f"installed {len(COMPONENTS)} components + {len(ROOT_FILES)} files -> {target}")
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


def uninstall(target: Path):
    sync = target / "scheduler" / "sync.py"
    manifest = target / "scheduler" / "manifest.yml"
    if not (sync.exists() and manifest.exists() and (target / "PRINCIPLES.md").exists()):
        return die(f"{target} doesn't look like an AI-OS Seed install — refusing "
                   f"to delete it. Remove it yourself if you're sure.")
    # De-schedule first: empty the manifest, let sync reconcile (removes the
    # managed crontab block / launchd plists), then remove the tree.
    manifest.write_text("jobs: []\n")
    r = subprocess.run([sys.executable, str(sync)], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"warning: scheduler cleanup reported: {r.stderr.strip() or r.stdout.strip()}",
              file=sys.stderr)
        print("continuing with tree removal; check `crontab -l` / launchctl yourself.",
              file=sys.stderr)
    else:
        print("scheduled jobs removed.")
    shutil.rmtree(target)
    print(f"removed {target}. That's the whole footprint — nothing else was installed.")
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--target", required=True, help="install root (absolute path)")
    ap.add_argument("--enable-demo", action="store_true",
                    help="add the hello_fleet demo to the scheduler manifest")
    ap.add_argument("--uninstall", action="store_true",
                    help="de-schedule managed jobs and remove the install")
    args = ap.parse_args()

    target = Path(args.target).expanduser()
    if not target.is_absolute():
        return die(f"--target must be an absolute path, got {args.target!r}")
    if args.enable_demo and args.uninstall:
        return die("--enable-demo and --uninstall are mutually exclusive")

    if args.uninstall:
        return uninstall(target)
    if args.enable_demo:
        return enable_demo(target)
    return install(target)


if __name__ == "__main__":
    sys.exit(main())
