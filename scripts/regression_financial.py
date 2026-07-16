from __future__ import annotations

import asyncio
from uuid import uuid4

if __package__ in {None, ""}:
    from _bootstrap import add_project_root_to_path

    add_project_root_to_path()

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.db import build_engine, init_db
from app.services.referrals import install_repository_patches

install_repository_patches()

from scripts.financial_regression_core import run_core  # noqa: E402
from scripts.financial_regression_guards import (  # noqa: E402
    custom_sales_are_disabled,
    run_guards,
)
from scripts.regression_admin_operations import run_admin_operations_regression  # noqa: E402
from scripts.regression_billing_referrals import run_billing_referral_regression  # noqa: E402
from scripts.regression_feed_social import run_feed_social_regression  # noqa: E402
from scripts.regression_growth_rewards import run_growth_rewards_regression  # noqa: E402


async def amain() -> None:
    await custom_sales_are_disabled()
    settings = get_settings()
    engine = build_engine(settings)
    await init_db(engine)
    assert engine.dialect.name == "postgresql", "financial regression requires PostgreSQL"
    async with engine.connect() as connection:
        transaction = await connection.begin()
        factory = async_sessionmaker(connection, expire_on_commit=False, autoflush=False)
        try:
            async with factory() as session:
                suffix = uuid4().hex[:8]
                context = await run_core(session, suffix)
                await run_guards(session, settings, suffix, context)
                await run_billing_referral_regression(session, suffix)
                await run_growth_rewards_regression(session, suffix)
                await run_admin_operations_regression(session, factory, suffix)
                await run_feed_social_regression(session, suffix)
        finally:
            await transaction.rollback()
    await engine.dispose()
    print("financial integrity regression passed")


if __name__ == "__main__":
    asyncio.run(amain())
