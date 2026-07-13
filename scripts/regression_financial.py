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
        finally:
            await transaction.rollback()
    await engine.dispose()
    print("financial integrity regression passed")


if __name__ == "__main__":
    asyncio.run(amain())
