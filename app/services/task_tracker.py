from __future__ import annotations

import asyncio
import json
import logging
from contextlib import suppress
from html import escape
from typing import Any

from aiogram import Bot
from aiogram.types import InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from app.context import AppContext
from app.db import session_scope
from app.models import GalleryItem, GenerationTask, User
from app.repositories import refund_task_credits

logger = logging.getLogger(__name__)

ACTIVE_STATES = {"submitted", "waiting", "queuing", "generating"}
FINAL_STATES = {"success", "fail"}


class TaskTracker:
    def __init__(self, context: AppContext, bot: Bot, interval: float = 15.0) -> None:
        self.context = context
        self.bot = bot
        self.interval = interval
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if not self._task:
            self._task = asyncio.create_task(self._run(), name="comet-task-tracker")

    async def stop(self) -> None:
        self._stop.set()
        if self._task:
            self._task.cancel()
            with suppress(asyncio.CancelledError, asyncio.TimeoutError, TimeoutError):
                await self._task

    async def _run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    await self.process_once()
                except Exception:
                    logger.exception("Task tracker iteration failed")
                with suppress(asyncio.TimeoutError, TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
        except (asyncio.CancelledError, asyncio.TimeoutError, TimeoutError):
            return

    async def process_once(self) -> None:
        async with session_scope(self.context.session_factory) as session:
            stmt = (
                select(GenerationTask)
                .where(GenerationTask.status.in_(ACTIVE_STATES), GenerationTask.provider_task_id.is_not(None))
                .order_by(GenerationTask.updated_at.asc())
                .limit(20)
            )
            tasks = list(await session.scalars(stmt))

        for task in tasks:
            await self._process_task(task.id)

    async def _process_task(self, local_task_id: int) -> None:
        async with session_scope(self.context.session_factory) as session:
            task = await session.get(GenerationTask, local_task_id)
            if not task or not task.provider_task_id or task.status not in ACTIVE_STATES:
                return
            input_payload = task.input_payload or {}
            provider = str(input_payload.get("provider") or "comet").lower()
            provider_family = str(input_payload.get("provider_family") or _infer_provider_family(task)).lower()
            if provider == "kie":
                data = await self.context.kie.query_task(task.provider_task_id)
            elif provider_family == "seedance":
                data = await self.context.comet.query_seedance_video_task(task.provider_task_id)
            else:
                data = await self.context.comet.query_kling_image_to_video_task(task.provider_task_id)
            state = _normalize_task_state(data.get("state") or data.get("task_status") or task.status)
            await session.refresh(task)
            if task.status in FINAL_STATES:
                return
            task.result_payload = data
            if state not in FINAL_STATES:
                task.status = state
                with suppress(Exception):
                    await self._update_status_message(task, state, data)
                return

            task.status = state
            if state == "success":
                urls = _extract_result_urls(data)
                task.result_urls = urls
                with suppress(Exception):
                    await self._update_status_message(task, "success", data)
                if urls:
                    session.add(
                        GalleryItem(
                            generation_task_id=task.id,
                            user_id=task.user_id,
                            title=f"Работа #{task.id}",
                            prompt=task.prompt,
                            media_url=urls[0],
                            media_type=_task_media_type(task),
                            model_code=task.model_code,
                            is_public=False,
                        )
                    )
                await session.flush()
                with suppress(Exception):
                    await self._notify_success(task, urls)
            else:
                task.error_message = str(data.get("failMsg") or data.get("error") or "Generation failed")
                with suppress(Exception):
                    await self._update_status_message(task, "fail", data)
                await refund_task_credits(session, task=task)
                with suppress(Exception):
                    await self._notify_failure(task)

    async def apply_callback_payload(self, payload: dict[str, Any]) -> bool:
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
        async with session_scope(self.context.session_factory) as session:
            task = await session.scalar(
                select(GenerationTask)
                .where(GenerationTask.provider_task_id == provider_task_id)
                .with_for_update()
            )
            if not task:
                return False
            if task.status in FINAL_STATES:
                return True
            data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
            state = _normalize_task_state(data.get("state") or data.get("task_status") or task.status)
            task.result_payload = data
            if state == "success":
                task.status = "success"
                task.result_urls = _extract_result_urls(data)
                with suppress(Exception):
                    await self._update_status_message(task, "success", data)
                if task.result_urls:
                    session.add(
                        GalleryItem(
                            generation_task_id=task.id,
                            user_id=task.user_id,
                            title=f"Работа #{task.id}",
                            prompt=task.prompt,
                            media_url=task.result_urls[0],
                            media_type=_task_media_type(task),
                            model_code=task.model_code,
                            is_public=False,
                        )
                    )
                with suppress(Exception):
                    await self._notify_success(task, task.result_urls)
            elif state == "fail":
                task.status = "fail"
                task.error_message = str(data.get("failMsg") or data.get("error") or "Generation failed")
                with suppress(Exception):
                    await self._update_status_message(task, "fail", data)
                await refund_task_credits(session, task=task)
                with suppress(Exception):
                    await self._notify_failure(task)
            else:
                task.status = state
        return True

    async def _notify_success(self, task: GenerationTask, urls: list[str]) -> None:
        if not task.chat_id:
            user = await self._load_user(task.user_id)
            chat_id = user.telegram_id if user else None
        else:
            chat_id = task.chat_id
        if not chat_id:
            return
        if not urls:
            await self.bot.send_message(chat_id, f"Генерация #{task.id} завершена, но файл результата не найден.")
            return
        await self.bot.send_message(chat_id, f"Генерация #{task.id} готова. Ниже превью и оригинал документом.")
        media_type = _task_media_type(task)
        for index, url in enumerate(urls[:5], start=1):
            with suppress(Exception):
                if media_type == "video":
                    await self.bot.send_video(
                        chat_id,
                        url,
                        caption=f"Превью #{index}",
                        reply_markup=_feed_publish_keyboard(task.id, media_type=media_type) if index == 1 else None,
                    )
                else:
                    await self.bot.send_photo(
                        chat_id,
                        url,
                        caption=f"Превью #{index}",
                        reply_markup=_feed_publish_keyboard(task.id, media_type=media_type) if index == 1 else None,
                    )
            with suppress(Exception):
                await self.bot.send_document(
                    chat_id,
                    url,
                    caption=f"Оригинал #{index}",
                )
                continue
            await self.bot.send_message(chat_id, f"Оригинал #{index}:\n{escape(str(url))}")

    async def _notify_failure(self, task: GenerationTask) -> None:
        chat_id = task.chat_id
        if not chat_id:
            user = await self._load_user(task.user_id)
            chat_id = user.telegram_id if user else None
        if chat_id:
            await self.bot.send_message(
                chat_id,
                f"Генерация #{task.id} завершилась ошибкой.\n"
                f"{escape(str(task.error_message or 'Причина не указана'))}\n"
                "Списанные кредиты возвращены.",
            )

    async def _update_status_message(
        self,
        task: GenerationTask,
        state: str,
        data: dict[str, Any],
    ) -> None:
        if not task.chat_id or not task.message_id:
            return
        progress = _progress_percent(state, data)
        text = _status_text_for_task(task.id, state, progress, data)
        await self.bot.edit_message_text(text, chat_id=task.chat_id, message_id=task.message_id)

    async def _load_user(self, user_id: int) -> User | None:
        async with session_scope(self.context.session_factory) as session:
            return await session.get(User, user_id)


def _infer_provider_family(task: GenerationTask) -> str:
    if task.model_code.startswith("seedance"):
        return "seedance"
    if task.model_code.startswith("kling"):
        return "kling"
    if "video" in task.model_code:
        return "video"
    return "image"


def _task_media_type(task: GenerationTask) -> str:
    provider = str((task.input_payload or {}).get("provider") or "")
    if "video" in task.model_code or provider == "kie-video":
        return "video"
    return "image"


def _extract_result_urls(data: dict[str, Any]) -> list[str]:
    task_result = data.get("task_result")
    if isinstance(task_result, dict):
        videos = task_result.get("videos")
        if isinstance(videos, list):
            urls = _extract_urls_from_list(videos)
            if urls:
                return urls
    result_json = data.get("resultJson") or data.get("result")
    if isinstance(result_json, str):
        with suppress(json.JSONDecodeError):
            result_json = json.loads(result_json)
    if isinstance(result_json, dict):
        for key in ("resultUrls", "urls", "images", "videos"):
            value = result_json.get(key)
            if isinstance(value, list):
                urls = _extract_urls_from_list(value)
                if urls:
                    return urls
            if isinstance(value, str):
                return [value]
        for key in ("video_url", "download_url", "url", "result_url", "output_url"):
            value = result_json.get(key)
            if isinstance(value, str) and value:
                return [value]
    for key in ("resultUrls", "urls"):
        value = data.get(key)
        if isinstance(value, list):
            return [str(item) for item in value if item]
    for key in ("video_url", "download_url", "url", "result_url", "output_url"):
        value = data.get(key)
        if isinstance(value, str) and value:
            return [value]
    return []


def _extract_urls_from_list(items: list[Any]) -> list[str]:
    urls: list[str] = []
    for item in items:
        if isinstance(item, str) and item:
            urls.append(item)
            continue
        if not isinstance(item, dict):
            continue
        for key in ("url", "video_url", "image_url", "download_url", "result_url", "output_url"):
            value = item.get(key)
            if isinstance(value, str) and value:
                urls.append(value)
                break
    return urls


def _feed_publish_keyboard(task_id: int, *, media_type: str = "image") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if media_type == "image":
        builder.button(text="Еще вариант (тот же реф)", callback_data=f"image:again:{task_id}")
    builder.button(text="В ленту", callback_data=f"feed:publish:confirm:{task_id}")
    builder.button(text="Главное меню", callback_data="menu:main")
    builder.adjust(*([1, 1, 1] if media_type == "image" else [1, 1]))
    return builder.as_markup()


def _normalize_task_state(raw_state: object) -> str:
    normalized = str(raw_state or "").strip().lower()
    return {
        "created": "submitted",
        "pending": "submitted",
        "submitted": "submitted",
        "queued": "waiting",
        "queue": "waiting",
        "waiting": "waiting",
        "running": "generating",
        "processing": "generating",
        "generating": "generating",
        "succeed": "success",
        "succeeded": "success",
        "success": "success",
        "completed": "success",
        "complete": "success",
        "failed": "fail",
        "fail": "fail",
        "error": "fail",
        "canceled": "fail",
        "cancelled": "fail",
    }.get(normalized, normalized)


def _progress_percent(state: str, data: dict[str, Any]) -> int:
    raw_progress = data.get("progress")
    if isinstance(raw_progress, int | float):
        if raw_progress <= 1:
            return int(raw_progress * 100)
        return int(raw_progress)
    if isinstance(raw_progress, str):
        cleaned = raw_progress.strip().removesuffix("%")
        with suppress(ValueError):
            value = float(cleaned)
            if value <= 1:
                return int(value * 100)
            return int(value)
    defaults = {
        "submitted": 65,
        "waiting": 70,
        "queuing": 75,
        "generating": 85,
        "success": 100,
        "fail": 100,
    }
    return defaults.get(state, 70)


def _status_text_for_task(
    task_id: int,
    state: str,
    percent: int,
    data: dict[str, Any],
) -> str:
    titles = {
        "submitted": "Генерация отправлена",
        "waiting": "Ожидаю обработку",
        "queuing": "Генерация в очереди",
        "generating": "Создаю результат",
        "success": "Готово",
        "fail": "Ошибка генерации",
    }
    title = titles.get(state, f"Статус: {state}")
    bar = _progress_bar(percent)
    text = f"Генерация #{task_id}\n{title}\n{bar} {max(0, min(100, percent))}%"
    if state in {"waiting", "queuing", "generating"}:
        text += "\n\nМожно закрыть Telegram, результат придет сюда автоматически."
    if state == "success":
        text += "\n\nРезультат готов. Отправляю файл ниже."
    if state == "fail":
        reason = data.get("failMsg") or data.get("error") or "Причина не указана"
        text += f"\n\n{escape(str(reason))}\nСписанные кредиты возвращены."
    return text


def _progress_bar(percent: int) -> str:
    percent = max(0, min(100, percent))
    filled = max(0, min(10, round(percent / 10)))
    return "▰" * filled + "▱" * (10 - filled)
