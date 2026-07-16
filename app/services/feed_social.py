from __future__ import annotations

from typing import Any

from sqlalchemy import ForeignKey, Index, UniqueConstraint, func, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import Mapped, mapped_column

from app import db as app_db
from app.db import Base
from app.models import FeedLike, GenerationTask, User


class FeedDislike(Base):
    __tablename__ = "feed_dislikes"
    __table_args__ = (
        UniqueConstraint("user_id", "task_id", name="uq_feed_dislikes_user_task"),
        Index("ix_feed_dislikes_user_id", "user_id"),
        Index("ix_feed_dislikes_task_id", "task_id"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    task_id: Mapped[int] = mapped_column(ForeignKey("generation_tasks.id", ondelete="CASCADE"))


FEED_SOCIAL_SCHEMA_SQL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS feed_dislikes (
        id serial PRIMARY KEY,
        user_id integer NOT NULL REFERENCES users(id) ON DELETE CASCADE,
        task_id integer NOT NULL REFERENCES generation_tasks(id) ON DELETE CASCADE,
        CONSTRAINT uq_feed_dislikes_user_task UNIQUE (user_id, task_id)
    )
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_feed_dislikes_user_id
    ON feed_dislikes (user_id)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_feed_dislikes_task_id
    ON feed_dislikes (task_id)
    """,
)

if not any("feed_dislikes" in statement for statement in app_db.SCHEMA_COMPAT_SQL):
    app_db.SCHEMA_COMPAT_SQL = (*app_db.SCHEMA_COMPAT_SQL, *FEED_SOCIAL_SCHEMA_SQL)


def _public_feed_conditions() -> tuple[Any, ...]:
    return (
        GenerationTask.is_public_feed.is_(True),
        GenerationTask.feed_status == "approved",
        GenerationTask.status == "success",
        GenerationTask.result_urls.is_not(None),
        GenerationTask.result_urls != [],
    )


async def get_social_feed_tasks(session: AsyncSession, limit: int = 30) -> list[GenerationTask]:
    dislike_count = (
        select(func.count(FeedDislike.id))
        .where(FeedDislike.task_id == GenerationTask.id)
        .correlate(GenerationTask)
        .scalar_subquery()
    )
    score = GenerationTask.likes_count - dislike_count + GenerationTask.shares_count * 3
    stmt = (
        select(GenerationTask)
        .where(*_public_feed_conditions())
        .order_by(
            score.desc(),
            GenerationTask.published_at.desc().nullslast(),
            GenerationTask.created_at.desc(),
        )
        .limit(max(1, min(int(limit), 100)))
    )
    return list(await session.scalars(stmt))


async def get_public_feed_task_locked(
    session: AsyncSession,
    task_id: int,
) -> GenerationTask | None:
    return await session.scalar(
        select(GenerationTask)
        .where(GenerationTask.id == task_id, *_public_feed_conditions())
        .with_for_update()
    )


async def feed_dislike_count(session: AsyncSession, task_id: int) -> int:
    count = await session.scalar(
        select(func.count(FeedDislike.id)).where(FeedDislike.task_id == task_id)
    )
    return int(count or 0)


async def toggle_feed_reaction(
    session: AsyncSession,
    *,
    task_id: int,
    user_id: int,
) -> tuple[int | None, bool]:
    """Toggle a feed reaction.

    Positive ``task_id`` means like. Negative ``task_id`` means dislike. The signed-id
    convention keeps the existing Mini App endpoint and Telegram callback contract
    backwards-compatible while adding a second mutually exclusive reaction.
    """

    is_dislike = int(task_id) < 0
    resolved_task_id = abs(int(task_id))
    task = await get_public_feed_task_locked(session, resolved_task_id)
    if not task:
        return None, False

    target_model = FeedDislike if is_dislike else FeedLike
    opposite_model = FeedLike if is_dislike else FeedDislike
    existing = await session.scalar(
        select(target_model).where(
            target_model.user_id == user_id,
            target_model.task_id == resolved_task_id,
        )
    )

    if existing:
        await session.delete(existing)
        if not is_dislike:
            task.likes_count = max(0, int(task.likes_count or 0) - 1)
        await session.flush()
        count = (
            await feed_dislike_count(session, resolved_task_id)
            if is_dislike
            else int(task.likes_count or 0)
        )
        return count, False

    opposite = await session.scalar(
        select(opposite_model).where(
            opposite_model.user_id == user_id,
            opposite_model.task_id == resolved_task_id,
        )
    )
    if opposite:
        await session.delete(opposite)
        if is_dislike:
            task.likes_count = max(0, int(task.likes_count or 0) - 1)

    session.add(target_model(user_id=user_id, task_id=resolved_task_id))
    if not is_dislike:
        task.likes_count = int(task.likes_count or 0) + 1
    await session.flush()

    count = (
        await feed_dislike_count(session, resolved_task_id)
        if is_dislike
        else int(task.likes_count or 0)
    )
    return count, True


async def _author_feed_stats(session: AsyncSession, user_id: int) -> dict[str, int]:
    cache = session.info.setdefault("feed_author_stats", {})
    cached = cache.get(int(user_id))
    if isinstance(cached, dict):
        return dict(cached)

    works = await session.scalar(
        select(func.count(GenerationTask.id)).where(
            GenerationTask.user_id == user_id,
            *_public_feed_conditions(),
        )
    )
    likes = await session.scalar(
        select(func.coalesce(func.sum(GenerationTask.likes_count), 0)).where(
            GenerationTask.user_id == user_id,
            *_public_feed_conditions(),
        )
    )
    dislikes = await session.scalar(
        select(func.count(FeedDislike.id))
        .join(GenerationTask, GenerationTask.id == FeedDislike.task_id)
        .where(
            GenerationTask.user_id == user_id,
            *_public_feed_conditions(),
        )
    )
    stats = {
        "works": int(works or 0),
        "likes": int(likes or 0),
        "dislikes": int(dislikes or 0),
    }
    cache[int(user_id)] = dict(stats)
    return stats


def _public_user_name(user: User | None) -> str:
    if not user:
        return "BANANA user"
    if user.username:
        return f"@{user.username}"
    name = " ".join(part for part in (user.first_name, user.last_name) if part)
    return name or "BANANA user"


async def serialize_social_feed_task(
    session: AsyncSession,
    task: GenerationTask,
) -> dict[str, Any]:
    user = await session.get(User, task.user_id)
    media_url = str(task.result_urls[0]) if task.result_urls else ""
    payload = dict(task.input_payload or {})
    author = _public_user_name(user)
    stats = await _author_feed_stats(session, task.user_id)
    dislikes = await feed_dislike_count(session, task.id)
    author_key = str(user.partner_code or f"creator-{user.id}") if user else "banana-user"
    return {
        "id": task.id,
        "media_url": media_url,
        "media_type": "video"
        if task.model_code.startswith("kling")
        or "video" in task.model_code
        or str(payload.get("provider") or "") == "kie-video"
        else "image",
        "model_code": task.model_code,
        "author": author,
        "author_key": author_key,
        "author_username": user.username if user else None,
        "author_initial": author.lstrip("@").strip()[:1].upper() or "B",
        "author_profile": {
            "key": author_key,
            "name": author,
            "username": user.username if user else None,
            "works": stats["works"],
            "likes": stats["likes"],
            "dislikes": stats["dislikes"],
        },
        "likes": int(task.likes_count or 0),
        "dislikes": dislikes,
        "shares": int(task.shares_count or 0),
        "published_at": task.published_at.isoformat() if task.published_at else None,
        "aspect_ratio": payload.get("aspect_ratio"),
        "duration": payload.get("duration"),
    }


def install_feed_social_patch(repositories: Any) -> None:
    repositories.get_feed_tasks = get_social_feed_tasks
    repositories.like_feed_task = toggle_feed_reaction
    repositories.serialize_feed_task = serialize_social_feed_task
