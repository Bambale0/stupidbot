from __future__ import annotations

import hmac
import logging
import time
from contextlib import suppress
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, select

from app.db import session_scope
from app.models import Broadcast, CreditPackage, GenerationTask, Payment
from app.readiness import tracker_is_running

logger = logging.getLogger(__name__)
OPERATIONS_METRICS_PATH = "/ops/metrics"
METRICS_REDIS_KEY = "ops:http:metrics"
METRICS_TTL_SECONDS = 30 * 24 * 60 * 60
_INSTALL_MARKER = "_stupidbot_http_operations_installed"
_NO_STORE_HEADERS = {"Cache-Control": "no-store, max-age=0", "Pragma": "no-cache"}


def _route_metric_name(method: str, path: str) -> str | None:
    method = method.upper()
    if method == "GET" and path == "/api/tma/app/packages":
        return "package_catalog"
    if method == "POST" and path == "/api/tma/app/payments":
        return "payment_create"
    if method == "POST" and path == "/comet/callback":
        return "provider_callback"
    if method == "POST" and path == "/payments/tbank/callback":
        return "payment_callback"
    if method == "POST" and path.endswith("/action") and path.startswith("/api/tma/app/feed/"):
        return "feed_mutation"
    return None


async def _record_http_metric(
    request: Request,
    *,
    metric_name: str,
    status_code: int,
    duration_ms: int,
    exception: bool = False,
) -> None:
    redis = getattr(request.app.state, "redis", None)
    if redis is None:
        return
    prefix = f"http.{metric_name}"
    try:
        pipeline = redis.pipeline(transaction=False)
        pipeline.hincrby(METRICS_REDIS_KEY, f"{prefix}.requests", 1)
        pipeline.hincrby(METRICS_REDIS_KEY, f"{prefix}.status.{int(status_code)}", 1)
        if exception:
            pipeline.hincrby(METRICS_REDIS_KEY, f"{prefix}.exceptions", 1)
        pipeline.hset(
            METRICS_REDIS_KEY,
            mapping={
                f"{prefix}.last_status": int(status_code),
                f"{prefix}.last_duration_ms": max(0, int(duration_ms)),
                f"{prefix}.last_at_unix": int(time.time()),
            },
        )
        pipeline.expire(METRICS_REDIS_KEY, METRICS_TTL_SECONDS)
        await pipeline.execute()
    except Exception as exc:
        logger.warning(
            "operations_metric_write_failed metric=%s error=%s",
            metric_name,
            type(exc).__name__,
        )


async def operations_metrics_middleware(
    request: Request,
    call_next: Callable[[Request], Awaitable[Any]],
) -> Any:
    metric_name = _route_metric_name(request.method, request.url.path)
    if metric_name is None:
        return await call_next(request)

    started = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        await _record_http_metric(
            request,
            metric_name=metric_name,
            status_code=500,
            duration_ms=int((time.monotonic() - started) * 1000),
            exception=True,
        )
        raise

    await _record_http_metric(
        request,
        metric_name=metric_name,
        status_code=int(getattr(response, "status_code", 200)),
        duration_ms=int((time.monotonic() - started) * 1000),
    )
    return response


def _operations_token(settings: Any) -> str:
    return str(
        getattr(settings, "operations_token", None)
        or getattr(settings, "telegram_secret_token", None)
        or ""
    ).strip()


def _provided_operations_token(request: Request) -> str:
    header = str(request.headers.get("x-operations-token") or "").strip()
    if header:
        return header
    authorization = str(request.headers.get("authorization") or "").strip()
    if authorization.lower().startswith("bearer "):
        return authorization[7:].strip()
    return ""


def _authorize_operations(request: Request) -> None:
    settings = request.app.state.context.settings
    expected = _operations_token(settings)
    environment = str(getattr(settings, "app_env", "local") or "local").strip().lower()
    if not expected:
        if environment in {"prod", "production"}:
            raise HTTPException(status_code=503, detail="Operations token is not configured")
        return
    supplied = _provided_operations_token(request)
    if not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=401, detail="Invalid operations token")


def _decode_redis_hash(raw: dict[Any, Any]) -> dict[str, int | str]:
    decoded: dict[str, int | str] = {}
    for raw_key, raw_value in raw.items():
        key = raw_key.decode() if isinstance(raw_key, bytes) else str(raw_key)
        value = raw_value.decode() if isinstance(raw_value, bytes) else str(raw_value)
        with suppress(ValueError):
            decoded[key] = int(value)
            continue
        decoded[key] = value
    return decoded


async def _grouped_status_counts(session: Any, model: Any) -> dict[str, int]:
    rows = await session.execute(select(model.status, func.count()).group_by(model.status))
    return {str(status or "unknown"): int(count or 0) for status, count in rows.all()}


async def operations_metrics_payload(request: Request) -> dict[str, Any]:
    context = request.app.state.context
    async with session_scope(context.session_factory) as session:
        generation_tasks = await _grouped_status_counts(session, GenerationTask)
        payments = await _grouped_status_counts(session, Payment)
        broadcasts = await _grouped_status_counts(session, Broadcast)
        packages = list(await session.scalars(select(CreditPackage)))

    from app import repositories

    package_counts = {
        "total": len(packages),
        "enabled": sum(1 for package in packages if bool(package.is_enabled)),
        "sellable": sum(1 for package in packages if repositories.package_is_user_visible(package)),
    }
    redis_metrics: dict[str, int | str] = {}
    with suppress(Exception):
        redis_metrics = _decode_redis_hash(await request.app.state.redis.hgetall(METRICS_REDIS_KEY))

    active_generation_states = {"submitted", "waiting", "queuing", "generating", "submitting"}
    active_generation_tasks = sum(
        count for status, count in generation_tasks.items() if status in active_generation_states
    )
    return {
        "status": "ok",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "runtime": {
            "tracker_running": tracker_is_running(request.app.state.tracker),
        },
        "catalog": package_counts,
        "generation_tasks": {
            "active": active_generation_tasks,
            "by_status": generation_tasks,
        },
        "payments": {"by_status": payments},
        "broadcasts": {"by_status": broadcasts},
        "http": redis_metrics,
    }


async def operations_metrics_response(request: Request) -> JSONResponse:
    _authorize_operations(request)
    payload = await operations_metrics_payload(request)
    return JSONResponse(payload, headers=_NO_STORE_HEADERS)


def install_http_operations_routes() -> None:
    """Install low-cardinality request metrics and an authenticated operations endpoint."""

    if getattr(FastAPI, _INSTALL_MARKER, False):
        return

    original_init = FastAPI.__init__

    def init_with_operations(self: FastAPI, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.middleware("http")(operations_metrics_middleware)
        if not any(getattr(route, "path", None) == OPERATIONS_METRICS_PATH for route in self.routes):
            self.add_api_route(
                OPERATIONS_METRICS_PATH,
                operations_metrics_response,
                methods=["GET"],
                tags=["operations"],
                summary="Operational metrics",
            )

    FastAPI.__init__ = init_with_operations  # type: ignore[method-assign]
    setattr(FastAPI, _INSTALL_MARKER, True)
