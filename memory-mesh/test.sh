#!/usr/bin/env bash
set -euo pipefail
PYTHON="${TEST_PYTHON:-python3}"
"$PYTHON" -m coverage erase
"$PYTHON" -m coverage run -m unittest -v test_core.py
"$PYTHON" -m coverage run --append drill.py
"$PYTHON" -m coverage report -m
