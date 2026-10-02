#!/usr/bin/env bash
# Thin entry point: exec the reconciliation logic in sync.py.
# Uses PATH's python3, not /usr/bin/python3, so a venv holding PyYAML is honoured.
#
#   ./sync.sh            install/reconcile every scheduler/manifest.yml job (idempotent)
#   ./sync.sh --check    reconcile only — report drift, change nothing; exit 1 on drift
set -euo pipefail
here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec python3 "$here/sync.py" "$@"
