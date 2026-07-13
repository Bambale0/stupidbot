from __future__ import annotations

import app.bot  # noqa: F401,E402

from sqlalchemy import select

from app.models import GenerationTask
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


current.get_feed_tasks = _portable_feed_tasks
amain = current.amain
main = current.main

__all__ = ["amain", "main"]


if __name__ == "__main__":
    main()
