#!/usr/bin/env python3
"""Test matrix for memory-write-guard.py — the DENY cases matter most.

A guard change is only safe if the things it used to stop still stop. Exit 2 =
denied, exit 0 = allowed.
"""
import json
import os
import subprocess
import sys
import tempfile

# Defaults to the INSTALLED hook; pass a path to vet a candidate before it goes
# live. The hook is symlinked from ~/.claude/hooks, so editing it in place is
# editing the running guard -- a candidate gets proved here first.
HOOK = sys.argv[1] if len(sys.argv) > 1 else \
    os.path.join(os.path.dirname(os.path.realpath(__file__)), "memory-write-guard.py")
_ws = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
STORE = os.environ.get("MEMORY_WRITE_GUARD_STORE") or os.path.realpath(os.path.join(
    os.path.expanduser("~"), ".claude", "projects", _ws.replace("/", "-"), "memory"))
# 2026-09-19 (round 2): the sanctioned door is a FILE identity, not a basename,
# so the cases must name the door of the guard UNDER TEST -- one directory up
# from the hook. Hardcoding this workspace's path made the shipped selftest
# wrong for every install but this one. WORKSPACE is the base a
# workspace-relative invocation resolves against.
DOOR = os.path.realpath(os.path.join(
    os.path.dirname(os.path.dirname(os.path.realpath(HOOK))), "memory_write.py"))
WORKSPACE = os.path.dirname(os.path.dirname(DOOR))
MESH = os.path.basename(os.path.dirname(DOOR))
# A file that is NOT the door but wears its name -- the round-2 P1. A fixed
# path under the temp dir: rewritten each run, never accumulating, never
# removed (nothing in this tree gets rm'd).
MESH_DIR = os.path.dirname(DOOR)
IMPOSTOR_DIR = os.path.join(tempfile.gettempdir(),
                            "memory-write-guard-selftest-impostor")
os.makedirs(IMPOSTOR_DIR, exist_ok=True)
with open(os.path.join(IMPOSTOR_DIR, "memory_write.py"), "w") as _fh:
    _fh.write("#!/usr/bin/env python3\nraise SystemExit('not the door')\n")

CASES = [
    # (must_deny, tool, payload, label)
    # --- must still DENY: real writes to the store ---
    (True, "Bash", f"echo poison > {STORE}/MEMORY.md", "redirect into store"),
    (True, "Bash", 'bash -c "echo x > MEMORY.md"', "bash -c bypass attempt"),
    (True, "Bash", "sed -i 's/a/b/' MEMORY.md", "sed -i on MEMORY.md"),
    (True, "Bash", f"cp /tmp/evil {STORE}/QUARANTINE.md", "cp into store"),
    (True, "Bash", f"rm {STORE}/some-memory.md", "rm in store"),
    (True, "Bash", "tee MEMORY.md < /tmp/x", "tee MEMORY.md"),
    (True, "Write", f"{STORE}/x.md", "Write tool into store"),
    (True, "Edit", f"{STORE}/MEMORY.md", "Edit tool into store"),
    # a commit message must not become a laundering channel for a REAL write
    (True, "Bash", f'git commit -m "note" && echo x > {STORE}/MEMORY.md',
     "commit chained with a real write"),

    # --- 2026-07-30 narrowing: the deny set must not move ---
    # A heredoc BODY is stripped as data; the redirect lives on the command
    # line, so a heredoc aimed INTO the store must still deny.
    (True, "Bash", f"cat > {STORE}/MEMORY.md <<'EOF'\npoison\nEOF",
     "heredoc redirected into the store"),
    (True, "Bash", "cat > MEMORY.md <<'EOF'\npoison\nEOF",
     "heredoc into a bare MEMORY.md"),
    # A heredoc piped to a shell EXECUTES its body -- it is code, not data.
    (True, "Bash", "cat <<'EOF' | bash\necho x > MEMORY.md\nEOF",
     "heredoc body piped to a shell"),
    # Path resolution must not be fooled by traversal back into the store.
    (True, "Bash", f"echo x > {STORE}/../memory/MEMORY.md",
     "traversal resolving back into the store"),
    (True, "Bash", f"mv /tmp/evil {STORE}/MEMORY.md", "mv into store"),

    # --- must ALLOW: the false positives ---
    (False, "Bash",
     f'git -C {WORKSPACE}/{MESH} commit -qm "fix: an event '
     'never folds into MEMORY.md; see events/<host>.ndjson"',
     "commit message naming MEMORY.md and containing >"),
    (False, "Bash", 'git commit -m "rewrite MEMORY.md handling"',
     "commit message with MEMORY.md + rm-ish word"),
    (False, "Bash", "cat MEMORY.md 2>/dev/null", "read with /dev/null"),
    (False, "Bash", f"grep foo {STORE}/MEMORY.md", "plain read"),
    (False, "Bash",
     f"/usr/bin/python3 {MESH}/memory_write.py write "
     f"--slug x --commit > /tmp/out", "sanctioned writer"),
    (False, "Write", f"{WORKSPACE}/notes.md", "Write outside store"),

    # --- 2026-07-30: the four false positives that motivated the narrowing ---
    (False, "Bash",
     f"cat > {WORKSPACE}/evals/reviews/note.md <<'EOF'\n"
     "The fold writes MEMORY.md every five minutes, and QUARANTINE.md\n"
     "holds what it will not serve.\nEOF",
     "heredoc doc write whose PROSE names the store"),
    (False, "Bash",
     f"cat >> {WORKSPACE}/{MESH}/README.md <<'EOF'\n"
     "See MEMORY.md for the generated index.\nEOF",
     "README append mentioning MEMORY.md"),
    (False, "Bash",
     f"echo x > {WORKSPACE}/{MESH}/MEMORY.md",
     "a MEMORY.md that is NOT the store"),
    (False, "Bash",
     f"rm {WORKSPACE}/docs/notes-about-MEMORY.md",
     "a filename merely containing MEMORY.md"),
    # The guard denied this shape minutes after the narrowing shipped: a
    # /dev/null redirect inside a command substitution ends at `)`, which was
    # not in the terminator set, so the `>` survived and read as a write.
    (False, "Bash",
     f"n=$(grep -c foo {STORE}/MEMORY.md 2>/dev/null); echo $n",
     "/dev/null redirect ending at a closing paren"),
    (False, "Bash",
     f'echo "$(grep -c foo {STORE}/MEMORY.md 2>/dev/null)"',
     "/dev/null redirect ending at a quote"),

    # 2026-07-31 arrow class. These PIN a known false positive as DENY on
    # purpose -- they are not aspirational allows. `>` is a bash metacharacter,
    # so `echo hello->f` really does create the file `f` (verified against bash
    # before writing these). An arrow is therefore indistinguishable from a
    # redirect at this layer, and exempting one would allow
    # `echo poison->MEMORY.md`. If a future change flips any of these to
    # allow, that change has opened a write path -- prove otherwise first.
    (True, "Bash",
     f"""wc -c {STORE}/MEMORY.md; python3 -c "print(1, '->', 2)" """,
     "arrow in a python string, store read in a sibling segment"),
    (True, "Bash", f'wc -c {STORE}/MEMORY.md; echo "a -> b"',
     "arrow inside a quoted echo"),
    (True, "Bash", f'wc -c {STORE}/MEMORY.md; python3 -c "print(3 >= 2)"',
     "a >= comparison operator"),
    (True, "Bash", f'wc -c {STORE}/MEMORY.md; git log --format="%h -> %s" -1',
     "arrow in a git --format spec"),
    (True, "Bash", f'echo "poison"->{STORE}/MEMORY.md',
     "arrow that IS a redirect into the store -- why the class cannot be exempted"),

    # --- SEED-081 (gpt-6-astra, 2026-09-19): two holes in the SAME-DAY fix ---
    # Both were ALLOWED (rc=0) against the morning's guard and are the control
    # for these three cases.
    (True, "Bash",
     f"cd {MESH_DIR} && cd {IMPOSTOR_DIR} && python3 memory_write.py write "
     f"> {STORE}/probe.md",
     "a legitimate cd does not sanction an impostor reached by a LATER cd"),
    (True, "Bash", f"python3 {DOOR} --help > {STORE}/probe.md",
     "the REAL door does not sanction a shell redirect into the store"),
    (False, "Bash", f"cd {MESH_DIR} && python3 memory_write.py write --commit",
     "...and a genuine door call with no redirect still runs"),

    # --- 2026-09-19: the store spelled through a symlink or `..` (P0) ---
    # store_referenced matched STORE as a SUBSTRING, so the same file written
    # under an equivalent spelling was not "the store" at all and every guard
    # below it went quiet. Caught on macos-latest, where the sandbox store is
    # /var/folders/... and its realpath is /private/var/folders/... -- but the
    # `..` form below reproduces it on Linux too, so it is a real bypass, not
    # a platform quirk. Control: both were rc=0 (ALLOWED) before the fix.
    (True, "Bash", f'printf poison > {STORE}/../memory/x.md  # memory_write.py',
     "store reached through a `..` segment, wearing the door's name"),
    (True, "Bash", f'printf poison > {STORE}/./sub/../x.md',
     "store reached through `.` and `..`"),
    (False, "Bash", 'printf ok > /tmp/not-the-store/../elsewhere.md',
     "a `..` path that does NOT land in the store is untouched"),

    # --- 2026-09-19: the SANCTIONED substring bypass (P0) ---
    # `SANCTIONED = "memory_write.py"` + `SANCTIONED not in cmd` made the
    # ELEVEN CHARACTERS the credential: any occurrence anywhere exempted the
    # whole command. Sanctioning is now structural and per-segment. Each of
    # these was ALLOWED (rc=0) before the fix; (a) really created its file.
    (True, "Bash", f"printf poison > {STORE}/bypass.md  # memory_write.py",
     "trailing-comment bypass"),
    (True, "Bash", f"echo memory_write.py > {STORE}/x.md",
     "the writer's name as DATA being written"),
    (True, "Bash",
     f"/usr/bin/python3 {MESH}/memory_write.py --selftest && "
     f"printf x > {STORE}/sibling.md",
     "a sanctioned segment must not sanction its sibling"),
    (True, "Bash", f'echo "memory_write.py" | tee {STORE}/MEMORY.md',
     "writer name in a quoted string piped to tee"),
    (True, "Bash", f"cp /tmp/evil {STORE}/MEMORY.md # memory_write.py did it",
     "comment bypass on a cp"),
    # ...and the genuine writer must still be allowed, by either spelling.
    (False, "Bash",
     f"/usr/bin/python3 {MESH}/memory_write.py write --slug x "
     f"--text 'the fold writes MEMORY.md' --commit",
     "genuine writer, workspace-relative path, prose naming the store"),
    (False, "Bash",
     f"/usr/bin/python3 {DOOR} "
     f"write --slug x --text 'see {STORE}/MEMORY.md' --commit",
     "genuine writer, absolute path, store path in an argument"),
    (False, "Bash",
     f"cd {WORKSPACE} && python3 {MESH}/memory_write.py "
     f"write --slug x --text 'MEMORY.md' --commit",
     "genuine writer behind a cd"),
    (False, "Bash",
     f"env python3 {DOOR} write --slug x --text 'MEMORY.md' --commit",
     "genuine writer behind env"),
    # An unparseable command DENIES rather than failing open.
    (True, "Bash", f"printf x > {STORE}/MEMORY.md \"unbalanced",
     "shlex parse error denies (fail closed)"),

    # --- 2026-09-19 round 2 (R1): the IMPOSTOR door (P1, grok-4.6) ---
    # Sanction was `basename(prog) == "memory_write.py"`, so any file anywhere
    # with that name was the door. Both of these were ALLOWED (rc=0): a silent
    # store write with no lineage, wearing the writer's name. Sanction is now
    # realpath-identity with THIS install's own door.
    (True, "Bash",
     f"python3 {IMPOSTOR_DIR}/memory_write.py write --text 'MEMORY.md' "
     f"--commit > {STORE}/impersonate.md",
     "an impostor memory_write.py by absolute path"),
    (True, "Bash",
     f"cd {IMPOSTOR_DIR} && python3 memory_write.py write --commit "
     f"> {STORE}/impersonate.md",
     "an impostor memory_write.py reached by cd"),
    (True, "Bash",
     f"python3 /nonexistent-dir/memory_write.py write > {STORE}/x.md",
     "a door token that resolves to no file at all denies"),

    # --- 2026-09-19 round 2 (R3): inline code in a language runtime (P2) ---
    # WRITE_SHAPE reads shell. These three write the store with no redirect,
    # and all three were ALLOWED before this fix.
    (True, "Bash",
     f"python3 -c \"import pathlib; pathlib.Path('{STORE}/x.md')"
     f".write_text('poison')\"",
     "python3 -c writing the store through pathlib"),
    (True, "Bash",
     f"node -e \"require('fs').writeFileSync('{STORE}/x.md','poison')\"",
     "node -e writeFileSync into the store"),
    (True, "Bash",
     f"ruby -e \"File.write('{STORE}/x.md','poison')\"",
     "ruby -e File.write into the store"),
    (True, "Bash",
     f"perl -e \"open(F,'>','{STORE}/x.md')\"",
     "perl -e into the store"),
    (True, "Bash",
     f"python3 <<'EOF'\nimport pathlib\n"
     f"pathlib.Path('{STORE}/x.md').write_text('poison')\nEOF",
     "a heredoc PROGRAM fed to python3 -- the body is code, not prose"),
    # ...and inline code that does not name the store is untouched.
    (False, "Bash", 'python3 -c "print(1)"',
     "inline code that never names the store"),
    (False, "Bash", "node -e \"console.log('hi')\"",
     "node -e that never names the store"),
]

# The deny message must NAME the token that matched, so the operator can see
# which shape tripped it instead of guessing (2026-07-31).
MESSAGE_CASES = [
    (f"""wc -c {STORE}/MEMORY.md; python3 -c "print(1, '->', 2)" """, ">"),
    (f'echo poison > {STORE}/MEMORY.md', ">"),
    (f'cp /tmp/x {STORE}/MEMORY.md', "cp"),
]

# The deny must say WHY the command was not sanctioned, so an operator who
# expected the writer exemption can see which segment failed to earn it.
WHY_CASES = [
    (f"printf poison > {STORE}/bypass.md  # memory_write.py",
     "not this install's memory_write.py"),
    (f"printf x > {STORE}/MEMORY.md \"unbalanced", "could not parse"),
    # R1: the deny must name the ONE door it will accept, so an operator who
    # ran a namesake can see which file the guard means.
    (f"python3 {IMPOSTOR_DIR}/memory_write.py write --commit > {STORE}/i.md",
     DOOR),
    # R3: and say that inline code is what tripped it.
    (f"python3 -c \"open('{STORE}/x.md','w')\"", "INLINE CODE"),
]


def run(tool, payload, want_stderr=False):
    if tool == "Bash":
        event = {"tool_name": "Bash", "tool_input": {"command": payload}}
    else:
        event = {"tool_name": tool, "tool_input": {"file_path": payload}}
    # cwd = the MESH DIRECTORY on purpose: that is the adversarial one. A
    # relative `memory_write.py` resolves to the real door from here, so any
    # case that lets the process cwd stand in for where the command actually
    # `cd`-ed will sanction an impostor and show up as a FAIL. Run from
    # anywhere else, the impostor-by-cd case passes for the wrong reason —
    # which is how it stayed green on {{REDACTED}} while failing on {{REDACTED}}
    # (2026-09-19).
    p = subprocess.run([sys.executable, HOOK], input=json.dumps(event),
                       capture_output=True, text=True, timeout=30,
                       cwd=os.path.dirname(DOOR))
    return (p.returncode, p.stderr) if want_stderr else p.returncode


def main():
    bad = 0
    for must_deny, tool, payload, label in CASES:
        rc = run(tool, payload)
        denied = rc == 2
        ok = denied == must_deny
        if not ok:
            bad += 1
        want = "DENY" if must_deny else "allow"
        got = "DENY" if denied else "allow"
        print(f"  {'PASS' if ok else 'FAIL'}  want={want:<5} got={got:<5} {label}")

    for cmd, token in MESSAGE_CASES:
        rc, err = run("Bash", cmd, want_stderr=True)
        ok = rc == 2 and repr(token) in err
        if not ok:
            bad += 1
        print(f"  {'PASS' if ok else 'FAIL'}  deny message names {token!r}")

    for cmd, phrase in WHY_CASES:
        rc, err = run("Bash", cmd, want_stderr=True)
        ok = rc == 2 and phrase in err
        if not ok:
            bad += 1
        print(f"  {'PASS' if ok else 'FAIL'}  deny message explains {phrase!r}")

    total = len(CASES) + len(MESSAGE_CASES) + len(WHY_CASES)
    print(f"\n{total - bad} passed, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
