from __future__ import annotations

import hmac
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from sqlalchemy import func, select

from app.db import session_scope
from app.models import Broadcast, GenerationTask, Payment
from app.services.admin_hardening import broadcast_runtime_metrics

_INSTALL_MARKER = "_stupidbot_operations_metrics_installed"


async def operations_metrics_response(request: Request) -> dict[str, Any]:
    context = request.app.state.context
    expected = str(context.settings.telegram_secret_token or "")
    supplied = str(request.headers.get("x-ops-token") or "")
    if not expected or not supplied or not hmac.compare_digest(supplied, expected):
        raise HTTPException(status_code=403, detail="Invalid operations token")

    async with session_scope(context.session_factory) as session:
        broadcast_rows = list(
            await session.execute(
                select(Broadcast.status, func.count())
                .group_by(Broadcast.status)
                .order_by(Broadcast.status)
            )
        )
        active_tasks = await session.scalar(
            select(func.count())
            .select_from(GenerationTask)
            .where(
                GenerationTask.status.in_(
                    ["submitted", "submitting", "waiting", "queuing", "generating"]
                )
            )
        )
        failed_tasks = await session.scalar(
            select(func.count())
            .select_from(GenerationTask)
            .where(GenerationTask.status == "fail")
        )
        pending_payments = await session.scalar(
            select(func.count())
            .select_from(Payment)
            .where(Payment.status.in_(["created", "manual_pending"]))
        )

    return {
        "status": "ok",
        "broadcast_runtime": broadcast_runtime_metrics(),
        "broadcasts": {str(status): int(count) for status, count in broadcast_rows},
        "generation_tasks": {
            "active": int(active_tasks or 0),
            "failed": int(failed_tasks or 0),
        },
        "payments": {"pending": int(pending_payments or 0)},
    }


def install_operations_metrics_route() -> None:
    if getattr(FastAPI, _INSTALL_MARKER, False):
        return
    original_init = FastAPI.__init__

    def init_with_metrics(self: FastAPI, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        if not any(getattr(route, "path", None) == "/ops/metrics" for route in self.routes):
            self.add_api_route(
                "/ops/metrics",
                operations_metrics_response,
                methods=["GET"],
                tags=["operations"],
                summary="Protected operational metrics",
            )

    FastAPI.__init__ = init_with_metrics  # type: ignore[method-assign]
    setattr(FastAPI, _INSTALL_MARKER, True)
