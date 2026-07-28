from __future__ import annotations

import asyncio

if __package__ in {None, ""}:
    from _bootstrap import add_project_root_to_path

    add_project_root_to_path()

from scripts import current_policy_regression_adapter as adapter
from scripts.current_model_policy_patch import install
from scripts.private_feed_policy_patch import install as install_private_feed_policy
from scripts.regression_dynamic_packages import run_dynamic_package_regression
from scripts.regression_tma_request_integrity import run_tma_request_integrity_regression

install(adapter)
install_private_feed_policy(adapter)


async def amain() -> None:
    await run_dynamic_package_regression()
    await run_tma_request_integrity_regression()
    await adapter.amain()


if __name__ == "__main__":
    asyncio.run(amain())
