#!/usr/bin/env bash
set -euo pipefail

python3 -m compileall -q app scripts
python3 -m pip check
ruff check --select E9,F63,F7,F82 app scripts
