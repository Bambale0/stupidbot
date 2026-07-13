from __future__ import annotations

from datetime import timedelta
from typing import Any

from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GalleryItem, GenerationModel, GenerationTask, ProviderCostEntry
from app.services.financial_credits import now_utc, positive_int, refund_locked_task
from app.services.financial_settings import photo_credit_value_kopecks, video_credit_value_kopecks

FINAL_TASK_STATES = {"success", "fail"}
ACTIVE_ORPHAN_STATES = {"submitting", "generating"}


async def finalize_generation_task(
    session: AsyncSession,
    *,
    task_id: int,
    status: str,
    result_payload: dict[str, Any] | None = None,
    result_urls: list[str] | None = None,
    error_message: str | None = None,
) -> tuple[GenerationTask | None, bool]:
    if status not in FINAL_TASK_STATES:
        raise ValueError(f"unsupported final status: {status}")
    task = await session.get(GenerationTask, task_id, with_for_update=True)
    if not task:
        return None, False
    if task.status in FINAL_TASK_STATES:
        return task, False
    task.status = status
    task.finalized_at = now_utc()
    if result_payload is not None:
        task.result_payload = result_payload
    if result_urls is not None:
        task.result_urls = result_urls
    if error_message is not None:
        task.error_message = error_message
    if status == "fail":
        await refund_locked_task(session, task)
    elif task.result_urls:
        exists = await session.scalar(
            select(GalleryItem.id).where(GalleryItem.generation_task_id == task.id).limit(1)
        )
        if not exists:
            media_type = "video" if "video" in task.model_code or task.model_code.startswith("kling") else "image"
            session.add(
                GalleryItem(
                    generation_task_id=task.id,
                    user_id=task.user_id,
                    title=f"Работа #{task.id}",
                    prompt=task.prompt,
                    media_url=str(task.result_urls[0]),
                    media_type=media_type,
                    model_code=task.model_code,
                    is_public=False,
                )
            )
    await session.flush()
    return task, True


def _reported_cost(payload: Any) -> int | None:
    if not isinstance(payload, dict):
        return None
    for key in ("provider_cost_kopecks", "cost_kopecks", "billing_cost_kopecks"):
        if key in payload:
            return positive_int(payload[key])
    for key in ("billing", "usage", "metadata", "cost"):
        found = _reported_cost(payload.get(key))
        if found is not None:
            return found
    return None


async def record_task_financials(
    session: AsyncSession, *, task_id: int, settings: Any, provider_payload: dict[str, Any] | None = None
) -> GenerationTask | None:
    task = await session.get(GenerationTask, task_id, with_for_update=True)
    if not task or task.financials_calculated_at:
        return task
    model = await session.scalar(select(GenerationModel).where(GenerationModel.code == task.model_code))
    config = dict(model.config or {}) if model else {}
    input_payload = dict(task.input_payload or {})
    actual = _reported_cost(provider_payload or task.result_payload or {})
    fallback = bool(input_payload.get("fallback_from")) or input_payload.get("provider") == "kie"
    prefix = "fallback_" if fallback else ""
    seconds = positive_int(input_payload.get("billable_seconds") or input_payload.get("duration")) or 1
    per_second = positive_int(config.get(f"{prefix}provider_cost_kopecks_per_second"))
    configured = per_second * seconds if per_second else positive_int(config.get(f"{prefix}provider_cost_kopecks"))
    cost = configured if actual is None else actual
    credit_value = 0
    if model and model.category == "image":
        credit_value = photo_credit_value_kopecks(settings)
    elif model and model.category == "video":
        credit_value = video_credit_value_kopecks(settings)
    revenue = positive_int(task.cost_credits) * credit_value
    task.provider_cost_kopecks = cost
    task.estimated_revenue_kopecks = revenue
    task.estimated_margin_kopecks = revenue - cost
    task.financials_calculated_at = now_utc()
    exists = await session.scalar(select(ProviderCostEntry.id).where(ProviderCostEntry.generation_task_id == task.id))
    if not exists:
        session.add(
            ProviderCostEntry(
                generation_task_id=task.id,
                provider=str(input_payload.get("provider") or "unknown"),
                provider_model=str(input_payload.get("provider_model") or "") or None,
                cost_kopecks=cost,
                estimated_revenue_kopecks=revenue,
                estimated_margin_kopecks=revenue - cost,
                units=seconds if per_second else 1,
                metadata_json={"model_code": task.model_code, "actual_cost_reported": actual is not None},
            )
        )
    await session.flush()
    return task


async def reconcile_orphan_tasks(
    session: AsyncSession, *, timeout_seconds: int, limit: int = 50
) -> list[GenerationTask]:
    timeout = max(60, int(timeout_seconds or 0))
    now = now_utc()
    ids = list(
        await session.scalars(
            select(GenerationTask.id)
            .where(
                GenerationTask.provider_task_id.is_(None),
                or_(
                    and_(GenerationTask.status == "submitting", GenerationTask.updated_at < now - timedelta(seconds=timeout)),
                    and_(GenerationTask.status == "generating", GenerationTask.updated_at < now - timedelta(seconds=max(timeout * 24, 21600))),
                ),
            )
            .order_by(GenerationTask.updated_at)
            .with_for_update(skip_locked=True)
            .limit(max(1, min(limit, 200)))
        )
    )
    result: list[GenerationTask] = []
    for task_id in ids:
        task, changed = await finalize_generation_task(
            session,
            task_id=task_id,
            status="fail",
            result_payload={"reason": "orphan_submission_timeout"},
            error_message="Провайдер не подтвердил запуск задачи вовремя.",
        )
        if task and changed:
            result.append(task)
    return result
