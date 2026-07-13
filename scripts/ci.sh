#!/usr/bin/env bash
set -euo pipefail

python3 -m compileall -q app scripts
python3 scripts/regression_deployment_safety.py
python3 scripts/regression_bot_ux.py
python3 scripts/reference_regression.py
python3 scripts/regression_backend_contracts.py
python3 scripts/regression_gallery_compat.py
python3 scripts/admin_smoke.py
python3 -c 'import runpy; import scripts.sqlite_jsonb_compat; runpy.run_path("scripts/regression_500_current.py", run_name="__main__")'
