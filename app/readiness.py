from __future__ import annotations

import asyncio
import logging
from typing import Any, Awaitable

from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import text

logger = logging.getLogger(__name__)
READINESS_TIMEOUT_SECONDS = 3.0
_INSTALL_MARKER = "_stupidbot_http_readiness_installed"


async def _check_database(engine: Any) -> None:
    async with engine.connect() as connection:
        result = await connection.execute(text("SELECT 1"))
        if result.scalar_one() != 1:
            raise RuntimeError("database readiness query returned an unexpected value")


async def _check_redis(redis: Any) -> None:
    if await redis.ping() is not True:
        raise RuntimeError("redis PING returned an unexpected value")


def tracker_is_running(tracker: Any) -> bool:
    task = getattr(tracker, "_task", None)
    stop_event = getattr(tracker, "_stop", None)
    if task is None or task.done():
        return False
    return stop_event is not None and not stop_event.is_set()


async def _guarded_check(name: str, operation: Awaitable[None]) -> tuple[str, str]:
    try:
        await asyncio.wait_for(operation, timeout=READINESS_TIMEOUT_SECONDS)
    except Exception as exc:
        logger.warning("readiness_check_failed component=%s error=%s", name, type(exc).__name__)
        return name, "error"
    return name, "ok"


async def readiness_payload(*, engine: Any, redis: Any, tracker: Any) -> dict[str, Any]:
    database, redis_result = await asyncio.gather(
        _guarded_check("database", _check_database(engine)),
        _guarded_check("redis", _check_redis(redis)),
    )
    checks = {
        database[0]: database[1],
        redis_result[0]: redis_result[1],
        "tracker": "ok" if tracker_is_running(tracker) else "error",
    }
    status = "ready" if all(value == "ok" for value in checks.values()) else "not_ready"
    return {"status": status, "checks": checks}


async def readiness_response(request: Request) -> dict[str, Any]:
    payload = await readiness_payload(
        engine=request.app.state.engine,
        redis=request.app.state.redis,
        tracker=request.app.state.tracker,
    )
    if payload["status"] != "ready":
        raise HTTPException(status_code=503, detail=payload)
    return payload


def install_http_readiness_route() -> None:
    """Register `/ready` on FastAPI instances created after application bootstrap."""

    if getattr(FastAPI, _INSTALL_MARKER, False):
        return

    original_init = FastAPI.__init__

    def init_with_readiness(self: FastAPI, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        if not any(getattr(route, "path", None) == "/ready" for route in self.routes):
            self.add_api_route(
                "/ready",
                readiness_response,
                methods=["GET"],
                tags=["operations"],
                summary="Runtime readiness",
            )

    FastAPI.__init__ = init_with_readiness  # type: ignore[method-assign]
    setattr(FastAPI, _INSTALL_MARKER, True)
