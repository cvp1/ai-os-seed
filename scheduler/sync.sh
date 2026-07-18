#!/usr/bin/env bash
# Thin entry point: exec the real reconciliation logic in sync.py. A shell
# wrapper stays here (rather than just documenting `python3 sync.py`)
# because it's the one command every install/upgrade/doctor step can call
# uniformly — mirrors keyvault's own bash-wrapper-over-python convention.
#
# Uses PATH's `python3` deliberately, NOT a hardcoded /usr/bin/python3 —
# sync.py needs PyYAML, and AGENT-INSTALL.md's readiness guidance may have
# the operator install it into a venv (common on modern macOS/Homebrew
# Python, PEP 668) rather than the bare system interpreter. Honoring PATH
# means "whichever python3 you set up per the readiness check" just works;
# a hardcoded system path silently wouldn't see that venv (caught live in
# this project's own CI, macOS runner, 2026-07-18).
#
#   ./sync.sh            install/reconcile every scheduler/manifest.yml job (idempotent)
#   ./sync.sh --check    reconcile only — report drift, change nothing; exit 1 on drift
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$here/sync.py" "$@"
