from __future__ import annotations

import scripts.sqlite_jsonb_compat  # noqa: F401,E402
import app.bot  # noqa: F401,E402

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.db import session_scope
from app.models import GenerationTask
from app.repositories import ensure_defaults
from scripts import admin_smoke_current as current


async def _portable_feed_tasks(session, limit: int = 20):
    rows = list(
        await session.scalars(
            select(GenerationTask)
            .where(
                GenerationTask.is_public_feed.is_(True),
                GenerationTask.feed_status == "approved",
                GenerationTask.status == "success",
            )
            .order_by(
                GenerationTask.published_at.desc(),
                GenerationTask.created_at.desc(),
            )
            .limit(max(1, min(int(limit) * 2, 100)))
        )
    )
    return [task for task in rows if task.result_urls][: max(1, min(int(limit), 100))]


_original_init_db = current.init_db


async def _init_db_with_defaults(engine) -> None:
    await _original_init_db(engine)
    factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)
    async with session_scope(factory) as session:
        await ensure_defaults(session, get_settings().admin_ids)


current.init_db = _init_db_with_defaults
current.get_feed_tasks = _portable_feed_tasks
amain = current.amain
main = current.main

__all__ = ["amain", "main"]


if __name__ == "__main__":
    main()
