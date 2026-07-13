from __future__ import annotations

from html import escape
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from app.context import AppContext
from app.db import session_scope
from app.models import GenerationTask, User
from app.plugins.common import ensure_user_for_callback, ensure_user_for_message
from app.plugins.generation.plugin import (
    ImageFlow,
    _generation_limits_payload,
    _image_settings_keyboard,
    _image_settings_text,
    _repeat_image_state_payload,
    _submit_image_task,
    _task_is_image_generation,
)
from app.repositories import get_model
from app.ui import add_navigation_buttons, navigation_keyboard

router = Router(name="references")
REFERENCE_LIBRARY_SCAN_LIMIT = 80
REFERENCE_LIBRARY_LIMIT = 12


@router.message(Command("references"))
@router.message(F.text == "Мои референсы")
async def references_menu(message: Message, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    user = await ensure_user_for_message(message, context)
    await _send_reference_library(message, context, user.id)


@router.callback_query(F.data == "menu:references")
async def references_menu_callback(
    callback: CallbackQuery,
    context: AppContext,
    state: FSMContext,
) -> None:
    await state.clear()
    user = await ensure_user_for_callback(callback, context)
    if callback.message:
        await _send_reference_library(callback.message, context, user.id)
    await callback.answer()


@router.callback_query(F.data.startswith("refs:use:"))
async def use_saved_reference(
    callback: CallbackQuery,
    context: AppContext,
    state: FSMContext,
) -> None:
    task_id = _callback_task_id(callback.data, "refs:use:")
    if not task_id:
        await callback.answer("Референс не найден", show_alert=True)
        return
    await _load_reference_task_into_state(
        callback,
        context,
        state,
        task_id=task_id,
        success_text="Сохранённые референсы загружены.",
    )


@router.callback_query(F.data.startswith("image:again:"))
async def repeat_image_generation(
    callback: CallbackQuery,
    context: AppContext,
    state: FSMContext,
) -> None:
    task_id = _callback_task_id(callback.data, "image:again:")
    if not task_id:
        await callback.answer("Генерация не найдена", show_alert=True)
        return
    await _load_reference_task_into_state(
        callback,
        context,
        state,
        task_id=task_id,
        success_text=(
            "Тот же референс и настройки загружены. "
            "Можно изменить промпт или сразу нажать «Запустить»."
        ),
    )


@router.callback_query(ImageFlow.settings, F.data == "image:submit")
async def submit_image_from_settings(
    callback: CallbackQuery,
    context: AppContext,
    state: FSMContext,
    bot: Bot,
) -> None:
    await _submit_image_task(callback, context, state, bot)


async def _send_reference_library(message: Message, context: AppContext, user_id: int) -> None:
    async with session_scope(context.session_factory) as session:
        rows = list(
            await session.scalars(
                select(GenerationTask)
                .where(GenerationTask.user_id == user_id)
                .order_by(GenerationTask.created_at.desc(), GenerationTask.id.desc())
                .limit(REFERENCE_LIBRARY_SCAN_LIMIT)
            )
        )
    tasks = collect_reference_tasks(rows, limit=REFERENCE_LIBRARY_LIMIT)
    if not tasks:
        await message.answer(
            "Сохранённых референсов пока нет.\n\n"
            "После первой генерации фото появятся здесь автоматически, "
            "и отправлять их заново не придётся.",
            reply_markup=navigation_keyboard(back_callback="menu:more"),
        )
        return

    builder = InlineKeyboardBuilder()
    for task in tasks:
        builder.button(
            text=_reference_button_text(task),
            callback_data=f"refs:use:{task.id}",
        )
    nav_count = add_navigation_buttons(builder, back_callback="menu:more")
    builder.adjust(*([1] * len(tasks)), nav_count)
    await message.answer(
        "<b>Мои референсы</b>\n\n"
        "Здесь сохранены последние уникальные наборы фото из ваших генераций. "
        "Выберите набор — он сразу откроется в генераторе вместе с прошлым промптом и настройками.",
        reply_markup=builder.as_markup(),
    )


async def _load_reference_task_into_state(
    callback: CallbackQuery,
    context: AppContext,
    state: FSMContext,
    *,
    task_id: int,
    success_text: str,
) -> None:
    user = await ensure_user_for_callback(callback, context)
    payload, error = await _reference_state_payload(
        context,
        user_id=user.id,
        task_id=task_id,
    )
    if not payload:
        await callback.answer(error or "Референс недоступен", show_alert=True)
        return

    await state.clear()
    await state.set_state(ImageFlow.settings)
    await state.update_data(**payload)
    if callback.message:
        await callback.message.answer(
            f"{escape(success_text)}\n\n{_image_settings_text(payload)}",
            reply_markup=_image_settings_keyboard(payload),
        )
    await callback.answer("Референсы загружены")


async def _reference_state_payload(
    context: AppContext,
    *,
    user_id: int,
    task_id: int,
) -> tuple[dict[str, Any] | None, str | None]:
    async with session_scope(context.session_factory) as session:
        task = await session.scalar(
            select(GenerationTask).where(
                GenerationTask.id == task_id,
                GenerationTask.user_id == user_id,
            )
        )
        if not task or not _task_is_image_generation(task):
            return None, "Можно повторять только свои генерации изображений"
        model = await get_model(session, task.model_code)
        user = await session.get(User, user_id)
        if not model or not model.is_enabled or model.category != "image":
            return None, "Эта модель сейчас недоступна"
        if not user:
            return None, "Пользователь не найден"

        payload = _repeat_image_state_payload(task)
        if not payload:
            return None, "В этой генерации не сохранились референсы"

        limits = _generation_limits_payload(user, model)
        max_images = max(1, int(limits.get("max_images") or 1))
        references = list(payload.get("image_references") or [])[:max_images]
        if not references:
            return None, "В этой генерации не сохранились референсы"
        first_reference = references[0]
        payload.update(
            {
                "image_references": references,
                "image_file_id": first_reference.get("telegram_file_id"),
                "image_filename": first_reference.get("filename"),
                "image_mime_type": first_reference.get("mime_type"),
                "image_limits": limits,
                "explicit_model_selected": True,
                "reused_from_task_id": task.id,
            }
        )
        preserve_reference_origin(payload, task)
        return payload, None


def collect_reference_tasks(
    tasks: list[GenerationTask],
    *,
    limit: int = REFERENCE_LIBRARY_LIMIT,
) -> list[GenerationTask]:
    result: list[GenerationTask] = []
    signatures: set[tuple[str, ...]] = set()
    for task in tasks:
        if not _task_is_image_generation(task):
            continue
        signature = reference_signature(task)
        if not signature or signature in signatures:
            continue
        signatures.add(signature)
        result.append(task)
        if len(result) >= max(1, limit):
            break
    return result


def reference_signature(task: GenerationTask) -> tuple[str, ...]:
    payload = _repeat_image_state_payload(task)
    if not payload:
        return ()
    return tuple(
        str(item.get("telegram_file_id") or "")
        for item in payload.get("image_references", [])
        if str(item.get("telegram_file_id") or "").strip()
    )


def preserve_reference_origin(payload: dict[str, Any], task: GenerationTask) -> dict[str, Any]:
    source_task_id = int(task.source_feed_task_id or 0)
    if source_task_id > 0:
        payload["source_feed_task_id"] = source_task_id
    return payload


def _reference_button_text(task: GenerationTask) -> str:
    count = len(reference_signature(task))
    prompt = " ".join(str(task.prompt or "").split())
    prompt = f" · {prompt[:32]}" if prompt else ""
    return f"#{task.id} · {count} фото{prompt}"


def _callback_task_id(value: str | None, prefix: str) -> int | None:
    if not value or not value.startswith(prefix):
        return None
    try:
        task_id = int(value.removeprefix(prefix))
    except ValueError:
        return None
    return task_id if task_id > 0 else None


def setup(dispatcher: Dispatcher, context: AppContext) -> None:
    dispatcher.include_router(router)
