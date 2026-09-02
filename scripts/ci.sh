#!/usr/bin/env bash
set -euo pipefail

python3 -m compileall -q app scripts
python3 -m pip check
ruff check app scripts
