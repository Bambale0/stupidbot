from __future__ import annotations

from html import escape
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardMarkup,
    InputMediaPhoto,
    Message,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from app.context import AppContext
from app.db import session_scope
from app.models import GenerationTask, UploadedFile, User
from app.plugins.common import ensure_user_for_callback, ensure_user_for_message
from app.plugins.generation.plugin import (
    ImageFlow,
    _generation_limits_payload,
    _image_reference_items,
    _image_settings_keyboard,
    _image_settings_text,
    _max_image_references_from_limits,
    _repeat_image_state_payload,
    _submit_image_task,
    _task_is_image_generation,
)
from app.repositories import get_model, list_enabled_models
from app.services.generation_catalog import (
    DEFAULT_IMAGE_ASPECT_RATIO,
    DEFAULT_IMAGE_RESOLUTION,
)
from app.ui import add_navigation_buttons, model_price_text, navigation_keyboard

router = Router(name="references")
REFERENCE_LIBRARY_SCAN_LIMIT = 300
REFERENCE_LIBRARY_TASK_SCAN_LIMIT = 80
REFERENCE_LIBRARY_LIMIT = 100
REFERENCE_LIBRARY_ITEMS_KEY = "reference_library_items"
REFERENCE_LIBRARY_SELECTED_KEY = "reference_library_selected"
REFERENCE_LIBRARY_INDEX_KEY = "reference_library_index"


@router.message(Command("references"))
@router.message(F.text == "Мои референсы")
async def references_menu(message: Message, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    user = await ensure_user_for_message(message, context)
    await _send_reference_library(message, context, state, user.id)


@router.callback_query(F.data == "menu:references")
async def references_menu_callback(
    callback: CallbackQuery,
    context: AppContext,
    state: FSMContext,
) -> None:
    await state.clear()
    user = await ensure_user_for_callback(callback, context)
    if callback.message:
        await _send_reference_library(callback.message, context, state, user.id)
    await callback.answer()


@router.callback_query(F.data.startswith("refs:page:"))
async def show_reference_page(callback: CallbackQuery, state: FSMContext) -> None:
    index = _callback_index(callback.data, "refs:page:")
    items, selected, _ = await _reference_library_state(state)
    if index is None or not items:
        await callback.answer(
            "Библиотека референсов устарела. Откройте её заново.",
            show_alert=True,
        )
        return
    index %= len(items)
    await state.update_data(**{REFERENCE_LIBRARY_INDEX_KEY: index})
    await _render_reference_card(callback, items, selected, index)
    await callback.answer()


@router.callback_query(F.data.startswith("refs:toggle:"))
async def toggle_reference(callback: CallbackQuery, state: FSMContext) -> None:
    index = _callback_index(callback.data, "refs:toggle:")
    items, selected, _ = await _reference_library_state(state)
    if index is None or not items:
        await callback.answer(
            "Библиотека референсов устарела. Откройте её заново.",
            show_alert=True,
        )
        return
    index %= len(items)
    file_id = str(items[index]["telegram_file_id"])
    if file_id in selected:
        selected.remove(file_id)
        answer_text = "Референс снят"
    else:
        selected.append(file_id)
        answer_text = "Референс выбран"
    await state.update_data(
        **{
            REFERENCE_LIBRARY_SELECTED_KEY: selected,
            REFERENCE_LIBRARY_INDEX_KEY: index,
        }
    )
    await _render_reference_card(callback, items, selected, index)
    await callback.answer(answer_text)


@router.callback_query(F.data == "refs:clear")
async def clear_reference_selection(callback: CallbackQuery, state: FSMContext) -> None:
    items, _selected, index = await _reference_library_state(state)
    if not items:
        await callback.answer(
            "Библиотека референсов устарела. Откройте её заново.",
            show_alert=True,
        )
        return
    await state.update_data(**{REFERENCE_LIBRARY_SELECTED_KEY: []})
    await _render_reference_card(callback, items, [], index)
    await callback.answer("Выбор очищен")


@router.callback_query(F.data == "refs:apply")
async def apply_reference_selection(
    callback: CallbackQuery,
    context: AppContext,
    state: FSMContext,
) -> None:
    items, selected, index = await _reference_library_state(state)
    if not items or not selected:
        await callback.answer("Сначала выберите хотя бы одно фото", show_alert=True)
        return
    if callback.message:
        await _send_reference_model_picker(
            callback.message,
            context,
            selected_count=len(selected),
            back_index=index,
        )
    await callback.answer()


@router.callback_query(F.data.startswith("refs:model:"))
async def select_reference_model(
    callback: CallbackQuery,
    context: AppContext,
    state: FSMContext,
) -> None:
    model_code = str(callback.data or "").removeprefix("refs:model:").strip()
    user = await ensure_user_for_callback(callback, context)
    items, selected, _ = await _reference_library_state(state)
    selected_items = _selected_reference_items(items, selected)
    if not selected_items:
        await callback.answer("Сначала выберите референсы заново", show_alert=True)
        return

    async with session_scope(context.session_factory) as session:
        model = await get_model(session, model_code)
        fresh_user = await session.get(User, user.id)
    if not model or not model.is_enabled or model.category != "image":
        await callback.answer("Эта модель сейчас недоступна", show_alert=True)
        return
    if not fresh_user:
        await callback.answer("Пользователь не найден", show_alert=True)
        return

    limits = _generation_limits_payload(fresh_user, model)
    max_images = _max_image_references_from_limits(limits)
    references = selected_items[:max_images]
    first_reference = references[0]
    payload: dict[str, Any] = {
        "model_code": model.code,
        "prompt": "",
        "resolution": DEFAULT_IMAGE_RESOLUTION,
        "aspect_ratio": DEFAULT_IMAGE_ASPECT_RATIO,
        "image_file_id": first_reference["telegram_file_id"],
        "image_filename": first_reference.get("filename"),
        "image_mime_type": first_reference.get("mime_type"),
        "image_references": references,
        "image_limits": limits,
        "explicit_model_selected": True,
    }

    await state.clear()
    await state.set_state(ImageFlow.settings)
    await state.update_data(**payload)
    if callback.message:
        skipped = len(selected_items) - len(references)
        prefix = (
            f"Выбрано {len(references)} фото из библиотеки."
            if skipped <= 0
            else (
                f"Модель принимает максимум {max_images} фото. "
                f"Использую первые {len(references)} из выбранных."
            )
        )
        await callback.message.answer(
            f"{escape(prefix)}\n\n{_image_settings_text(payload)}",
            reply_markup=_image_settings_keyboard(payload),
        )
    await callback.answer("Референсы загружены")


@router.callback_query(F.data == "refs:noop")
async def reference_noop(callback: CallbackQuery) -> None:
    await callback.answer()


@router.callback_query(F.data.startswith("refs:use:"))
async def use_saved_reference(
    callback: CallbackQuery,
    context: AppContext,
    state: FSMContext,
) -> None:
    """Compatibility path for buttons sent by the former task-list UI."""

    task_id = _callback_task_id(callback.data, "refs:use:")
    if not task_id:
        await callback.answer("Референс не найден", show_alert=True)
        return
    user = await ensure_user_for_callback(callback, context)
    references, error = await _task_reference_items(
        context,
        user_id=user.id,
        task_id=task_id,
    )
    if not references:
        await callback.answer(error or "Референс недоступен", show_alert=True)
        return

    await state.clear()
    await state.update_data(
        **{
            REFERENCE_LIBRARY_ITEMS_KEY: references,
            REFERENCE_LIBRARY_SELECTED_KEY: [
                str(item["telegram_file_id"]) for item in references
            ],
            REFERENCE_LIBRARY_INDEX_KEY: 0,
        }
    )
    if callback.message:
        await _send_reference_model_picker(
            callback.message,
            context,
            selected_count=len(references),
            back_index=0,
        )
    await callback.answer("Референсы выбраны")


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
    await _load_generation_task_into_state(
        callback,
        context,
        state,
        task_id=task_id,
        success_text=(
            "Те же референсы и настройки загружены. "
            "Измените промпт или сразу нажмите «Запустить»."
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


async def _send_reference_library(
    message: Message,
    context: AppContext,
    state: FSMContext,
    user_id: int,
) -> None:
    async with session_scope(context.session_factory) as session:
        uploads = list(
            await session.scalars(
                select(UploadedFile)
                .where(
                    UploadedFile.user_id == user_id,
                    UploadedFile.file_type == "image",
                )
                .order_by(UploadedFile.created_at.desc(), UploadedFile.id.desc())
                .limit(REFERENCE_LIBRARY_SCAN_LIMIT)
            )
        )
        tasks = list(
            await session.scalars(
                select(GenerationTask)
                .where(GenerationTask.user_id == user_id)
                .order_by(GenerationTask.created_at.desc(), GenerationTask.id.desc())
                .limit(REFERENCE_LIBRARY_TASK_SCAN_LIMIT)
            )
        )
    items = collect_saved_references(
        uploads,
        tasks,
        limit=REFERENCE_LIBRARY_LIMIT,
    )
    if not items:
        await message.answer(
            "<b>Мои референсы</b>\n\n"
            "Здесь появятся загруженные фото. Отправьте референс при создании изображения, "
            "и потом его не придётся искать в галерее телефона.",
            reply_markup=navigation_keyboard(back_callback="menu:image"),
        )
        return

    await state.update_data(
        **{
            REFERENCE_LIBRARY_ITEMS_KEY: items,
            REFERENCE_LIBRARY_SELECTED_KEY: [],
            REFERENCE_LIBRARY_INDEX_KEY: 0,
        }
    )
    await message.answer_photo(
        photo=items[0]["telegram_file_id"],
        caption=_reference_library_caption(items, [], 0),
        reply_markup=_reference_library_keyboard(items, [], 0),
    )


async def _render_reference_card(
    callback: CallbackQuery,
    items: list[dict[str, Any]],
    selected: list[str],
    index: int,
) -> None:
    if not callback.message:
        return
    item = items[index]
    media = InputMediaPhoto(
        media=str(item["telegram_file_id"]),
        caption=_reference_library_caption(items, selected, index),
    )
    keyboard = _reference_library_keyboard(items, selected, index)
    try:
        await callback.message.edit_media(media=media, reply_markup=keyboard)
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        await callback.message.answer_photo(
            photo=item["telegram_file_id"],
            caption=_reference_library_caption(items, selected, index),
            reply_markup=keyboard,
        )


async def _send_reference_model_picker(
    message: Message,
    context: AppContext,
    *,
    selected_count: int,
    back_index: int,
) -> None:
    async with session_scope(context.session_factory) as session:
        models = await list_enabled_models(session, "image")
    if not models:
        await message.answer(
            "Сейчас нет доступных моделей изображений.",
            reply_markup=navigation_keyboard(back_callback=f"refs:page:{back_index}"),
        )
        return

    builder = InlineKeyboardBuilder()
    for model in models:
        builder.button(
            text=f"{model.title} · {model_price_text(model, short=True)}",
            callback_data=f"refs:model:{model.code}",
        )
    nav_count = add_navigation_buttons(
        builder,
        back_callback=f"refs:page:{back_index}",
    )
    builder.adjust(*([1] * len(models)), nav_count)
    await message.answer(
        f"Выбрано фото: <b>{selected_count}</b>\n\n"
        "Выберите модель. Старый промпт и настройки генерации не подставляются.",
        reply_markup=builder.as_markup(),
    )


async def _reference_library_state(
    state: FSMContext,
) -> tuple[list[dict[str, Any]], list[str], int]:
    data = await state.get_data()
    items = _image_reference_items(
        {"image_references": data.get(REFERENCE_LIBRARY_ITEMS_KEY)}
    )
    available_ids = {
        str(item["telegram_file_id"])
        for item in items
    }
    raw_selected = data.get(REFERENCE_LIBRARY_SELECTED_KEY)
    selected: list[str] = []
    if isinstance(raw_selected, list):
        for value in raw_selected:
            file_id = str(value or "").strip()
            if file_id and file_id in available_ids and file_id not in selected:
                selected.append(file_id)
    try:
        index = int(data.get(REFERENCE_LIBRARY_INDEX_KEY) or 0)
    except (TypeError, ValueError):
        index = 0
    if items:
        index %= len(items)
    else:
        index = 0
    return items, selected, index


def _reference_library_caption(
    items: list[dict[str, Any]],
    selected: list[str],
    index: int,
) -> str:
    selected_count = len(selected)
    return (
        "<b>Мои референсы</b>\n\n"
        f"Фото <b>{index + 1}/{len(items)}</b>\n"
        f"Выбрано: <b>{selected_count}</b>\n\n"
        "Листайте фотографии и отмечайте нужные. Затем нажмите "
        "«Использовать выбранные», выберите модель и отправьте новый промпт."
    )


def _reference_library_keyboard(
    items: list[dict[str, Any]],
    selected: list[str],
    index: int,
) -> InlineKeyboardMarkup:
    item = items[index]
    file_id = str(item["telegram_file_id"])
    selected_now = file_id in selected
    builder = InlineKeyboardBuilder()
    previous_index = (index - 1) % len(items)
    next_index = (index + 1) % len(items)
    builder.button(text="←", callback_data=f"refs:page:{previous_index}")
    builder.button(text=f"{index + 1}/{len(items)}", callback_data="refs:noop")
    builder.button(text="→", callback_data=f"refs:page:{next_index}")
    builder.button(
        text="✅ Выбрано" if selected_now else "Выбрать фото",
        callback_data=f"refs:toggle:{index}",
    )
    rows = [3, 1]
    if selected:
        builder.button(
            text=f"Использовать выбранные ({len(selected)})",
            callback_data="refs:apply",
        )
        builder.button(text="Снять выбор", callback_data="refs:clear")
        rows.extend([1, 1])
    nav_count = add_navigation_buttons(builder, back_callback="menu:image")
    rows.append(nav_count)
    builder.adjust(*rows)
    return builder.as_markup()


def collect_saved_references(
    uploads: list[UploadedFile],
    tasks: list[GenerationTask],
    *,
    limit: int = REFERENCE_LIBRARY_LIMIT,
) -> list[dict[str, Any]]:
    if limit <= 0:
        return []
    result: list[dict[str, Any]] = []
    seen: set[str] = set()

    def append_reference(item: dict[str, Any]) -> None:
        file_id = str(item.get("telegram_file_id") or "").strip()
        if not file_id or file_id in seen or len(result) >= limit:
            return
        seen.add(file_id)
        result.append(
            {
                "telegram_file_id": file_id,
                "filename": str(item.get("filename") or "image"),
                "mime_type": str(item.get("mime_type") or "image/jpeg"),
                "size": item.get("size"),
            }
        )

    for upload in uploads:
        append_reference(
            {
                "telegram_file_id": upload.telegram_file_id,
                "filename": upload.original_name,
                "mime_type": upload.mime_type,
                "size": upload.size_bytes,
            }
        )
        if len(result) >= limit:
            return result

    for task in tasks:
        if not _task_is_image_generation(task):
            continue
        payload = _repeat_image_state_payload(task)
        if not payload:
            continue
        for item in _image_reference_items(payload):
            append_reference(item)
            if len(result) >= limit:
                return result
    return result


def _selected_reference_items(
    items: list[dict[str, Any]],
    selected: list[str],
) -> list[dict[str, Any]]:
    by_file_id = {
        str(item["telegram_file_id"]): item
        for item in items
    }
    return [
        by_file_id[file_id]
        for file_id in selected
        if file_id in by_file_id
    ]


async def _task_reference_items(
    context: AppContext,
    *,
    user_id: int,
    task_id: int,
) -> tuple[list[dict[str, Any]] | None, str | None]:
    async with session_scope(context.session_factory) as session:
        task = await session.scalar(
            select(GenerationTask).where(
                GenerationTask.id == task_id,
                GenerationTask.user_id == user_id,
            )
        )
    if not task or not _task_is_image_generation(task):
        return None, "Можно использовать только свои референсы изображений"
    payload = _repeat_image_state_payload(task)
    references = _image_reference_items(payload or {})
    if not references:
        return None, "В этой генерации не сохранились референсы"
    return references, None


async def _load_generation_task_into_state(
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
    await callback.answer("Генерация загружена")


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
    """Backward-compatible helper retained for repeat-flow regressions."""

    if limit <= 0:
        return []
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
        if len(result) >= limit:
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


def _callback_index(value: str | None, prefix: str) -> int | None:
    if not value or not value.startswith(prefix):
        return None
    try:
        index = int(value.removeprefix(prefix))
    except ValueError:
        return None
    return index if index >= 0 else None


def _callback_task_id(value: str | None, prefix: str) -> int | None:
    if not value or not value.startswith(prefix):
        return None
    try:
        task_id = int(value.removeprefix(prefix))
    except ValueError:
        return None
    return task_id if task_id > 0 else None


def setup(dispatcher: Dispatcher, context: AppContext) -> None:
    del context
    dispatcher.include_router(router)
