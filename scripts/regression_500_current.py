from __future__ import annotations

import asyncio

if __package__ in {None, ""}:
    from _bootstrap import add_project_root_to_path

    add_project_root_to_path()

from scripts.current_policy_regression_adapter import amain


if __name__ == "__main__":
    asyncio.run(amain())
