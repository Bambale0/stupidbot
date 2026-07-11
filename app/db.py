from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncAttrs, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import Settings


class Base(AsyncAttrs, DeclarativeBase):
    pass


SCHEMA_COMPAT_SQL: tuple[str, ...] = (
    """
    ALTER TABLE users
    ADD COLUMN IF NOT EXISTS is_blocked boolean NOT NULL DEFAULT false
    """,
    """
    ALTER TABLE users
    ADD COLUMN IF NOT EXISTS partner_code varchar(64)
    """,
    """
    ALTER TABLE users
    ADD COLUMN IF NOT EXISTS referred_by_user_id integer REFERENCES users(id)
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ix_users_partner_code_unique
    ON users (partner_code)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_users_referred_by_user_id
    ON users (referred_by_user_id)
    """,
    """
    ALTER TABLE users
    ADD COLUMN IF NOT EXISTS affiliate_balance_kopecks integer NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE users
    ADD COLUMN IF NOT EXISTS photo_credits_balance integer NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE users
    ADD COLUMN IF NOT EXISTS video_credits_balance integer NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE users
    ADD COLUMN IF NOT EXISTS affiliate_earned_kopecks integer NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE users
    ADD COLUMN IF NOT EXISTS affiliate_commission_rate_bps integer NOT NULL DEFAULT 3000
    """,
    """
    ALTER TABLE payments
    ADD COLUMN IF NOT EXISTS affiliate_commission_user_id integer REFERENCES users(id)
    """,
    """
    ALTER TABLE payments
    ADD COLUMN IF NOT EXISTS affiliate_commission_kopecks integer NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE credit_packages
    ADD COLUMN IF NOT EXISTS terms text
    """,
    """
    ALTER TABLE credit_packages
    ADD COLUMN IF NOT EXISTS photo_credits integer NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE credit_packages
    ADD COLUMN IF NOT EXISTS video_credits integer NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE generation_tasks
    ADD COLUMN IF NOT EXISTS is_public_feed boolean NOT NULL DEFAULT false
    """,
    """
    ALTER TABLE generation_tasks
    ADD COLUMN IF NOT EXISTS feed_status varchar(32) NOT NULL DEFAULT 'hidden'
    """,
    """
    ALTER TABLE generation_tasks
    ADD COLUMN IF NOT EXISTS published_at timestamp with time zone
    """,
    """
    ALTER TABLE generation_tasks
    ADD COLUMN IF NOT EXISTS likes_count integer NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE generation_tasks
    ADD COLUMN IF NOT EXISTS shares_count integer NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE generation_tasks
    ADD COLUMN IF NOT EXISTS source_feed_task_id integer REFERENCES generation_tasks(id)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_generation_tasks_public_feed
    ON generation_tasks (is_public_feed, published_at)
    """,
)


def build_engine(settings: Settings):
    return create_async_engine(settings.database_url, pool_pre_ping=True, echo=False)


def build_session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def init_db(engine) -> None:
    from app import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        for statement in SCHEMA_COMPAT_SQL:
            await conn.execute(text(statement))


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
