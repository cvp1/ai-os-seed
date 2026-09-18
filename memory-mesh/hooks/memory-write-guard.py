#!/usr/bin/env python3
"""PreToolUse guard: block direct mutation of the auto-memory store.

MemGhost (arXiv 2607.05189, "When Claws Remember but Do Not Tell") demonstrated
a single crafted email getting a live Claude Code SDK agent to silently write
a false, persistent memory fact -- 71.4% success rate in background mode. The
/improve and /capture skills already gate memory writes behind
memory_write.py's `lineage:` field (Story 029, OWASP ASI06: contains-untrusted
facts land in QUARANTINE.md, never MEMORY.md, until Craig promotes them --
enforced at fold time since 2026-07-30, mesh_lib.fold_events) -- but until now
that gate was prose discipline only. Nothing stopped a session mid-task on
untrusted content (triage, {{REDACTED}}-brief, ai-pulse itself) from just using
Write/Edit or a shell redirect on MEMORY.md directly, skipping the lineage tag
and the quarantine route entirely. This hook makes the gate a technical
boundary instead of an honor system.

Contract (Claude Code PreToolUse hook): JSON event on stdin
(tool_name, tool_input). To block: print the reason to STDERR and exit 2
(stderr is fed back to the model). To allow: exit 0 with no output.
Fail-open on any error the guard can't parse -- a broken guard must never
brick a legitimate write.

Known false positive (accepted, fail-closed): a read of the store redirected
to a real file (`grep foo MEMORY.md > /tmp/out`) trips the write-shape check.
Deciding that case needs redirect-TARGET analysis -- knowing that the store is
only an input and /tmp/out is the only output -- across chains, pipelines and
subshells. That is a much larger parser with real bypass surface (`cat store >
a; echo x > store`), and it is not worth buying a formatting convenience with
a hole in the one control standing between a poisoned page and permanent
memory. The deny message says how to re-run; a security gate errs closed on
ambiguity.
Installed 2026-07-22, Craig ran the installer himself (MemGhost response).

2026-07-26 (Craig applied this patch himself): two redirection forms that
CANNOT write a file are stripped before the write-shape scan -- `/dev/null`
targets and fd duplications (`2>&1`). Both appear in ordinary reads
(`cat MEMORY.md 2>/dev/null`), and the bare `>` in the write-shape pattern
was denying them. This narrows the check ONLY where a write is impossible by
construction; every genuinely ambiguous shape still denies. See NOT_A_WRITE.

2026-07-28 (Craig authorized): a `git commit` MESSAGE was being scanned as if
it were shell syntax. A commit in an unrelated repo was denied because its
message said "MEMORY.md" and contained `events/<host>.ndjson` -- whose `>` hit
WRITE_SHAPE. Neither can write anything. Now the -m/-F argument of a git commit
is stripped before both tests. Deliberately NOT a general "strip quoted
strings": that would open `bash -c "echo x > MEMORY.md"`, which must keep
denying. A commit cannot mutate the store -- the file write it records would
already have been caught at the moment it happened. Verified with a 15-case
matrix covering every deny shape, including a commit chained to a real write.
See strip_commit_message().

2026-07-30 (Craig authorized): two narrowings, after the guard false-positived
four times in one session on work that could not touch the store -- a heredoc
writing a review doc, an append to a project README, and a byte measurement.
The Write branch was always right, because it resolves the real path through
in_store(); the Bash branch was matching the literal string "MEMORY.md"
anywhere in the command and calling that a reference to the store.

  1. HEREDOC BODIES are data, for the same reason a commit message is
     (strip_heredocs). A body cannot write anything: the redirect that makes
     `cat > file <<EOF` a write sits on the COMMAND line, outside the body, so
     stripping the body leaves every write shape visible. The one exception is
     a body piped into an interpreter -- `cat <<EOF | bash` really does execute
     its body -- so a heredoc whose command line contains a pipe is NOT
     stripped.
  2. A PATH IS RESOLVED, not string-matched (store_referenced). A token whose
     basename is MEMORY.md/QUARANTINE.md but which carries a directory
     component now counts only if it really resolves into the store, so
     `memory-mesh/MEMORY.md` and `docs/about-MEMORY.md` stop being treated as
     the store. A BARE `MEMORY.md` with no directory still counts, always:
     the hook cannot know the shell's cwd, and `bash -c "echo x > MEMORY.md"`
     is the exact bypass this guard exists to stop. Unknown cwd fails closed.

Both narrowings remove text from consideration or resolve it more precisely;
neither adds a path by which a store write becomes invisible. Proved by
hooks/selftest_guard.py, whose deny set is unchanged and now includes a
heredoc redirected INTO the store and a body piped to a shell.

2026-07-31 (Craig authorized): the DISCRIMINATOR was examined and deliberately
left alone; only the deny MESSAGE changed. A perf/usability audit hit a third
false-positive class -- a `>` that is not a redirect at all (`->`, `=>`, `>=`,
`-->`) sitting in an unrelated segment of a compound command that also READS
the store, e.g. `wc -c <store>; python3 -c "print(a, '->', b)"`. The obvious
narrowings were tested against bash and BOTH open a real bypass:

  * `echo hello->probe_out` genuinely creates the file `probe_out` -- `>` is a
    shell metacharacter, so an arrow tokenizes as word `-` plus redirect. An
    arrow is NOT decorative to bash; exempting it would allow
    `echo poison->MEMORY.md`.
  * exempting quoted `>` fails for the same reason the 2026-07-28 note gives:
    `bash -c 'echo x > MEMORY.md'` is entirely inside quotes and still writes.

So the accepted-false-positive posture stands: this guard errs closed on
ambiguity, and relating the write-shape to the store would need the
redirect-TARGET parser the module docstring already rejects. What was actually
broken was the message -- it said "if it was actually a READ redirected
elsewhere, re-run without the redirect", which describes only the 2026-07-22
FP class and sent the operator hunting for a redirect that did not exist. The
message now quotes the exact token that matched WRITE_SHAPE, so the shape that
tripped it is visible instead of guessed. No change to what is denied; the
26-case matrix is unchanged and gained 5 arrow-class cases pinning the
deny (they must KEEP denying) and asserting the token is named.
"""
import json
import os
import re
import sys

def _store():
    """One derivation, fleet- and seed-wide (mesh_lib.store_dir): the harness
    keys the auto-memory store by the WORKSPACE path (memory-mesh's parent)
    with / -> -. Was hardcoded to one host's path until 2026-09-17, which on
    any other host denied every call (its selftest: 15/34 want=allow -> DENY)."""
    override = os.environ.get("MEMORY_WRITE_GUARD_STORE")
    if override:
        return os.path.realpath(override)
    workspace = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    return os.path.realpath(os.path.join(
        os.path.expanduser("~"), ".claude", "projects", workspace.replace("/", "-"), "memory"))


STORE = _store()
SANCTIONED = "memory_write.py"
# The store's two named surfaces. A bare mention of either is treated as the
# store even without a path, because the shell's cwd is unknowable here.
STORE_FILES = {"MEMORY.md", "QUARANTINE.md"}
WRITE_SHAPE = re.compile(
    r">|\btee\b|\bsed\s+-i\b|\bcp\b|\bmv\b|\brm\b|\btruncate\b|\bdd\b"
    r"|open\([^)]*['\"][wax]")
# Redirections that cannot write a file, stripped before the write-shape scan.
# Anchored so `>/dev/null/../MEMORY.md` is NOT stripped, and `>&` is dropped
# only before a digit -- bash's `cmd >& file` really does write a file.
#
# 2026-07-30: the terminator set gained `)`, `"`, `'` and a backtick. It was
# whitespace/`;`/`|`/`&`/end only, so `$(grep x MEMORY.md 2>/dev/null)` kept its
# `>` and denied -- a command substitution is the single most common place a
# /dev/null redirect ends, and this guard denied one of its own author's reads
# minutes after being narrowed. What follows the word `/dev/null` cannot change
# the fact that /dev/null is the target, so widening the terminator set removes
# no protection; the anchor that matters is that `/dev/null` is the WHOLE path,
# which is still enforced (a `/` following it does not terminate the match).
NOT_A_WRITE = re.compile(
    r"(?:\d*|&)\s*>{1,2}\s*/dev/null(?=[\s;|&)\"'`]|$)"
    r"|\d*>&\d+")
FILE_TOOLS = {"Write", "Edit", "NotebookEdit", "MultiEdit"}

# 2026-07-28: a `git commit` MESSAGE is data, not shell syntax, and this guard
# scanned it. A commit in an unrelated repo was denied because its message said
# "MEMORY.md" (mentions_store) and contained `events/<host>.ndjson` -- whose `>`
# matched WRITE_SHAPE. Neither can write anything.
#
# Strip ONLY the -m/-F argument of a git commit. NOT quoted strings in general:
# `bash -c "echo x > MEMORY.md"` must keep denying, and stripping every quoted
# string would open exactly that bypass. Handles combined short flags (-qm) and
# repeated -m. A commit cannot mutate the store: the file write it records would
# already have been caught above, at the moment it happened.
GIT_COMMIT_MSG = re.compile(
    r"""(?<!\w)-[a-zA-Z]*[mF]\s+           # -m / -F, incl. combined like -qm
        (?:"[^"]*"|'[^']*'|\S+)""", re.X)

# The opening of a heredoc: `<<EOF`, `<<-EOF`, `<<'EOF'`, `<< "EOF"`.
HEREDOC_OPEN = re.compile(r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1")

# Shell metacharacters, so a command can be split into path-ish tokens.
TOKEN_SPLIT = re.compile(r"[\s;|&<>()\[\]{}=,]+")


def strip_commit_message(cmd):
    """Remove git-commit message arguments before the write-shape scan."""
    if not re.search(r"\bgit\b[^;|&]*\bcommit\b", cmd):
        return cmd
    return GIT_COMMIT_MSG.sub(" ", cmd)


def strip_heredocs(cmd):
    """Remove heredoc BODIES -- prose, not shell syntax (2026-07-30).

    Safe because the redirect that makes a heredoc a write is on the command
    line, never in the body: `cat > MEMORY.md <<EOF` keeps both its `>` and its
    path after stripping. What the body can no longer do is make a document
    that merely DISCUSSES MEMORY.md look like a write to it.

    NOT stripped when the opening line contains a pipe: `cat <<EOF | bash`
    executes its body as shell, so there the body is code and must be scanned.
    """
    lines = cmd.splitlines()
    out, i = [], 0
    while i < len(lines):
        line = lines[i]
        out.append(line)
        m = HEREDOC_OPEN.search(line)
        i += 1
        if not m or "|" in line:
            continue
        delim = m.group(2)
        while i < len(lines) and lines[i].strip() != delim:
            i += 1                      # drop the body
        if i < len(lines):
            i += 1                      # and the terminator
    return "\n".join(out)


def in_store(path):
    if not path:
        return False
    try:
        real = os.path.realpath(os.path.expanduser(path))
    except OSError:
        return False
    return real == STORE or real.startswith(STORE + os.sep)


def store_referenced(text):
    """Does `text` refer to the auto-memory store? (2026-07-30)

    Replaces a bare `"MEMORY.md" in text` substring test. A token that names one
    of the store's files is resolved: with a directory component it counts only
    if it really lands in the store, so a project's own MEMORY.md or a doc
    called about-MEMORY.md is no longer mistaken for the operator's brain.

    A BARE filename always counts. That is not an oversight -- the guard cannot
    see the shell's cwd, so `echo x > MEMORY.md` could be the store, and the
    only safe reading of "could be" is yes.
    """
    if STORE in text:
        return True
    for raw in TOKEN_SPLIT.split(text):
        tok = raw.strip("\"'")
        if not tok:
            continue
        if os.path.basename(tok) not in STORE_FILES:
            continue
        if "/" not in tok or in_store(tok):
            return True
    return False


def deny(reason):
    print(reason, file=sys.stderr)
    sys.exit(2)


def main():
    event = json.loads(sys.stdin.read() or "{}")
    tool = event.get("tool_name", "")
    inp = event.get("tool_input", {}) or {}

    if tool in FILE_TOOLS:
        if in_store(inp.get("file_path", "")):
            deny(
                f"memory-write-guard: direct {tool} into the auto-memory "
                "store is blocked. Memory writes must go through "
                "the workspace door, memory-mesh/memory_write.py (via "
                "Bash), so "
                "the lineage gate (craig-direct vs contains-untrusted, "
                "Story 029) is honestly set on every write -- this is the "
                "boundary MemGhost-style email/web-borne memory poisoning "
                "relies on not existing. Call memory_write.py instead."
            )
        return

    if tool == "Bash":
        cmd = inp.get("command", "") or ""
        # Heredoc bodies and git commit messages are DATA. Strip both before
        # either test -- otherwise text that merely names the store still reads
        # as a reference to it.
        scannable = strip_heredocs(strip_commit_message(cmd))
        mentions_store = store_referenced(scannable)
        # /dev/null targets and fd dups cannot write; ignore them.
        scannable = NOT_A_WRITE.sub(" ", scannable)
        hit = WRITE_SHAPE.search(scannable)
        if mentions_store and SANCTIONED not in cmd and hit:
            # Quote the token that matched. The guard cannot tell a redirect
            # from an arrow in a string (see the 2026-07-31 note: to bash they
            # are the same token), so it denies either way -- but naming the
            # match tells the operator WHICH shape to change instead of
            # sending them looking for a redirect that may not exist.
            token = hit.group(0).strip() or hit.group(0)
            deny(
                "memory-write-guard: this command names the auto-memory store "
                f"AND contains a write-shaped token ({token!r}), so it is "
                "denied without going through memory_write.py (the lineage "
                "gate).\n"
                f"  * If {token!r} is not a redirect at all -- an arrow like "
                "'->' or '=>' inside a string, in an unrelated part of a "
                "compound command -- split that part into its own call. bash "
                "cannot tell those apart from a redirect either, so this "
                "guard will not.\n"
                "  * If it is a READ redirected elsewhere, re-run without the "
                "redirect/pipe-to-file.\n"
                "  * If it is a legitimate memory write, use "
                "the workspace door, memory-mesh/memory_write.py --commit."
            )
        return


if __name__ == "__main__":
    try:
        main()
    except SystemExit:
        raise
    except Exception:
        # Fail-open: a broken guard must never block work it can't parse.
        sys.exit(0)
