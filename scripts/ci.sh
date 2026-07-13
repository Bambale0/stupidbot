#!/usr/bin/env bash
set -euo pipefail

python3 -m compileall -q app scripts
python3 scripts/regression_deployment_safety.py
python3 scripts/regression_bot_ux.py
python3 scripts/reference_regression.py
python3 scripts/regression_backend_contracts.py
python3 scripts/regression_gallery_compat.py
python3 scripts/admin_smoke.py
python3 -c 'import asyncio; import scripts.sqlite_jsonb_compat; import scripts.sqlite_feed_query as feed; import scripts.regression_500_current as regression; regression.adapter.legacy.get_feed_tasks = feed.get_feed_tasks; asyncio.run(regression.amain())'
