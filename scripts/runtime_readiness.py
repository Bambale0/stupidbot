from __future__ import annotations

import asyncio

from redis.asyncio import Redis
from sqlalchemy import text

if __package__ in {None, ""}:
    from _bootstrap import add_project_root_to_path

    add_project_root_to_path()

from app.config import get_settings
from app.db import build_engine

CHECK_TIMEOUT_SECONDS = 5.0


async def check_database(engine) -> None:
    async with engine.connect() as connection:
        result = await connection.execute(text("SELECT 1"))
        assert result.scalar_one() == 1


async def check_redis(redis: Redis) -> None:
    assert await redis.ping() is True


async def amain() -> None:
    settings = get_settings()
    engine = build_engine(settings)
    redis = Redis.from_url(settings.redis_url, decode_responses=False)
    try:
        await asyncio.wait_for(check_database(engine), timeout=CHECK_TIMEOUT_SECONDS)
        await asyncio.wait_for(check_redis(redis), timeout=CHECK_TIMEOUT_SECONDS)
    finally:
        await redis.aclose()
        await engine.dispose()
    print("Runtime readiness passed: PostgreSQL and Redis")


if __name__ == "__main__":
    asyncio.run(amain())
