#!/usr/bin/env bash
set -euo pipefail

python3 -m compileall -q app scripts
python3 scripts/admin_smoke.py
python3 scripts/regression_500_current.py
