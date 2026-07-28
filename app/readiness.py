from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Awaitable

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from sqlalchemy import text

logger = logging.getLogger(__name__)
READINESS_TIMEOUT_SECONDS = 3.0
_INSTALL_MARKER = "_stupidbot_http_readiness_installed"
_NO_STORE_HEADERS = {"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"}


async def _check_database(engine: Any) -> None:
    async with engine.connect() as connection:
        result = await connection.execute(text("SELECT 1"))
        if result.scalar_one() != 1:
            raise RuntimeError("database readiness query returned an unexpected value")


async def _check_redis(redis: Any) -> None:
    if await redis.ping() is not True:
        raise RuntimeError("redis PING returned an unexpected value")


async def _check_telegram(bot: Any) -> None:
    identity = await bot.get_me()
    if not getattr(identity, "id", None):
        raise RuntimeError("telegram getMe returned no bot id")


def tracker_is_running(tracker: Any) -> bool:
    task = getattr(tracker, "_task", None)
    stop_event = getattr(tracker, "_stop", None)
    if task is None or task.done():
        return False
    return stop_event is not None and not stop_event.is_set()


async def _guarded_check(name: str, operation: Awaitable[None]) -> tuple[str, str, int]:
    started = time.monotonic()
    try:
        await asyncio.wait_for(operation, timeout=READINESS_TIMEOUT_SECONDS)
    except Exception as exc:
        latency_ms = max(0, int((time.monotonic() - started) * 1000))
        logger.warning(
            "readiness_check_failed component=%s error=%s latency_ms=%d",
            name,
            type(exc).__name__,
            latency_ms,
        )
        return name, "error", latency_ms
    latency_ms = max(0, int((time.monotonic() - started) * 1000))
    return name, "ok", latency_ms


async def readiness_payload(
    *,
    engine: Any,
    redis: Any,
    tracker: Any,
    bot: Any | None = None,
) -> dict[str, Any]:
    operations: list[Awaitable[tuple[str, str, int]]] = [
        _guarded_check("database", _check_database(engine)),
        _guarded_check("redis", _check_redis(redis)),
    ]
    if bot is not None:
        operations.append(_guarded_check("telegram", _check_telegram(bot)))

    results = await asyncio.gather(*operations)
    checks = {name: result for name, result, _ in results}
    latency_ms = {name: latency for name, _, latency in results}
    if bot is None:
        checks["telegram"] = "skipped"
        latency_ms["telegram"] = 0

    checks["tracker"] = "ok" if tracker_is_running(tracker) else "error"
    latency_ms["tracker"] = 0
    status = "ready" if all(value in {"ok", "skipped"} for value in checks.values()) else "not_ready"
    return {
        "status": status,
        "checks": checks,
        "latency_ms": latency_ms,
    }


async def readiness_response(request: Request) -> JSONResponse:
    payload = await readiness_payload(
        engine=request.app.state.engine,
        redis=request.app.state.redis,
        bot=getattr(request.app.state, "bot", None),
        tracker=request.app.state.tracker,
    )
    return JSONResponse(
        payload,
        status_code=200 if payload["status"] == "ready" else 503,
        headers=_NO_STORE_HEADERS,
    )


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
