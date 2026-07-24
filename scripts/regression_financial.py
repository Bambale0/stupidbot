from __future__ import annotations

import asyncio
from uuid import uuid4

if __package__ in {None, ""}:
    from _bootstrap import add_project_root_to_path

    add_project_root_to_path()

from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.db import build_engine, init_db
from app.services.model_contracts import (
    install_kie_image_contract,
    install_model_repository_contracts,
)
from app.services.referrals import install_repository_patches

install_repository_patches()
install_model_repository_contracts()
install_kie_image_contract()

from scripts.financial_regression_core import run_core  # noqa: E402
from scripts.financial_regression_guards import (  # noqa: E402
    custom_sales_are_disabled,
    run_guards,
)
from scripts.regression_admin_operations import run_admin_operations_regression  # noqa: E402
from scripts.regression_billing_referrals import run_billing_referral_regression  # noqa: E402
from scripts.regression_feed_social import run_feed_social_regression  # noqa: E402
from scripts.regression_growth_rewards import run_growth_rewards_regression  # noqa: E402
from scripts.regression_model_provider_contracts import (  # noqa: E402
    check_catalog,
    check_frontend_contract,
    check_kie_lite_payload,
    check_normalization_and_geometry,
)
from scripts.regression_telegram_feed_links import (  # noqa: E402
    run_telegram_feed_links_regression,
)


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
    check_catalog()
    check_normalization_and_geometry()
    await check_kie_lite_payload()
    check_frontend_contract()
    run_telegram_feed_links_regression()
    await engine.dispose()
    print("financial integrity regression passed")


if __name__ == "__main__":
    asyncio.run(amain())
