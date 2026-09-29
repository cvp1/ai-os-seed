#!/usr/bin/env bash
set -euo pipefail
PYTHON="${TEST_PYTHON:-python3}"
# coverage is a nice-to-have, not a dependency: a stdlib-only host (the seed's
# stated target) runs the same suite under plain unittest (SEED-081).
if "$PYTHON" -c "import coverage" 2>/dev/null; then
  "$PYTHON" -m coverage erase
  "$PYTHON" -m coverage run -m unittest -v test_core.py test_learn_harvest.py
  "$PYTHON" -m coverage run --append drill.py
  "$PYTHON" -m coverage report -m
else
  echo "coverage not installed — running the suite under plain unittest" >&2
  "$PYTHON" -m unittest -v test_core.py test_learn_harvest.py
  "$PYTHON" drill.py
fi
