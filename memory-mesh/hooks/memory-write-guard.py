#!/usr/bin/env python3
"""PreToolUse guard: block direct mutation of the auto-memory store.

Memory writes must go through memory_write.py so the `lineage:` field is set
honestly; this hook denies Write/Edit into the store and Bash commands that
name the store and carry a write shape.

Contract (Claude Code PreToolUse hook): JSON event on stdin
(tool_name, tool_input). To block: print the reason to STDERR and exit 2.
To allow: exit 0 with no output. Fails open on any internal error.

Errs closed on ambiguity: a read of the store redirected to a file, or a
non-redirect `>` (`->`, `=>`) in a command that also names the store, is
denied. Redirect-target analysis is deliberately not attempted. Heredoc
bodies, git commit messages, /dev/null targets and fd duplications are
stripped before scanning because they cannot write.
"""
import json
import os
import re
import shlex
import sys

def _store():
    """The auto-memory store path, keyed by the workspace path with / -> -
    (same derivation as mesh_lib.store_dir)."""
    override = os.environ.get("MEMORY_WRITE_GUARD_STORE")
    if override:
        return os.path.realpath(override)
    workspace = os.path.dirname(os.path.dirname(os.path.dirname(os.path.realpath(__file__))))
    return os.path.realpath(os.path.join(
        os.path.expanduser("~"), ".claude", "projects", workspace.replace("/", "-"), "memory"))


STORE = _store()
# The one sanctioned write path: this install's memory_write.py, compared by
# realpath (a basename match is not enough). WORKSPACE is the base for a
# workspace-relative invocation (`python3 memory-mesh/memory_write.py …`).
SANCTIONED_BASENAME = "memory_write.py"
DOOR = os.path.realpath(os.path.join(
    os.path.dirname(os.path.dirname(os.path.realpath(__file__))),
    SANCTIONED_BASENAME))
WORKSPACE = os.path.dirname(os.path.dirname(DOOR))
# The store's two named surfaces. A bare mention of either is treated as the
# store even without a path, because the shell's cwd is unknowable here.
STORE_FILES = {"MEMORY.md", "QUARANTINE.md"}
# Shell constructs that can create, modify or remove a file (ln included: a
# planted symlink lets a later write go through it).
WRITE_SHAPE = re.compile(
    r">|\btee\b|\bsed\s+-i\b|\bcp\b|\bmv\b|\brm\b|\btruncate\b|\bdd\b"
    r"|\bscp\b|\brsync\b|\binstall\b|\bln\b|\bunlink\b|\bshred\b"
    r"|open\([^)]*['\"][wax]")
# Redirections that cannot write a file, stripped before the write-shape scan.
# Anchored so `>/dev/null/../MEMORY.md` is NOT stripped, and `>&` is dropped
# only before a digit -- bash's `cmd >& file` really does write a file.
# `/dev/null` must be the whole path; quotes, `)` and backticks may end it.
NOT_A_WRITE = re.compile(
    r"(?:\d*|&)\s*>{1,2}\s*/dev/null(?=[\s;|&)\"'`]|$)"
    r"|\d*>&\d+")
FILE_TOOLS = {"Write", "Edit", "NotebookEdit", "MultiEdit"}

# The -m/-F argument of a git commit: data, not shell. Only this is stripped,
# not quoted strings in general (`bash -c "echo x > MEMORY.md"` must deny).
GIT_COMMIT_MSG = re.compile(
    r"""(?<!\w)-[a-zA-Z]*[mF]\s+           # -m / -F, incl. combined like -qm
        (?:"[^"]*"|'[^']*'|\S+)""", re.X)

# The opening of a heredoc: `<<EOF`, `<<-EOF`, `<<'EOF'`, `<< "EOF"`. The
# delimiter must end there; shapes like `<<E"OF"` are not matched (not stripped).
HEREDOC_OPEN = re.compile(
    r"<<-?\s*(['\"]?)([A-Za-z_][A-Za-z0-9_]*)\1(?![\w'\"\\])")
# What an UNQUOTED heredoc body executes: command substitution.
HEREDOC_SUBST = re.compile(r"\$\(|`")

# Shell metacharacters, so a command can be split into path-ish tokens.
TOKEN_SPLIT = re.compile(r"[\s;|&<>()\[\]{}=,]+")
# Just the shell redirect, for the sanctioned-segment check below.
REDIRECT_SHAPE = re.compile(r">")


def strip_commit_message(cmd):
    """Remove git-commit message arguments before the write-shape scan."""
    if not re.search(r"\bgit\b[^;|&]*\bcommit\b", cmd):
        return cmd
    return GIT_COMMIT_MSG.sub(" ", cmd)


def strip_heredocs(cmd):
    """Remove heredoc bodies, which are data; the write redirect stays on the
    command line.

    Not stripped when the opening line contains a pipe (`cat <<EOF | bash`
    executes the body), or when the delimiter is unquoted and the body has a
    command substitution (bash expands it).
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
        body = []
        while i < len(lines) and lines[i].strip() != delim:
            body.append(lines[i])
            i += 1
        if not m.group(1) and HEREDOC_SUBST.search("\n".join(body)):
            out.extend(body)            # expanded by bash: code, keep it
            continue                    # (the terminator line is kept too)
        if i < len(lines):
            i += 1                      # drop the body and the terminator
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
    """Does `text` refer to the auto-memory store?

    A path token counts if it resolves into the store. A bare MEMORY.md or
    QUARANTINE.md always counts, since the shell's cwd is unknown.
    """
    if STORE in text:
        return True
    for raw in TOKEN_SPLIT.split(text):
        tok = raw.strip("\"'")
        if not tok:
            continue
        # Any token that resolves into the store counts (catches symlink and
        # `..` spellings the literal substring test misses).
        if "/" in tok and in_store(tok):
            return True
        if os.path.basename(tok) not in STORE_FILES:
            continue
        if "/" not in tok or in_store(tok):
            return True
    return False


# --- structural sanctioning --------------------------------------------------
# A command is sanctioned only where a real invocation of memory_write.py is
# the program of the simple command that carries the write shape. Any shape
# this parser cannot read denies. Shell comments are removed before the
# sanction parse only.
PY_INTERP = re.compile(r"^(?:python|python[0-9]+(?:\.[0-9]+)*|pypy[0-9]*)$")
ENV_ASSIGN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
# Operators that end one simple command and start the next.
SEGMENT_OPS = ("&&", "||", ";;", ";", "|", "&", "\n", "(", ")")

# --- opaque inline code ------------------------------------------------------
# Inline code in a language runtime (`python3 -c`, `node -e`, ...) can write
# without a shell redirect, so a segment that names the store and runs inline
# code is denied unless it is the door. Inline reads of the store are an
# accepted false positive.
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
    """The inline-code flag of a non-door segment that names the store, or None."""
    try:
        segments = split_segments(strip_shell_comments(scannable))
    except ValueError:
        return None                  # the parse failure is handled elsewhere
    bases_by_seg = _bases_per_segment(segments)
    for seg, base in zip(segments, bases_by_seg):
        if not store_referenced(seg):
            continue
        try:
            flag = inline_code_flag(seg)
            if flag and not segment_is_sanctioned(seg, [base, WORKSPACE]):
                return flag
        except ValueError:
            continue
    return None


def heredoc_into_runtime(cmd):
    """A heredoc fed to a language runtime whose body names the store (the body
    is the program there, so strip_heredocs must not hide it)."""
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
        text = "\n".join(body)
        if k < len(argv) and _is_runtime(argv[k]) and (
                store_referenced(text)
                or (names_store_parent(text) and has_inline_write(text))):
            return f"{os.path.basename(argv[k])} <<"
    return None


# --- the store spelled in pieces ---------------------------------------------
# Inline code can assemble the store path from parts. A runtime segment that
# names the store's parent (project dir, project key, or projects dir) and
# carries a write-shaped call is denied. A speed bump only: encoded paths
# still pass a text scan.
INLINE_WRITE = re.compile(
    r"write|append|symlink|\blink|rename|replace|copy|move|unlink|remove"
    r"|rmtree|truncate|touch|mkdir|os\.open|fdopen")


def _store_parent_markers():
    parent = os.path.dirname(STORE)
    out = {parent, os.path.dirname(parent)}
    key = os.path.basename(parent)
    if len(key) >= 8:                   # the project key, e.g. -home-u-Github-CC
        out.add(key)
    return {m for m in out if m and m != os.sep}


def names_store_parent(text):
    return any(m in text for m in _store_parent_markers())


def has_inline_write(text):
    return bool(WRITE_SHAPE.search(NOT_A_WRITE.sub(" ", text))
                or INLINE_WRITE.search(text))


def split_path_inline_write(scannable):
    """The inline-code flag of a runtime segment that names the store's parent
    and carries a write shape, or None. Sanctioned (door) segments are exempt."""
    try:
        segments = split_segments(strip_shell_comments(scannable))
    except ValueError:
        return None
    for seg, base in zip(segments, _bases_per_segment(segments)):
        if not names_store_parent(seg) or not has_inline_write(seg):
            continue
        try:
            flag = inline_code_flag(seg)
            if flag and not segment_is_sanctioned(seg, [base, WORKSPACE]):
                return flag
        except ValueError:
            continue
    return None


def strip_shell_comments(text):
    """Drop `# ...` to end of line outside quotes (only at a word start).

    If any line leaves a quote open, nothing is stripped.
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


def _bases_per_segment(segments):
    """The cwd each segment runs in, tracking `cd` in command order.

    One entry per segment; the last cd in a segment applies to later ones.
    """
    here = os.getcwd()
    out = []
    for seg in segments:
        out.append(here)
        for tgt in _cd_targets([seg]):
            here = tgt                    # last cd in this segment wins
    return out


def _resolution_bases(segments):
    """Deprecated; raises so any remaining caller fails loudly."""
    raise RuntimeError("_resolution_bases is superseded by _bases_per_segment "
                       "(command order matters; see SEED-081, 2026-09-19)")


def _unused_resolution_bases_doc(segments):
    """Unused: bases a relative program token may be resolved against."""
    cd = _cd_targets(segments)
    return cd + ([] if cd else [os.getcwd()]) + [WORKSPACE]


def _cd_targets(segments):
    """Targets of `cd`/`pushd` in these segments, in order."""
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
    """Does this program token resolve (by realpath) to DOOR?"""
    if not prog:
        return False
    prog = os.path.expanduser(prog)
    if os.path.isabs(prog):
        candidates = [prog]
    else:
        # A bare name resolves against the current directory only, never
        # WORKSPACE.
        bases = list(bases) if "/" in prog else [b for b in bases
                                                 if b != WORKSPACE]
        candidates = [os.path.join(b, prog) for b in bases]
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
    """(sanctioned, why_not). Sanctioned only when every write-shaped segment
    is itself an invocation of DOOR, with no redirect."""
    try:
        text = strip_shell_comments(scannable)
        segments = split_segments(text)
    except ValueError as exc:
        return False, f"the guard could not parse this command ({exc})"
    bases_by_seg = _bases_per_segment(segments)
    writing = []
    for seg, base in zip(segments, bases_by_seg):
        if WRITE_SHAPE.search(NOT_A_WRITE.sub(" ", seg)):
            writing.append((seg, [base, WORKSPACE]))
    if not writing:
        # The write shape is only in a non-executable part (e.g. a comment).
        return False, "no executable segment carries the write; denying anyway"
    for seg, bases in writing:
        try:
            if not segment_is_sanctioned(seg, bases):
                return False, (
                    "the command that writes is not this install's "
                    f"memory_write.py ({DOOR}): {seg.strip()[:120]!r}")
            # A redirect is opened by the shell, outside the door's lineage
            # gate, so a door segment with a redirect is refused.
            if REDIRECT_SHAPE.search(NOT_A_WRITE.sub(" ", seg)):
                return False, (
                    "this segment redirects with '>' while naming the store. "
                    "memory_write.py writes the store itself; a shell redirect "
                    "goes around its lineage gate even when the program IS "
                    "this install's door. Run the door without a redirect.")
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
        # Heredoc bodies and git commit messages are data; strip both first.
        scannable = strip_heredocs(strip_commit_message(cmd))
        mentions_store = store_referenced(scannable)
        # Inline runtime code is opaque to the shell write scan; check it first.
        opaque = None
        if mentions_store:
            opaque = opaque_inline_write(scannable)
        if opaque is None:
            opaque = split_path_inline_write(scannable)
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
            # Name the matched token so the caller knows which shape to change.
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
        # Fail open on internal errors.
        sys.exit(0)
