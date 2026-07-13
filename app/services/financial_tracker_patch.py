from __future__ import annotations

from contextlib import suppress
from typing import Any

from sqlalchemy import select

from app.db import session_scope
from app.models import GenerationTask
from app.services.financial_settings import orphan_task_timeout_seconds
from app.services.financial_tasks import (
    FINAL_TASK_STATES,
    finalize_generation_task,
    reconcile_orphan_tasks,
    record_task_financials,
)


def install_tracker_patches() -> None:
    from app.services import task_tracker

    async def process_once(self: Any) -> None:
        async with session_scope(self.context.session_factory) as session:
            ids = list(
                await session.scalars(
                    select(GenerationTask.id)
                    .where(
                        GenerationTask.status.in_(task_tracker.ACTIVE_STATES),
                        GenerationTask.provider_task_id.is_not(None),
                    )
                    .order_by(GenerationTask.updated_at.asc())
                    .limit(20)
                )
            )
        for task_id in ids:
            await _process_task(self, task_tracker, task_id)

        async with session_scope(self.context.session_factory) as session:
            orphaned = await reconcile_orphan_tasks(
                session,
                timeout_seconds=orphan_task_timeout_seconds(self.context.settings),
            )
        for task in orphaned:
            with suppress(Exception):
                await self._update_status_message(
                    task,
                    "fail",
                    {"error": "Провайдер не подтвердил запуск задачи вовремя."},
                )
            with suppress(Exception):
                await self._notify_failure(task)

        async with session_scope(self.context.session_factory) as session:
            final_ids = list(
                await session.scalars(
                    select(GenerationTask.id)
                    .where(
                        GenerationTask.status.in_(FINAL_TASK_STATES),
                        GenerationTask.financials_calculated_at.is_(None),
                    )
                    .order_by(GenerationTask.updated_at.asc())
                    .limit(50)
                )
            )
            for final_id in final_ids:
                await record_task_financials(
                    session,
                    task_id=final_id,
                    settings=self.context.settings,
                )

    async def apply_callback_payload(self: Any, payload: dict[str, Any]) -> bool:
        payload_data = payload.get("data") if isinstance(payload.get("data"), dict) else {}
        provider_task_id = str(
            payload.get("taskId")
            or payload.get("task_id")
            or payload_data.get("taskId")
            or payload_data.get("task_id")
            or ""
        )
        if not provider_task_id:
            return False
        data = payload_data if payload_data else payload
        state = task_tracker._normalize_task_state(
            data.get("state") or data.get("task_status") or "submitted"
        )
        async with session_scope(self.context.session_factory) as session:
            task_id = await session.scalar(
                select(GenerationTask.id).where(
                    GenerationTask.provider_task_id == provider_task_id
                )
            )
            if not task_id:
                return False
            if state in FINAL_TASK_STATES:
                urls = task_tracker._extract_result_urls(data) if state == "success" else None
                error = None if state == "success" else "Провайдер завершил генерацию с ошибкой."
                task, changed = await finalize_generation_task(
                    session,
                    task_id=task_id,
                    status=state,
                    result_payload=data,
                    result_urls=urls,
                    error_message=error,
                )
                if task and changed:
                    await record_task_financials(
                        session,
                        task_id=task.id,
                        settings=self.context.settings,
                        provider_payload=data,
                    )
            else:
                task = await session.get(GenerationTask, task_id, with_for_update=True)
                changed = bool(task and task.status not in FINAL_TASK_STATES)
                if changed:
                    task.status = state
                    task.result_payload = data
        if not task:
            return False
        if not changed:
            return True
        with suppress(Exception):
            await self._update_status_message(task, state, data)
        if state == "success":
            with suppress(Exception):
                await self._notify_success(task, list(task.result_urls or []))
        elif state == "fail":
            with suppress(Exception):
                await self._notify_failure(task)
        return True

    task_tracker.TaskTracker.process_once = process_once
    task_tracker.TaskTracker.apply_callback_payload = apply_callback_payload


async def _process_task(self: Any, task_tracker: Any, task_id: int) -> None:
    async with session_scope(self.context.session_factory) as session:
        task = await session.get(GenerationTask, task_id)
        if not task or not task.provider_task_id or task.status not in task_tracker.ACTIVE_STATES:
            return
        provider_task_id = task.provider_task_id
        payload = dict(task.input_payload or {})
        provider = str(payload.get("provider") or "comet").lower()
        family = str(payload.get("provider_family") or task_tracker._infer_provider_family(task)).lower()

    if provider == "kie":
        data = await self.context.kie.query_task(provider_task_id)
    elif family == "seedance":
        data = await self.context.comet.query_seedance_video_task(provider_task_id)
    else:
        data = await self.context.comet.query_kling_image_to_video_task(provider_task_id)
    state = task_tracker._normalize_task_state(
        data.get("state") or data.get("task_status") or "submitted"
    )

    async with session_scope(self.context.session_factory) as session:
        if state in FINAL_TASK_STATES:
            urls = task_tracker._extract_result_urls(data) if state == "success" else None
            error = None if state == "success" else "Провайдер завершил генерацию с ошибкой."
            task, changed = await finalize_generation_task(
                session,
                task_id=task_id,
                status=state,
                result_payload=data,
                result_urls=urls,
                error_message=error,
            )
            if task and changed:
                await record_task_financials(
                    session,
                    task_id=task.id,
                    settings=self.context.settings,
                    provider_payload=data,
                )
        else:
            task = await session.get(GenerationTask, task_id, with_for_update=True)
            changed = bool(task and task.status not in FINAL_TASK_STATES)
            if changed:
                task.status = state
                task.result_payload = data
    if not task or not changed:
        return
    with suppress(Exception):
        await self._update_status_message(task, state, data)
    if state == "success":
        with suppress(Exception):
            await self._notify_success(task, list(task.result_urls or []))
    elif state == "fail":
        with suppress(Exception):
            await self._notify_failure(task)
