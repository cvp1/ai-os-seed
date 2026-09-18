#!/usr/bin/env python3
"""Test matrix for memory-write-guard.py — the DENY cases matter most.

A guard change is only safe if the things it used to stop still stop. Exit 2 =
denied, exit 0 = allowed.
"""
import json
import os
import subprocess
import sys

# Defaults to the INSTALLED hook; pass a path to vet a candidate before it goes
# live. The hook is symlinked from ~/.claude/hooks, so editing it in place is
# editing the running guard -- a candidate gets proved here first.
HOOK = sys.argv[1] if len(sys.argv) > 1 else \
    os.path.join(os.path.dirname(os.path.realpath(__file__)), "memory-write-guard.py")
_ws = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
STORE = os.environ.get("MEMORY_WRITE_GUARD_STORE") or os.path.realpath(os.path.join(
    os.path.expanduser("~"), ".claude", "projects", _ws.replace("/", "-"), "memory"))

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
     'git -C /home/{{REDACTED}}/{{REDACTED}}/memory-mesh commit -qm "fix: an event '
     'never folds into MEMORY.md; see events/<host>.ndjson"',
     "commit message naming MEMORY.md and containing >"),
    (False, "Bash", 'git commit -m "rewrite MEMORY.md handling"',
     "commit message with MEMORY.md + rm-ish word"),
    (False, "Bash", "cat MEMORY.md 2>/dev/null", "read with /dev/null"),
    (False, "Bash", f"grep foo {STORE}/MEMORY.md", "plain read"),
    (False, "Bash",
     f"/usr/bin/python3 memory-mesh/memory_write.py write "
     f"--slug x --commit > /tmp/out", "sanctioned writer"),
    (False, "Write", "/home/{{REDACTED}}/{{REDACTED}}/notes.md", "Write outside store"),

    # --- 2026-07-30: the four false positives that motivated the narrowing ---
    (False, "Bash",
     "cat > /home/{{REDACTED}}/{{REDACTED}}/evals/reviews/note.md <<'EOF'\n"
     "The fold writes MEMORY.md every five minutes, and QUARANTINE.md\n"
     "holds what it will not serve.\nEOF",
     "heredoc doc write whose PROSE names the store"),
    (False, "Bash",
     "cat >> /home/{{REDACTED}}/{{REDACTED}}/memory-mesh/README.md <<'EOF'\n"
     "See MEMORY.md for the generated index.\nEOF",
     "README append mentioning MEMORY.md"),
    (False, "Bash",
     "echo x > /home/{{REDACTED}}/{{REDACTED}}/memory-mesh/MEMORY.md",
     "a MEMORY.md that is NOT the store"),
    (False, "Bash",
     "rm /home/{{REDACTED}}/{{REDACTED}}/docs/notes-about-MEMORY.md",
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
]

# The deny message must NAME the token that matched, so the operator can see
# which shape tripped it instead of guessing (2026-07-31).
MESSAGE_CASES = [
    (f"""wc -c {STORE}/MEMORY.md; python3 -c "print(1, '->', 2)" """, ">"),
    (f'echo poison > {STORE}/MEMORY.md', ">"),
    (f'cp /tmp/x {STORE}/MEMORY.md', "cp"),
]


def run(tool, payload, want_stderr=False):
    if tool == "Bash":
        event = {"tool_name": "Bash", "tool_input": {"command": payload}}
    else:
        event = {"tool_name": tool, "tool_input": {"file_path": payload}}
    p = subprocess.run([sys.executable, HOOK], input=json.dumps(event),
                       capture_output=True, text=True, timeout=30)
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

    total = len(CASES) + len(MESSAGE_CASES)
    print(f"\n{total - bad} passed, {bad} failed")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
