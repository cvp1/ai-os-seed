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
import shlex
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
# The one sanctioned write path. 2026-09-19: this used to be a bare substring
# test (`SANCTIONED not in cmd`) against the RAW command, so ANY occurrence of
# the string anywhere -- in a trailing `# memory_write.py` comment, in a
# filename, in an echoed word -- exempted the WHOLE command. Verified bypass:
# `printf x > STORE/f.md # memory_write.py` was allowed, rc=0, file created.
# Sanctioning is now structural and PER SEGMENT (command_is_sanctioned).
# 2026-09-19 (round 2, grok-4.6): a basename is not a credential either.
# `python3 /tmp/not-the-door/memory_write.py … > STORE/f.md` was ALLOWED, because
# the sanction test was `basename(prog) == "memory_write.py"`. The door is a
# FILE, not a name: this install's own memory_write.py, one directory up from
# the hook, compared by realpath. WORKSPACE is the base a workspace-relative
# invocation (`python3 memory-mesh/memory_write.py …`) is resolved against.
SANCTIONED_BASENAME = "memory_write.py"
DOOR = os.path.realpath(os.path.join(
    os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
    SANCTIONED_BASENAME))
WORKSPACE = os.path.dirname(os.path.dirname(DOOR))
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
        # ANY token that RESOLVES into the store counts, whatever it is named.
        # The substring test above only sees the store spelled literally, so
        # the same path written through a symlink or a `..` segment slipped
        # past it: `printf x > $TMP/a/../store/f.md  # memory_write.py` was
        # ALLOWED. On macOS EVERY temp path is such a spelling
        # (/var/folders -> /private/var/folders), which is why M1-bash-guard's
        # comment bypass passed on Linux and failed on macos-latest
        # (CI 2026-09-19). realpath is the instrument in_store already uses;
        # this only widens what the guard calls the store, never narrows it.
        if "/" in tok and in_store(tok):
            return True
        if os.path.basename(tok) not in STORE_FILES:
            continue
        if "/" not in tok or in_store(tok):
            return True
    return False


# --- structural sanctioning (2026-09-19) -------------------------------------
# A command is sanctioned only where a real invocation of memory_write.py is
# the PROGRAM of the simple command that carries the write shape. The old
# substring test made the word itself the credential; anyone who could get the
# eleven characters into the command -- a comment, a quoted string, a path --
# could write the store with no door and no lineage. Being conservative is the
# whole point here: any shape this parser cannot read DENIES.

# A shell comment cannot execute, so `#` to end-of-line is removed before the
# sanction parse ONLY (mentions_store / the write-shape scan still see the raw
# scannable text, so this can never make a denied command allowed).
PY_INTERP = re.compile(r"^(?:python|python[0-9]+(?:\.[0-9]+)*|pypy[0-9]*)$")
ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Operators that end one simple command and start the next.
SEGMENT_OPS = ("&&", "||", ";;", ";", "|", "&", "\n", "(", ")")

# --- opaque inline code (2026-09-19, round 2, grok-4.6) ----------------------
# WRITE_SHAPE reads SHELL. A language runtime handed inline code writes files
# with no shell redirect at all, so all three of these named the store and were
# ALLOWED:
#   python3 -c "pathlib.Path('STORE/x.md').write_text('…')"
#   node -e "require('fs').writeFileSync('STORE/x.md','…')"
#   ruby -e "File.write('STORE/x.md','…')"
# The guard cannot interpret an arbitrary program in an arbitrary language, and
# a partial interpreter is worse than none. So it stops guessing: a segment that
# NAMES THE STORE and runs inline code in a runtime is denied unless it is the
# door. This is a fail-closed narrowing -- `python3 -c "print(1)"`, which does
# not name the store, is untouched. The accepted false positive is a READ of the
# store written as inline code (`python3 -c "print(open('…/MEMORY.md').read())"`);
# it is the same trade the module docstring already makes for `grep … > /tmp/out`,
# and the deny message says how to re-run.
RUNTIMES = {
    "node", "nodejs", "deno", "bun", "ruby", "irb", "perl", "php", "lua",
    "luajit", "tclsh", "wish", "Rscript", "osascript", "bash", "sh", "zsh",
    "ksh", "dash", "fish", "elixir", "erl", "groovy", "scala", "julia",
}
INLINE_FLAGS = {"-c", "-e", "-E", "-r", "-p", "-P", "-n", "--eval", "--exec",
                "--execute", "--command", "--expression"}
INLINE_FLAG_PREFIXES = ("--eval=", "--exec=", "--execute=", "--command=",
                        "--expression=")


def _is_runtime(prog):
    base = os.path.basename(prog)
    return base in RUNTIMES or bool(PY_INTERP.match(base))


def inline_code_flag(seg):
    """The inline-code flag a language runtime in this segment was handed, or
    None. Raises ValueError when shlex cannot parse the segment."""
    argv = shlex.split(seg)
    k = 0
    while k < len(argv) and (ENV_ASSIGN.match(argv[k]) or argv[k] == "env"):
        k += 1
    if k >= len(argv) or not _is_runtime(argv[k]):
        return None
    runtime = os.path.basename(argv[k])
    for tok in argv[k + 1:]:
        if tok in INLINE_FLAGS:
            return f"{runtime} {tok}"
        if tok.startswith(INLINE_FLAG_PREFIXES):
            return f"{runtime} {tok.split('=', 1)[0]}"
    return None


def opaque_inline_write(scannable):
    """(why, None) for a segment that names the store and runs inline code.

    Returns None when nothing in the command matches. Sanctioned segments (the
    real door) are exempt, but the door is never invoked as `-c`/`-e` anyway --
    segment_program already refuses to look past an inline-code flag."""
    try:
        segments = split_segments(strip_shell_comments(scannable))
    except ValueError:
        return None                  # the parse failure is handled elsewhere
    bases = _cd_targets(segments) + [os.getcwd(), WORKSPACE]
    for seg in segments:
        if not store_referenced(seg):
            continue
        try:
            flag = inline_code_flag(seg)
            if flag and not segment_is_sanctioned(seg, bases):
                return flag
        except ValueError:
            continue
    return None


def heredoc_into_runtime(cmd):
    """A heredoc fed to a language runtime whose BODY names the store.

    strip_heredocs() drops bodies as data, which is right for `cat > doc.md
    <<EOF`. It is wrong for `python3 <<EOF`, where the body IS the program --
    the store reference would be stripped before it was ever looked for."""
    lines = cmd.splitlines()
    i = 0
    while i < len(lines):
        line = lines[i]
        m = HEREDOC_OPEN.search(line)
        i += 1
        if not m:
            continue
        head = line[:m.start()]
        body = []
        delim = m.group(2)
        while i < len(lines) and lines[i].strip() != delim:
            body.append(lines[i])
            i += 1
        if i < len(lines):
            i += 1
        try:
            argv = shlex.split(head)
        except ValueError:
            argv = head.split()
        k = 0
        while k < len(argv) and (ENV_ASSIGN.match(argv[k]) or argv[k] == "env"):
            k += 1
        if k < len(argv) and _is_runtime(argv[k]) and \
                store_referenced("\n".join(body)):
            return f"{os.path.basename(argv[k])} <<"
    return None


def strip_shell_comments(text):
    """Drop `# ...` to end of line, outside quotes, line by line. bash starts a
    comment only at the beginning of a word, so `foo#bar` is NOT a comment.

    A quote left open at end-of-line means the line-by-line reading is wrong
    (the next line is inside that string), so nothing is stripped at all --
    stripping is an optimisation for the sanction parse, never a safety claim.
    """
    try:
        return "\n".join(_strip_line_comment(ln) for ln in text.split("\n"))
    except ValueError:
        return text


def _strip_line_comment(line):
    quote, esc, prev = None, False, ""
    for i, ch in enumerate(line):
        if esc:
            esc = False
            prev = ch
            continue
        if quote:
            if ch == "\\" and quote == '"':
                esc = True
            elif ch == quote:
                quote = None
            prev = ch
            continue
        if ch == "\\":
            esc = True
            prev = ch
            continue
        if ch in "'\"":
            quote = ch
            prev = ch
            continue
        if ch == "#" and (prev == "" or prev.isspace() or prev in ";|&()"):
            return line[:i]
        prev = ch
    if quote is not None:
        raise ValueError("unbalanced quote")
    return line


def split_segments(text):
    """Quote-aware split into simple-command segments on ; && || | & ( ) and
    newline. Raises ValueError on an unbalanced quote -- which DENIES."""
    segs, buf, quote, esc = [], [], None, False
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if esc:
            buf.append(ch)
            esc = False
            i += 1
            continue
        if quote:
            if ch == "\\" and quote == '"':
                esc = True
            elif ch == quote:
                quote = None
            buf.append(ch)
            i += 1
            continue
        if ch == "\\":
            esc = True
            buf.append(ch)
            i += 1
            continue
        if ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
            continue
        matched = None
        for op in SEGMENT_OPS:
            if text.startswith(op, i):
                matched = op
                break
        if matched:
            segs.append("".join(buf))
            buf = []
            i += len(matched)
            continue
        buf.append(ch)
        i += 1
    if quote is not None:
        raise ValueError("unbalanced quote")
    segs.append("".join(buf))
    return [s for s in segs if s.strip()]


def segment_program(seg):
    """The program a simple command would exec, or None if unreadable.

    Leading `VAR=val` assignments and `env` are skipped; when the program is a
    python interpreter the first non-flag argument is the real program, so
    `python3 /abs/memory-mesh/memory_write.py write ...` resolves to
    memory_write.py. Raises ValueError when shlex cannot parse the segment."""
    argv = shlex.split(seg)          # ValueError on a bad quote -> deny
    k = 0
    while k < len(argv) and (ENV_ASSIGN.match(argv[k]) or argv[k] == "env"):
        k += 1
    if k >= len(argv):
        return None
    prog = argv[k]
    if PY_INTERP.match(os.path.basename(prog)):
        k += 1
        while k < len(argv) and argv[k].startswith("-"):
            if argv[k] in ("-c", "-m"):
                return argv[k]       # code/module, never the writer
            k += 1
        if k >= len(argv):
            return None
        prog = argv[k]
    # A token that still carries whitespace came out of a single quoted word
    # (`"python3 memory_write.py"`); it is data, not a program path.
    if not prog or any(c.isspace() for c in prog):
        return None
    return prog


def _cd_targets(segments):
    """Directories an earlier `cd`/`pushd` in the same command would move to.

    A relative program token in a later segment is resolved against these too,
    so `cd <workspace> && python3 memory-mesh/memory_write.py ...` still names
    the real door. This can only ever make the DOOR reachable -- the comparison
    below is realpath-equality with one exact file, so a wrong base cannot
    sanction anything."""
    out = []
    for seg in segments:
        try:
            argv = shlex.split(seg)
        except ValueError:
            continue
        k = 0
        while k < len(argv) and ENV_ASSIGN.match(argv[k]):
            k += 1
        if k + 1 < len(argv) and argv[k] in ("cd", "pushd"):
            target = argv[k + 1]
            if not target.startswith("-"):
                out.append(os.path.expanduser(target))
    return out


def resolves_to_door(prog, bases):
    """Does this program token name THIS install's own memory_write.py?

    2026-09-19 (round 2): the check used to be `basename(prog) ==
    "memory_write.py"`, which sanctioned any file anywhere with that name --
    `python3 /tmp/x/memory_write.py --text ... > STORE/f.md` was ALLOWED and
    wrote the store with no door and no lineage. Sanction is now identity, not
    a name: realpath of the token must equal DOOR. A token that resolves to
    nothing resolves to nothing -- it simply is not the door, so it denies."""
    if not prog:
        return False
    prog = os.path.expanduser(prog)
    candidates = [prog] if os.path.isabs(prog) else [
        os.path.join(b, prog) for b in bases]
    for cand in candidates:
        try:
            if os.path.realpath(cand) == DOOR:
                return True
        except OSError:
            continue
    return False


def segment_is_sanctioned(seg, bases):
    prog = segment_program(seg)      # may raise ValueError
    return resolves_to_door(prog, bases)


def command_is_sanctioned(scannable):
    """(sanctioned, why_not). Sanctioned only when EVERY segment that carries a
    write-shaped token is itself an invocation of THIS install's memory_write.py
    (resolved by realpath, never by basename). A sanctioned segment never
    sanctions a sibling: `python3 memory_write.py x && printf y > STORE/f.md`
    is denied on the second segment."""
    try:
        text = strip_shell_comments(scannable)
        segments = split_segments(text)
    except ValueError as exc:
        return False, f"the guard could not parse this command ({exc})"
    bases = _cd_targets(segments) + [os.getcwd(), WORKSPACE]
    writing = []
    for seg in segments:
        if WRITE_SHAPE.search(NOT_A_WRITE.sub(" ", seg)):
            writing.append(seg)
    if not writing:
        # The write shape is not in any executable segment (a comment, say).
        # Preserve the historical verdict rather than trust this parser to be
        # the only thing standing between a poisoned page and the store.
        return False, "no executable segment carries the write; denying anyway"
    for seg in writing:
        try:
            if not segment_is_sanctioned(seg, bases):
                return False, (
                    "the command that writes is not this install's "
                    f"memory_write.py ({DOOR}): {seg.strip()[:120]!r}")
        except ValueError as exc:
            return False, f"the guard could not parse {seg.strip()[:80]!r} ({exc})"
    return True, ""


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
        # Inline code in a language runtime is opaque to the shell-shaped
        # write scan below, so it is decided first and on its own terms.
        opaque = None
        if mentions_store:
            opaque = opaque_inline_write(scannable)
        if opaque is None:
            opaque = heredoc_into_runtime(strip_commit_message(cmd))
        if opaque:
            deny(
                "memory-write-guard: this command names the auto-memory store "
                f"and hands INLINE CODE to a language runtime ({opaque!r}). "
                "Inline code is opaque to this guard -- it cannot tell a read "
                "from a write in an arbitrary language -- so it is denied "
                "without going through the lineage gate.\n"
                "  * If it is a memory WRITE, use the workspace door, "
                "memory-mesh/memory_write.py --commit.\n"
                "  * If it is only a READ, re-run it as a plain command "
                "(cat/grep/python3 <script.py>) that does not carry the "
                "program on the command line."
            )
        # /dev/null targets and fd dups cannot write; ignore them.
        scannable = NOT_A_WRITE.sub(" ", scannable)
        hit = WRITE_SHAPE.search(scannable)
        sanctioned, why_not = (True, "")
        if mentions_store and hit:
            sanctioned, why_not = command_is_sanctioned(scannable)
        if mentions_store and not sanctioned and hit:
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
                "the workspace door, memory-mesh/memory_write.py --commit.\n"
                f"  * Not sanctioned because: {why_not}"
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
