#!/usr/bin/env bash
# Thin entry point: exec the real reconciliation logic in sync.py. A shell
# wrapper stays here (rather than just documenting `python3 sync.py`)
# because it's the one command every install/upgrade/doctor step can call
# without knowing which python3 is on $PATH vs the one that ran the
# installer — mirrors keyvault's own bash-wrapper-over-python convention.
#
#   ./sync.sh            install/reconcile every scheduler/manifest.yml job (idempotent)
#   ./sync.sh --check    reconcile only — report drift, change nothing; exit 1 on drift
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec /usr/bin/python3 "$here/sync.py" "$@"
