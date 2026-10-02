#!/usr/bin/env python3
"""import_pack — read-only inspection and integrity verification of a cc-pack.

    import_pack.py --pack <path> --inspect
    import_pack.py --pack <path> --verify [--target <ROOT>]

Stdlib-only and standalone; mirrors cc-pack/pack_lib.py's verify logic (keep
the two in sync). <path> is a pack directory or a .tar; a tar is read from its
member table, never extracted. Both verbs are read-only; the write path is
`install.py --approve import-pack --from-pack <dir>`, which runs --verify first.

--verify's exit code reflects integrity only. With --allowed-signers it also
prints "signature: <state>" (unsigned/invalid/unknown-signer/
verification-error/verified) for the caller to enforce. --allowed-signers is
never guessed.
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

PACK_SCHEMA = 1
AUDIENCES = ("replica", "shareable")
KINDS = ("system", "memory", "vault")
MANIFEST_NAME = "pack.json"
SIG_NAME = "pack.json.sig"
SUMS_NAME = "SHA256SUMS"
_RESERVED_TOP = {MANIFEST_NAME, SIG_NAME, SUMS_NAME}

# Size bounds; must match pack_lib.py.
MAX_MANIFEST_BYTES = 4 * 1024 * 1024
MAX_SUMS_BYTES = 16 * 1024 * 1024
MAX_MEMBER_BYTES = 256 * 1024 * 1024

# Part type -> allowed audiences. Static copy of cc-pack/parts/*.py AUDIENCES;
# update by hand when a handler is added or changed.
PART_AUDIENCES = {
    "briefs": frozenset({"replica"}),
    "doctrine": frozenset({"replica", "shareable"}),
    "skills": frozenset({"replica", "shareable"}),
    "memory-digest": frozenset({"replica", "shareable"}),
    "mesh-events": frozenset({"replica"}),
    "secret-handles": frozenset({"replica", "shareable"}),
    "secrets-encrypted": frozenset({"replica"}),
    "vault": frozenset({"replica"}),
    "usage-ledgers": frozenset({"replica"}),
}
PART_SCHEMAS = {
    "briefs": 1,
    "doctrine": 1,
    "skills": 1,
    "memory-digest": 1,
    "mesh-events": 1,
    "secret-handles": 1,
    "secrets-encrypted": 1,
    "vault": 1,
    "usage-ledgers": 1,
}

# \A/\Z, not ^/$: `$` would accept a trailing newline.
_SAFE_COMPONENT_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def is_safe_relpath(rel):
    """True if rel is a safe relative posix path. Check before any path join:
    joining an absolute path discards the left side."""
    if not rel or not isinstance(rel, str):
        return False
    if "\x00" in rel or "\\" in rel:
        return False
    if any(ord(c) < 0x20 for c in rel):
        return False
    if rel.startswith("/"):
        return False
    return all(p not in ("", ".", "..") for p in rel.split("/"))


def is_safe_component(name):
    return isinstance(name, str) and bool(_SAFE_COMPONENT_RE.match(name))


def _safe_display(s, maxlen=200):
    """Make an untrusted string terminal-safe: length-capped, repr() if it has control chars."""
    s = str(s)
    if len(s) > maxlen:
        s = s[:maxlen] + "…[truncated]"
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in s):
        return repr(s)
    return s


class PackSource:
    """Uniform read access over a directory pack or a tar pack, without ever
    extracting a tar member to disk for inspection purposes."""

    def __init__(self, path):
        self.path = Path(path)
        self.is_tar = self.path.is_file()
        self._tf = tarfile.open(self.path, "r") if self.is_tar else None
        if self.is_tar:
            names = self._tf.getnames()
            if len(names) != len(set(names)):
                self.close()
                raise SystemExit(f"{path}: duplicate member names in the tar — refusing")
            # Every real member lives under a single top-level pack-id dir
            # (build_pack.py's tarfile.add(final_dir, arcname=pack_id)).
            roots = {n.split("/", 1)[0] for n in names if n}
            if len(roots) != 1:
                self.close()
                raise SystemExit(
                    f"{path}: expected exactly one top-level dir in the tar, "
                    f"found {sorted(roots)}")
            self.root = roots.pop()
            for m in self._tf.getmembers():
                if not (m.isfile() or m.isdir()):
                    self.close()
                    raise SystemExit(
                        f"{path}: member {m.name!r} is not a regular file or "
                        f"directory (type={m.type!r}) — refusing symlinks/"
                        f"hardlinks/devices/fifos in an untrusted pack")
        else:
            self.root = None
            if not (self.path / MANIFEST_NAME).exists():
                raise SystemExit(f"{path}: no {MANIFEST_NAME} — not a pack directory")

    def close(self):
        if self._tf is not None:
            self._tf.close()

    def _member(self, rel):
        return f"{self.root}/{rel}" if self.is_tar else rel

    def exists(self, rel):
        if not is_safe_relpath(rel):
            return False
        if self.is_tar:
            try:
                self._tf.getmember(self._member(rel))
                return True
            except KeyError:
                return False
        return (self.path / rel).exists()

    def read_bytes(self, rel, max_bytes=MAX_MANIFEST_BYTES):
        if not is_safe_relpath(rel):
            raise SystemExit(f"{rel}: unsafe path, refusing to read")
        if self.is_tar:
            m = self._tf.getmember(self._member(rel))
            if m.size > max_bytes:
                raise SystemExit(f"{rel}: {m.size} bytes exceeds the {max_bytes}-byte cap")
            f = self._tf.extractfile(m)
            if f is None:
                raise SystemExit(f"{rel}: not a regular file in the tar")
            return f.read()
        p = self.path / rel
        size = p.stat().st_size
        if size > max_bytes:
            raise SystemExit(f"{rel}: {size} bytes exceeds the {max_bytes}-byte cap")
        return p.read_bytes()

    def sha256(self, rel, chunk=1 << 20, max_bytes=MAX_MEMBER_BYTES):
        if not is_safe_relpath(rel):
            return None
        h = hashlib.sha256()
        if self.is_tar:
            try:
                m = self._tf.getmember(self._member(rel))
            except KeyError:
                return None
            if not m.isfile():
                return None
            if m.size > max_bytes:
                raise SystemExit(f"{rel}: {m.size} bytes exceeds the {max_bytes}-byte member cap")
            f = self._tf.extractfile(m)
            if f is None:
                return None
            while True:
                b = f.read(chunk)
                if not b:
                    break
                h.update(b)
            return h.hexdigest()
        p = self.path / rel
        if not p.exists() or not p.is_file():
            return None
        size = p.stat().st_size
        if size > max_bytes:
            raise SystemExit(f"{rel}: {size} bytes exceeds the {max_bytes}-byte member cap")
        with open(p, "rb") as fh:
            while True:
                b = fh.read(chunk)
                if not b:
                    break
                h.update(b)
        return h.hexdigest()

    def total_size(self):
        if self.is_tar:
            return sum(m.size for m in self._tf.getmembers() if m.isfile())
        return sum(p.stat().st_size for p in self.path.rglob("*") if p.is_file())

    def list_files(self):
        """Every physical file as a pack-relative posix path, excluding reserved top-level names."""
        if self.is_tar:
            out = set()
            prefix = self.root + "/"
            for m in self._tf.getmembers():
                if not m.isfile():
                    continue
                if not m.name.startswith(prefix):
                    continue
                rel = m.name[len(prefix):]
                if rel in _RESERVED_TOP:
                    continue
                out.add(rel)
            return out
        out = set()
        for p in self.path.rglob("*"):
            if not p.is_file():
                continue
            rel = p.relative_to(self.path).as_posix()
            if rel in _RESERVED_TOP:
                continue
            out.add(rel)
        return out


def read_manifest(src):
    try:
        raw = src.read_bytes(MANIFEST_NAME)
    except SystemExit:
        raise
    try:
        manifest = json.loads(raw)
    except json.JSONDecodeError as e:
        raise SystemExit(f"{MANIFEST_NAME} did not parse: {e}")
    if not isinstance(manifest, dict):
        raise SystemExit(f"{MANIFEST_NAME} top level is {type(manifest).__name__}, expected an object")
    return manifest


def _expected_member_set(manifest, problems):
    """Pack-relative paths the manifest's parts declare; appends problems found."""
    expected = set()
    parts = manifest.get("parts")
    if not isinstance(parts, list):
        problems.append(f"manifest 'parts' is {type(parts).__name__}, expected a list")
        return expected
    seen_ids = set()
    for i, part in enumerate(parts):
        if not isinstance(part, dict):
            problems.append(f"parts[{i}] is {type(part).__name__}, expected an object")
            continue
        pid, files = part.get("id"), part.get("files")
        if not is_safe_component(pid):
            problems.append(f"parts[{i}].id {pid!r} is not a safe path component")
            continue
        if pid in seen_ids:
            problems.append(f"duplicate part id {pid!r}")
        seen_ids.add(pid)
        if not isinstance(files, list):
            problems.append(f"parts[{i}] ({pid}).files is {type(files).__name__}, expected a list")
            continue
        for f in files:
            if not is_safe_relpath(f):
                problems.append(f"parts[{i}] ({pid}) has an unsafe file entry {f!r}")
                continue
            expected.add(f"parts/{pid}/{f}")
    return expected


def verify_pack(src):
    """Integrity-check a pack (mirrors pack_lib.verify_pack). Returns (ok, problems)."""
    problems = []
    if not src.exists(MANIFEST_NAME):
        return False, [f"no {MANIFEST_NAME}"]
    try:
        manifest = read_manifest(src)
    except SystemExit as e:
        return False, [str(e)]

    if manifest.get("pack_schema") != PACK_SCHEMA:
        problems.append(
            f"pack_schema {manifest.get('pack_schema')!r} — this importer supports {PACK_SCHEMA}")
    audience = manifest.get("audience")
    if audience not in AUDIENCES:
        problems.append(f"unknown audience {audience!r}")
    if manifest.get("kind") not in KINDS:
        problems.append(f"unknown kind {manifest.get('kind')!r}")
    if manifest.get("requires") not in (None, []):
        problems.append(f"'requires' must be empty in v1, got {manifest.get('requires')!r}")
    if audience == "shareable" and "scrub" not in manifest:
        problems.append("audience=shareable but no 'scrub' report present")
    if audience == "replica" and "scrub" in manifest:
        problems.append("audience=replica but a 'scrub' report is present (shareable-only field)")

    if not src.exists(SUMS_NAME):
        problems.append(f"no {SUMS_NAME}")
        return False, problems
    try:
        sums_bytes = src.read_bytes(SUMS_NAME, max_bytes=MAX_SUMS_BYTES)
    except SystemExit as e:
        problems.append(str(e))
        return False, problems
    actual_sums_hash = hashlib.sha256(sums_bytes).hexdigest()
    claimed_sums_hash = manifest.get("sha256sums_sha256")
    if actual_sums_hash != claimed_sums_hash:
        problems.append(
            f"sha256sums_sha256 mismatch: manifest says {claimed_sums_hash}, "
            f"{SUMS_NAME} hashes to {actual_sums_hash}")

    declared = {}
    for line in sums_bytes.decode(errors="replace").splitlines():
        if not line.strip():
            continue
        h, _, rel = line.partition("  ")
        if not rel:
            problems.append(f"malformed {SUMS_NAME} line: {line!r}")
            continue
        if not is_safe_relpath(rel):
            problems.append(f"{SUMS_NAME} declares an unsafe path, refusing to read it: {rel!r}")
            continue
        declared[rel] = h

    for rel, expected in declared.items():
        try:
            actual = src.sha256(rel)
        except SystemExit as e:
            problems.append(str(e))
            continue
        if actual is None:
            problems.append(f"declared file missing or not a regular file: {rel}")
        elif actual != expected:
            problems.append(f"hash mismatch: {rel} (expected {expected}, got {actual})")

    physical = src.list_files()
    expected_from_manifest = _expected_member_set(manifest, problems)
    declared_set = set(declared)
    extra_in_sums = sorted(declared_set - expected_from_manifest)
    missing_from_sums = sorted(expected_from_manifest - declared_set)
    extra_on_disk = sorted(physical - declared_set)
    if extra_in_sums:
        problems.append(
            f"{len(extra_in_sums)} file(s) in {SUMS_NAME} but not claimed by any manifest "
            f"part (possible smuggled content): " + ", ".join(extra_in_sums[:5])
            + (" …" if len(extra_in_sums) > 5 else ""))
    if missing_from_sums:
        problems.append(
            f"{len(missing_from_sums)} manifest-declared file(s) not in {SUMS_NAME}: "
            + ", ".join(missing_from_sums[:5])
            + (" …" if len(missing_from_sums) > 5 else ""))
    if extra_on_disk:
        problems.append(
            f"{len(extra_on_disk)} physical file(s) present but not in {SUMS_NAME} "
            f"(possible smuggled content): " + ", ".join(extra_on_disk[:5])
            + (" …" if len(extra_on_disk) > 5 else ""))

    if audience in AUDIENCES:
        for part in manifest.get("parts", []) or []:
            if not isinstance(part, dict):
                continue
            ptype, pschema = part.get("type"), part.get("schema")
            if ptype not in PART_AUDIENCES:
                problems.append(f"unknown part type {ptype!r} — refusing (no --skip-unknown-parts in P1)")
                continue
            if audience not in PART_AUDIENCES[ptype]:
                problems.append(
                    f"part type {ptype!r} is not permitted for audience {audience!r} "
                    f"(audience gate layer 3)")
                continue
            if not isinstance(pschema, int) or pschema > PART_SCHEMAS.get(ptype, 0):
                problems.append(
                    f"part {ptype!r} claims schema {pschema!r}, this importer's static "
                    f"table supports up to {PART_SCHEMAS.get(ptype, 0)} — refusing, no escape")

    return (len(problems) == 0), problems


# --- signature verification (mirrors pack_lib.verify_pack_sig, works on dir or tar).
# --allowed-signers must be given explicitly; never guess a default location.
SIG_NAMESPACE = "cc-pack"
SIG_STATES = ("unsigned", "invalid", "unknown-signer", "verification-error", "verified")


def _ssh_keygen_path():
    """Prefer Homebrew's ssh-keygen (FIDO/sk- key support on macOS), else PATH."""
    for candidate in ("/opt/homebrew/bin/ssh-keygen",):
        if Path(candidate).exists():
            return candidate
    return shutil.which("ssh-keygen") or "ssh-keygen"


def verify_pack_sig(src, allowed_signers):
    """Classify pack.json.sig; returns (state, principal_or_None, detail_or_None).

    Checks the signature and registry exist first (ssh-keygen can't tell those
    cases apart), then check-novalidate, then find-principals + verify. The
    caller enforces policy."""
    if not src.exists(SIG_NAME):
        return "unsigned", None, "no pack.json.sig present"
    if allowed_signers is None:
        return "verification-error", None, "no --allowed-signers path given"
    allowed = Path(allowed_signers)
    if not allowed.exists():
        return "verification-error", None, f"allowed_signers registry not found: {allowed}"
    if not src.exists(MANIFEST_NAME):
        return "verification-error", None, f"no {MANIFEST_NAME} in this pack"

    try:
        data = src.read_bytes(MANIFEST_NAME, max_bytes=MAX_MANIFEST_BYTES)
        sig_bytes = src.read_bytes(SIG_NAME, max_bytes=MAX_MANIFEST_BYTES)
    except SystemExit as e:
        return "verification-error", None, str(e)

    keygen = _ssh_keygen_path()
    sig_path = None
    try:
        fd, sig_path_str = tempfile.mkstemp(suffix=".sig")
        sig_path = Path(sig_path_str)
        with os.fdopen(fd, "wb") as f:
            f.write(sig_bytes)

        r = subprocess.run(
            [keygen, "-Y", "check-novalidate", "-n", SIG_NAMESPACE, "-s", str(sig_path)],
            input=data, capture_output=True, timeout=20)
        if r.returncode != 0:
            return "invalid", None, (r.stdout + r.stderr).decode(errors="replace").strip()[:300]

        r = subprocess.run(
            [keygen, "-Y", "find-principals", "-f", str(allowed), "-s", str(sig_path)],
            capture_output=True, text=True, timeout=20)
        if r.returncode != 0 or not r.stdout.strip():
            return "unknown-signer", None, "signature key is not present in allowed_signers"
        # A key may map to several principals; accept the first that verifies.
        candidates = [ln for ln in r.stdout.strip().splitlines() if ln.strip()]
        last_detail = None
        for principal in candidates:
            r = subprocess.run(
                [keygen, "-Y", "verify", "-f", str(allowed), "-I", principal,
                 "-n", SIG_NAMESPACE, "-s", str(sig_path)],
                input=data, capture_output=True, timeout=20)
            if r.returncode == 0:
                return "verified", principal, None
            last_detail = (r.stdout + r.stderr).decode(errors="replace").strip()[:300]
        return "invalid", candidates[0] if candidates else None, last_detail
    except subprocess.TimeoutExpired:
        return "verification-error", None, "ssh-keygen timed out"
    except OSError as e:
        return "verification-error", None, f"ssh-keygen invocation failed: {e}"
    finally:
        if sig_path is not None:
            try:
                sig_path.unlink()
            except OSError:
                pass


def cmd_inspect(args):
    src = PackSource(args.pack)
    try:
        manifest = read_manifest(src)
        print(f"id:        {_safe_display(manifest.get('id', '?'))}")
        print(f"kind:      {_safe_display(manifest.get('kind', '?'))}")
        print(f"audience:  {_safe_display(manifest.get('audience', '?'))}")
        print(f"created:   {_safe_display(manifest.get('created', '?'))}")
        print(f"builder:   {_safe_display(manifest.get('builder', '?'))}")
        tags = manifest.get("tags", [])
        tags_str = ", ".join(_safe_display(t) for t in tags) if isinstance(tags, list) else "(malformed)"
        print(f"tags:      {tags_str or '(none)'}")
        print(f"schema:    {_safe_display(manifest.get('pack_schema', '?'))}")
        sig_state, sig_principal, sig_detail = verify_pack_sig(src, args.allowed_signers)
        principal_note = f", principal={_safe_display(sig_principal)}" if sig_principal else ""
        print(f"signature: {sig_state}{principal_note}")
        if sig_state == "verification-error" and args.allowed_signers is None:
            print("           (pass --allowed-signers <path> to verify beyond presence)")
        elif sig_detail and sig_state not in ("unsigned", "verified"):
            print(f"           {_safe_display(sig_detail, maxlen=300)}")
        print(f"scrub:     {'yes' if 'scrub' in manifest else 'no'}")
        if manifest.get("origin"):
            print(f"origin:    {_safe_display(manifest['origin'])}")
        print(f"size:      {src.total_size()} bytes")
        print("NOTE: --inspect shows manifest claims; it does not verify hashes. "
              "Run --verify before trusting this pack's content.")
        print("parts:")
        parts = manifest.get("parts", [])
        if not isinstance(parts, list):
            print(f"  (malformed: 'parts' is {type(parts).__name__})")
            return 0
        for part in parts:
            if not isinstance(part, dict):
                print(f"  (malformed part entry: {type(part).__name__})")
                continue
            files = part.get("files", [])
            if not isinstance(files, list):
                files = []
            print(f"  - {_safe_display(part.get('type'))} (id={_safe_display(part.get('id'))}, "
                  f"schema={_safe_display(part.get('schema'))}, dest={_safe_display(part.get('dest'))!r}, "
                  f"{len(files)} file(s))")
            for f in files[:args.max_files]:
                print(f"      {_safe_display(f)}")
            if len(files) > args.max_files:
                print(f"      … {len(files) - args.max_files} more")
        return 0
    finally:
        src.close()


def cmd_verify(args):
    src = PackSource(args.pack)
    try:
        ok, problems = verify_pack(src)
        if ok:
            print("VERIFIED — pack_schema known, SHA256SUMS self-consistent, "
                  "every declared file present and hash-matching, no "
                  "undeclared/extra files, audience gate holds. See the "
                  "'signature:' line below for whether pack.json's own "
                  "metadata is also authenticated.")
        else:
            print("FAILED:")
            for p in problems:
                print(f"  - {_safe_display(p, maxlen=500)}")
        # Classification only; exit code reflects integrity. Callers parse this exact line.
        sig_state, sig_principal, sig_detail = verify_pack_sig(src, args.allowed_signers)
        principal_note = f" principal={_safe_display(sig_principal)}" if sig_principal else ""
        print(f"signature: {sig_state}{principal_note}")
        if sig_detail and sig_state not in ("unsigned", "verified"):
            print(f"  {_safe_display(sig_detail, maxlen=300)}")
        if args.target:
            print(f"\n--target {_safe_display(args.target)}: informational only here — this "
                  f"tool never writes. Run install.py --approve import-pack --from-pack "
                  f"<pack-dir> --target {_safe_display(args.target)} to actually apply it.")
        return 0 if ok else 1
    finally:
        src.close()


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0],
                                  formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--pack", required=True, help="pack directory or .tar file")
    ap.add_argument("--target", default=None,
                     help="informational only — this tool never writes; see install.py --approve import-pack")
    ap.add_argument("--max-files", type=int, default=10,
                     help="--inspect: cap listed filenames per part (default 10)")
    ap.add_argument("--allowed-signers", default=None,
                     help="signer registry path for signature verification — "
                          "REQUIRED to get anything beyond presence/'unsigned'; "
                          "this tool never probes a default location (it ships "
                          "to hosts without the cc-pack workspace repo)")
    g = ap.add_mutually_exclusive_group(required=True)
    g.add_argument("--inspect", action="store_true")
    g.add_argument("--verify", action="store_true")
    args = ap.parse_args()
    return cmd_inspect(args) if args.inspect else cmd_verify(args)


if __name__ == "__main__":
    sys.exit(main())
