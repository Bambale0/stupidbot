from __future__ import annotations

from sqlalchemy import select

from app.models import GenerationTask


async def get_feed_tasks(session, limit: int = 20):
    score = GenerationTask.likes_count + GenerationTask.shares_count * 3
    rows = list(
        await session.scalars(
            select(GenerationTask)
            .where(
                GenerationTask.is_public_feed.is_(True),
                GenerationTask.feed_status == "approved",
                GenerationTask.status == "success",
            )
            .order_by(
                score.desc(),
                GenerationTask.published_at.desc().nullslast(),
                GenerationTask.created_at.desc(),
            )
            .limit(100)
        )
    )
    return [task for task in rows if task.result_urls][: max(1, min(int(limit), 100))]
