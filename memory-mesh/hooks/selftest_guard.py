#!/usr/bin/env python3
"""Test matrix for memory-write-guard.py (exit 2 = denied, 0 = allowed).

Usage: selftest_guard.py [HOOK_PATH]  (defaults to the installed hook)
"""
import json
import os
import subprocess
import sys
import tempfile

# Hook under test: argv[1], or the installed one beside this file.
HOOK = sys.argv[1] if len(sys.argv) > 1 else \
    os.path.join(os.path.dirname(os.path.realpath(__file__)), "memory-write-guard.py")
_ws = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
STORE = os.environ.get("MEMORY_WRITE_GUARD_STORE") or os.path.realpath(os.path.join(
    os.path.expanduser("~"), ".claude", "projects", _ws.replace("/", "-"), "memory"))
# The sanctioned door is the memory_write.py one directory above the hook
# under test; WORKSPACE is what workspace-relative invocations resolve against.
DOOR = os.path.realpath(os.path.join(
    os.path.dirname(os.path.dirname(os.path.realpath(HOOK))), "memory_write.py"))
WORKSPACE = os.path.dirname(os.path.dirname(DOOR))
MESH = os.path.basename(os.path.dirname(DOOR))
# An impostor memory_write.py at a fixed temp path, rewritten each run.
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

    # --- heredocs and traversal ---
    # A heredoc redirected into the store must deny.
    (True, "Bash", f"cat > {STORE}/MEMORY.md <<'EOF'\npoison\nEOF",
     "heredoc redirected into the store"),
    (True, "Bash", "cat > MEMORY.md <<'EOF'\npoison\nEOF",
     "heredoc into a bare MEMORY.md"),
    # A heredoc piped to a shell executes its body.
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

    # --- must ALLOW: prose and non-store paths naming the store ---
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
    # /dev/null redirects ending at `)` or a quote are reads.
    (False, "Bash",
     f"n=$(grep -c foo {STORE}/MEMORY.md 2>/dev/null); echo $n",
     "/dev/null redirect ending at a closing paren"),
    (False, "Bash",
     f'echo "$(grep -c foo {STORE}/MEMORY.md 2>/dev/null)"',
     "/dev/null redirect ending at a quote"),

    # Arrow class: intentionally DENY. `->` is a real redirect in bash, so it
    # cannot be exempted without opening `echo poison->MEMORY.md`.
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

    # --- cd sequencing and redirects from the real door ---
    (True, "Bash",
     f"cd {MESH_DIR} && cd {IMPOSTOR_DIR} && python3 memory_write.py write "
     f"> {STORE}/probe.md",
     "a legitimate cd does not sanction an impostor reached by a LATER cd"),
    (True, "Bash", f"python3 {DOOR} --help > {STORE}/probe.md",
     "the REAL door does not sanction a shell redirect into the store"),
    (False, "Bash", f"cd {MESH_DIR} && python3 memory_write.py write --commit",
     "...and a genuine door call with no redirect still runs"),

    # --- the store spelled through `.`/`..` or a symlink ---
    (True, "Bash", f'printf poison > {STORE}/../memory/x.md  # memory_write.py',
     "store reached through a `..` segment, wearing the door's name"),
    (True, "Bash", f'printf poison > {STORE}/./sub/../x.md',
     "store reached through `.` and `..`"),
    (False, "Bash", 'printf ok > /tmp/not-the-store/../elsewhere.md',
     "a `..` path that does NOT land in the store is untouched"),

    # --- the writer's name appearing in a command must not sanction it ---
    # Sanctioning is structural and per-segment.
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

    # --- impostor doors: sanction is realpath identity with this install's door ---
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

    # --- inline code in a language runtime writing the store ---
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

    # --- unquoted heredocs, copy/link tools, split paths ---
    # An unquoted heredoc expands $(...) and backticks, so its body is code.
    (True, "Bash", f"cat <<EOF\n$(printf poison > {STORE}/heredoc.md)\nEOF",
     "unquoted heredoc body with $(...) writing the store"),
    (True, "Bash", f"cat <<EOF\n`printf poison > {STORE}/heredoc.md`\nEOF",
     "unquoted heredoc body with backticks writing the store"),
    # Copy/link tools the write-shape list did not know.
    (True, "Bash", f"scp /tmp/evil {STORE}/scp.md", "scp into store"),
    (True, "Bash", f"rsync /tmp/evil {STORE}/rsync.md", "rsync into store"),
    (True, "Bash", f"install -m644 /tmp/evil {STORE}/install.md",
     "install into store"),
    (True, "Bash", f"ln -sfn /tmp/outside.txt {STORE}/link.md",
     "ln -sfn into store"),
    (True, "Bash", f"ln /tmp/outside.txt {STORE}/hard.md", "hard ln into store"),
    (True, "Bash", f"/bin/cp -t {STORE} /tmp/evil", "cp -t by absolute path"),
    # A partly-quoted delimiter (`<<E"OF"` means EOF) must not hide later lines.
    (True, "Bash", f"cat <<E\"OF\"\nhi\nEOF\nprintf poison > {STORE}/late.md",
     "partly-quoted heredoc delimiter must not swallow a later write"),
    # The store path split across string concatenation in inline code.
    (True, "Bash",
     f"python3 -c \"open('{os.path.dirname(STORE)}' + '/{os.path.basename(STORE)}"
     f"/concat.md','w').write('poison')\"",
     "python3 -c with the store path split by concatenation"),
    (True, "Bash",
     f"python3 -c \"import pathlib; pathlib.Path('{os.path.dirname(STORE)}', "
     f"'{os.path.basename(STORE)}', 'x.md').write_text('poison')\"",
     "python3 -c pathlib join of the store's parent + basename"),
    (True, "Bash",
     f"node -e \"require('fs').writeFileSync('{os.path.dirname(STORE)}'+"
     f"'/{os.path.basename(STORE)}/x.md','p')\"",
     "node -e split path writeFileSync"),
    (True, "Bash",
     f"python3 <<'EOF'\nimport pathlib\n"
     f"pathlib.Path('{os.path.dirname(STORE)}' + '/{os.path.basename(STORE)}/x.md')"
     f".write_text('poison')\nEOF",
     "heredoc program with the store path split by concatenation"),
    # ...a quoted heredoc is still prose, and a read of the store's parent is
    # still a read; the plain read/copy-out shapes stay allowed.
    (False, "Bash",
     f"cat > {WORKSPACE}/evals/reviews/note.md <<'EOF'\n"
     f"$(printf x > {STORE}/x.md) is how an attack would look\nEOF",
     "QUOTED heredoc body with $(...) is inert prose"),
    (False, "Bash",
     f"python3 -c \"import os; print(os.listdir('{os.path.dirname(STORE)}'))\"",
     "inline READ of the store's parent with no write shape"),
    (False, "Bash", "rsync -a /tmp/a/ /tmp/b/", "rsync that never names the store"),
]

# The deny message must name the token that matched.
MESSAGE_CASES = [
    (f"""wc -c {STORE}/MEMORY.md; python3 -c "print(1, '->', 2)" """, ">"),
    (f'echo poison > {STORE}/MEMORY.md', ">"),
    (f'cp /tmp/x {STORE}/MEMORY.md', "cp"),
]

# The deny message must say why the command was not sanctioned.
WHY_CASES = [
    (f"printf poison > {STORE}/bypass.md  # memory_write.py",
     "not this install's memory_write.py"),
    (f"printf x > {STORE}/MEMORY.md \"unbalanced", "could not parse"),
    # names the one door it accepts
    (f"python3 {IMPOSTOR_DIR}/memory_write.py write --commit > {STORE}/i.md",
     DOOR),
    # names inline code as the cause
    (f"python3 -c \"open('{STORE}/x.md','w')\"", "INLINE CODE"),
]


def run(tool, payload, want_stderr=False):
    if tool == "Bash":
        event = {"tool_name": "Bash", "tool_input": {"command": payload}}
    else:
        event = {"tool_name": tool, "tool_input": {"file_path": payload}}
    # Run from the mesh dir: a relative memory_write.py resolves to the real
    # door here, which makes the impostor-by-cd case meaningful.
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
