from __future__ import annotations

import base64
import asyncio
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from html import escape
from io import BytesIO
import json
import logging
import math
import tempfile
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import BufferedInputFile, CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from PIL import Image, ImageOps

from app.context import AppContext
from app.db import session_scope
from app.models import GalleryItem, GenerationTask, UploadedFile, User
from app.plugins.common import ensure_user_for_callback, ensure_user_for_message
from app.repositories import (
    get_model,
    list_enabled_models,
    model_credit_type,
    refund_task_credits,
    spend_user_credits,
    user_credit_balance,
    user_generates_for_free,
    user_has_unlimited,
)
from app.services.comet import (
    CometGeneratedImage,
    CometImageReference,
    CometImageResult,
)
from app.services.generation_catalog import (
    DEFAULT_IMAGE_ASPECT_RATIO,
    DEFAULT_IMAGE_RESOLUTION,
    DEFAULT_MINI_APP_IMAGE_MODEL,
    DEFAULT_MINI_APP_VIDEO_MODEL,
    IMAGE_ASPECT_RATIOS,
    IMAGE_RESOLUTIONS,
    MINI_APP_IMAGE_MODELS,
    MINI_APP_VIDEO_MODELS,
    normalize_image_aspect_ratio,
    normalize_image_resolution,
)
from app.services.kie import KieUploadReference
from app.ui import (
    add_navigation_buttons,
    model_keyboard,
    navigation_keyboard,
    options_keyboard,
)

router = Router(name="generation")
logger = logging.getLogger(__name__)
IMAGE_REFERENCE_LOCKS: dict[int, asyncio.Lock] = {}
IMAGE_REFERENCE_ALBUMS: dict[str, dict[str, Any]] = {}
IMAGE_REFERENCE_ALBUM_DELAY = 0.9

VIDEO_MODES = ["std", "pro"]
MOTION_CONTROL_MODE = "720p"
MOTION_CONTROL_CHARACTER_ORIENTATION = "video"
MOTION_CONTROL_MIN_SECONDS = 3
MOTION_CONTROL_MAX_SECONDS = 30
MOTION_CONTROL_IMAGE_ORIENTATION_MAX_SECONDS = 10
MOTION_CONTROL_IMAGE_MAX_BYTES = 10_000_000
MOTION_CONTROL_VIDEO_MAX_BYTES = 100_000_000
MOTION_CONTROL_IMAGE_MIME_TYPES = {"image/jpeg", "image/jpg", "image/png"}
MOTION_CONTROL_IMAGE_EXTENSION_MIME_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}
MOTION_CONTROL_VIDEO_MIME_TYPES = {"video/mp4", "video/quicktime", "video/x-matroska"}
MOTION_CONTROL_VIDEO_EXTENSION_MIME_TYPES = {
    ".mp4": "video/mp4",
    ".mov": "video/quicktime",
    ".qt": "video/quicktime",
    ".mkv": "video/x-matroska",
}
VIDEO_DURATIONS = ["5", "10"]
VIDEO_ASPECT_RATIOS = ["16:9", "9:16", "1:1"]
DEFAULT_VIDEO_ASPECT_RATIO = "16:9"
DEFAULT_VIDEO_RESOLUTION = "720p"
TELEGRAM_PHOTO_MAX_BYTES = 9_500_000
TELEGRAM_PREVIEW_VARIANTS = ((2048, 88), (1600, 84), (1280, 80), (1024, 76))


@dataclass(slots=True)
class SentImageFiles:
    preview_file_ids: list[str]
    source_file_ids: list[str]


class ImageFlow(StatesGroup):
    reference_prompt = State()
    settings = State()


class MotionFlow(StatesGroup):
    image = State()
    prompt = State()
    mode = State()
    duration = State()
    motion_video = State()


@router.message(F.web_app_data)
async def receive_mini_app_brief(message: Message, context: AppContext, state: FSMContext) -> None:
    await ensure_user_for_message(message, context)
    raw_data = message.web_app_data.data if message.web_app_data else ""
    try:
        payload = json.loads(raw_data)
    except json.JSONDecodeError:
        await message.answer("Не получилось прочитать бриф из мини-аппа.", reply_markup=navigation_keyboard())
        return

    if not isinstance(payload, dict) or payload.get("source") != "pink_lab":
        await message.answer("Бриф из мини-аппа не распознан.", reply_markup=navigation_keyboard())
        return

    prompt = _clean_mini_app_text(payload.get("prompt"), 1800)
    if not prompt:
        await message.answer("BANANA прислал пустой prompt.", reply_markup=navigation_keyboard())
        return

    await state.clear()
    kind = str(payload.get("kind") or "image")
    if kind == "motion":
        model_code = str(payload.get("model_code") or DEFAULT_MINI_APP_VIDEO_MODEL)
        if model_code not in MINI_APP_VIDEO_MODELS:
            model_code = DEFAULT_MINI_APP_VIDEO_MODEL
        source_feed_task_id = _mini_app_source_feed_task_id(payload)
        await state.set_state(MotionFlow.image)
        await state.update_data(
            model_code=model_code,
            prompt=prompt,
            aspect_ratio=_normalize_video_aspect(payload.get("aspect_ratio")),
            **({"source_feed_task_id": source_feed_task_id} if source_feed_task_id else {}),
        )
        await message.answer(
            _mini_app_brief_text(
                _clean_mini_app_text(payload.get("model_title"), 80) or "AI Video",
                prompt,
                "Отправьте изображение персонажа: JPEG, PNG или JPG до 10 MB.",
            ),
            reply_markup=navigation_keyboard(back_callback="menu:motion"),
        )
        return

    model_code = str(payload.get("model_code") or DEFAULT_MINI_APP_IMAGE_MODEL)
    if model_code not in MINI_APP_IMAGE_MODELS:
        model_code = DEFAULT_MINI_APP_IMAGE_MODEL
    source_feed_task_id = _mini_app_source_feed_task_id(payload)
    await state.set_state(ImageFlow.reference_prompt)
    await state.update_data(
        model_code=model_code,
        prompt=prompt,
        resolution=DEFAULT_IMAGE_RESOLUTION,
        aspect_ratio=_normalize_mini_app_aspect(payload.get("aspect_ratio")),
        **({"source_feed_task_id": source_feed_task_id} if source_feed_task_id else {}),
    )
    await message.answer(
        _mini_app_brief_text(
            "Banana",
            prompt,
            "Отправьте фото-референс. После загрузки я покажу параметры и кнопку запуска.",
        ),
        reply_markup=navigation_keyboard(back_callback="menu:image"),
    )


@router.message(F.text == "Nano Banana")
@router.message(F.text == "Banana")
@router.message(Command("image"))
async def image_menu(message: Message, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    await ensure_user_for_message(message, context)
    await _send_image_request_screen(message, context, state)


@router.callback_query(F.data == "menu:image")
async def image_menu_callback(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    await ensure_user_for_callback(callback, context)
    if callback.message:
        await _send_image_request_screen(callback.message, context, state)
    await callback.answer()


@router.message(F.text == "Kling Video")
@router.message(F.text == "AI Video")
@router.message(Command("motion"))
async def motion_menu(message: Message, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    await ensure_user_for_message(message, context)
    await _send_model_menu(message, context, "video", "Выберите модель видео:")


@router.callback_query(F.data == "menu:motion")
async def motion_menu_callback(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    await ensure_user_for_callback(callback, context)
    if callback.message:
        await _send_model_menu(callback.message, context, "video", "Выберите модель видео:")
    await callback.answer()


async def _send_model_menu(
    message: Message,
    context: AppContext,
    category: str,
    title: str,
) -> None:
    async with session_scope(context.session_factory) as session:
        models = await list_enabled_models(session, category)
    if not models:
        await message.answer("Сейчас нет включенных моделей в этом разделе.", reply_markup=navigation_keyboard())
        return
    await message.answer(title, reply_markup=model_keyboard(models))


async def _default_image_model_code(context: AppContext) -> str | None:
    async with session_scope(context.session_factory) as session:
        models = await list_enabled_models(session, "image")
    if not models:
        return None
    for model in models:
        if model.code == DEFAULT_MINI_APP_IMAGE_MODEL:
            return model.code
    return models[0].code


async def _send_image_request_screen(
    message: Message,
    context: AppContext,
    state: FSMContext,
) -> None:
    data = await state.get_data()
    model_code = str(data.get("model_code") or "").strip()
    if not model_code:
        model_code = await _default_image_model_code(context) or DEFAULT_MINI_APP_IMAGE_MODEL
        await state.update_data(
            model_code=model_code,
            resolution=DEFAULT_IMAGE_RESOLUTION,
            aspect_ratio=DEFAULT_IMAGE_ASPECT_RATIO,
        )
    async with session_scope(context.session_factory) as session:
        model = await get_model(session, model_code)
    title = str(getattr(model, "title", "Banana 2") or "Banana 2")
    max_images = _max_image_references_from_config(getattr(model, "config", None)) if model else 15
    await state.set_state(ImageFlow.reference_prompt)
    await message.answer(
        f"Выбрана модель: {escape(title)}. Можно отправить до {_references_count_text(max_images)}.\n"
        "Пришлите фото-референсы, затем я покажу параметры и кнопку запуска.",
        reply_markup=navigation_keyboard(),
    )


@router.message(StateFilter(None), F.photo)
@router.message(
    StateFilter(None),
    lambda message: bool(message.document and (message.document.mime_type or "").startswith("image/")),
)
async def receive_quick_image_reference(
    message: Message,
    context: AppContext,
    state: FSMContext,
) -> None:
    if not _extract_image_file(message)[0]:
        return
    await state.set_state(ImageFlow.reference_prompt)
    await state.update_data(
        model_code=await _default_image_model_code(context),
        resolution=DEFAULT_IMAGE_RESOLUTION,
        aspect_ratio=DEFAULT_IMAGE_ASPECT_RATIO,
    )
    await receive_image_reference(message, context, state)


def _clean_mini_app_text(value: Any, limit: int) -> str:
    if not isinstance(value, str):
        return ""
    return value.replace("\x00", "").strip()[:limit]


def _normalize_mini_app_aspect(value: Any) -> str:
    return normalize_image_aspect_ratio(value)


def _mini_app_source_feed_task_id(payload: dict[str, Any]) -> int | None:
    try:
        task_id = int(payload.get("source_feed_task_id") or 0)
    except (TypeError, ValueError):
        return None
    return task_id if task_id > 0 else None


def _mini_app_brief_text(title: str, prompt: str, next_step: str) -> str:
    prompt_text = escape(_shorten(prompt, 1200))
    return (
        f"BANANA сохранил бриф для <b>{escape(title)}</b>.\n\n"
        f"Промпт:\n{prompt_text}\n\n"
        f"{escape(next_step)}"
    )


@router.callback_query(F.data.startswith("gen:model:"))
async def select_model(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    user = await ensure_user_for_callback(callback, context)
    code = callback.data.removeprefix("gen:model:")
    async with session_scope(context.session_factory) as session:
        model = await get_model(session, code)
    if not model or not model.is_enabled:
        await callback.answer("Модель недоступна", show_alert=True)
        return

    await state.clear()
    if model.category == "image":
        await state.update_data(model_code=model.code, user_id=user.id, explicit_model_selected=True)
        if callback.message:
            await _send_image_request_screen(callback.message, context, state)
    else:
        await state.set_state(MotionFlow.image)
        await state.update_data(model_code=model.code, user_id=user.id, explicit_model_selected=True)
        if callback.message:
            await callback.message.answer(
                "Отправьте изображение-референс: JPEG, PNG или JPG до 10 MB.",
                reply_markup=navigation_keyboard(back_callback="menu:motion"),
            )
    await callback.answer()


@router.message(ImageFlow.reference_prompt, F.photo | F.document)
async def receive_image_reference(
    message: Message,
    context: AppContext,
    state: FSMContext,
) -> None:
    if await _enqueue_image_reference_album(message, context, state, create_status=True):
        return
    await _append_image_references_batch([message], context, state, create_status=True)


def _image_reference_lock(user_id: int) -> asyncio.Lock:
    lock = IMAGE_REFERENCE_LOCKS.get(user_id)
    if lock is None:
        lock = asyncio.Lock()
        IMAGE_REFERENCE_LOCKS[user_id] = lock
    return lock


def _image_reference_album_key(message: Message) -> str | None:
    media_group_id = str(getattr(message, "media_group_id", None) or "").strip()
    if not media_group_id:
        return None
    user_id = message.from_user.id if message.from_user else 0
    return f"{message.chat.id}:{user_id}:{media_group_id}"


async def _enqueue_image_reference_album(
    message: Message,
    context: AppContext,
    state: FSMContext,
    *,
    create_status: bool,
) -> bool:
    key = _image_reference_album_key(message)
    if not key:
        return False
    bucket = IMAGE_REFERENCE_ALBUMS.get(key)
    if bucket is None:
        bucket = {"messages": [], "context": context, "state": state, "create_status": create_status}
        IMAGE_REFERENCE_ALBUMS[key] = bucket
        asyncio.create_task(_flush_image_reference_album(key))
    bucket["messages"].append(message)
    return True


async def _flush_image_reference_album(key: str) -> None:
    await asyncio.sleep(IMAGE_REFERENCE_ALBUM_DELAY)
    bucket = IMAGE_REFERENCE_ALBUMS.pop(key, None)
    if not bucket:
        return
    messages = sorted(bucket["messages"], key=lambda item: item.message_id)
    if messages:
        await _append_image_references_batch(
            messages,
            bucket["context"],
            bucket["state"],
            create_status=bool(bucket["create_status"]),
        )


async def _append_image_references_batch(
    messages: list[Message],
    context: AppContext,
    state: FSMContext,
    *,
    create_status: bool,
) -> None:
    if not messages:
        return
    first_message = messages[0]
    user = await ensure_user_for_message(first_message, context)
    extracted: list[tuple[str, str, str | None, int | None, Message]] = []
    for message in messages:
        file_id, filename, mime_type, size = _extract_image_file(message)
        if file_id:
            extracted.append((file_id, filename, mime_type, size, message))
    if not extracted:
        await first_message.answer("Нужна картинка JPEG, PNG или WebP.", reply_markup=navigation_keyboard())
        return
    async with _image_reference_lock(user.id):
        data = await state.get_data()
        model_code = data.get("model_code")
        existing_prompt = str(data.get("prompt") or "").strip()
        incoming_prompt = next((message.caption.strip() for *_, message in extracted if (message.caption or "").strip()), "")
        existing_references = _image_reference_items(data)
        explicit_model_selected = bool(data.get("explicit_model_selected"))
        if not model_code or (str(model_code) == "nano-banana" and not explicit_model_selected):
            model_code = await _default_image_model_code(context)
            if not model_code:
                await first_message.answer("Сейчас нет включенных моделей изображений.", reply_markup=navigation_keyboard())
                return
        status_message = await first_message.answer(_status_text("Принимаю референсы", 15)) if create_status else None
        async with session_scope(context.session_factory) as session:
            model = await get_model(session, str(model_code))
            fresh_user = await session.get(User, user.id)
            if not model or not model.is_enabled:
                await first_message.answer("Модель изображений сейчас недоступна.", reply_markup=navigation_keyboard())
                return
            limits = _generation_limits_payload(fresh_user or user, model)
            max_images = _max_image_references_from_limits(limits)
            available_slots = max(0, max_images - len(existing_references))
            if available_slots <= 0:
                text = f"Для {escape(model.title)} можно загрузить максимум {_references_count_text(max_images)}."
                if status_message:
                    await status_message.edit_text(text, reply_markup=navigation_keyboard(back_callback="menu:image"))
                else:
                    await first_message.answer(text, reply_markup=navigation_keyboard(back_callback="menu:image"))
                return
            accepted = extracted[:available_slots]
            for file_id, filename, mime_type, size, _message in accepted:
                session.add(
                    UploadedFile(
                        user_id=user.id,
                        file_type="image",
                        telegram_file_id=file_id,
                        original_name=filename,
                        mime_type=mime_type,
                        size_bytes=size,
                        kie_file_url=f"telegram://{file_id}",
                        expires_at=datetime.now(timezone.utc) + timedelta(days=3),
                    )
                )
        new_references = [
            _image_reference_payload(file_id=file_id, filename=filename, mime_type=mime_type, size=size)
            for file_id, filename, mime_type, size, _message in accepted
        ]
        reference_items = _trim_image_reference_items([*existing_references, *new_references], max_images)
        first_reference = reference_items[0]
        updates: dict[str, Any] = {
            "image_file_id": first_reference["telegram_file_id"],
            "image_filename": first_reference.get("filename"),
            "image_mime_type": first_reference.get("mime_type"),
            "image_references": reference_items,
            "model_code": model_code,
            "image_limits": limits,
            "prompt": incoming_prompt or existing_prompt,
            "resolution": _normalize_image_resolution(data.get("resolution")),
            "aspect_ratio": data.get("aspect_ratio") or DEFAULT_IMAGE_ASPECT_RATIO,
        }
        if status_message:
            updates["status_message_id"] = status_message.message_id
        await state.update_data(**updates)
        await state.set_state(ImageFlow.settings)
        current_data = await state.get_data()
        if status_message:
            await status_message.edit_text(_image_settings_text(current_data), reply_markup=_image_settings_keyboard(current_data))
        else:
            await _edit_image_settings_message(first_message.bot, first_message.chat.id, state)
        skipped = len(extracted) - len(new_references)
        if skipped > 0:
            await first_message.answer(
                f"Добавил {_references_count_text(len(new_references))}; еще {skipped} не поместилось в лимит {max_images}.",
                reply_markup=navigation_keyboard(back_callback="menu:image"),
            )
@router.callback_query(ImageFlow.reference_prompt, F.data.startswith("gen:image_model:"))
async def receive_image_model(
    callback: CallbackQuery,
    context: AppContext,
    state: FSMContext,
    bot: Bot,
) -> None:
    code = callback.data.removeprefix("gen:image_model:")
    await state.update_data(model_code=code)
    await _submit_image_task(callback, context, state, bot)


@router.message(ImageFlow.reference_prompt)
async def receive_image_reference_fallback(message: Message) -> None:
    await message.answer("Сначала отправьте фото-референс: JPEG, PNG или WebP.", reply_markup=navigation_keyboard())


@router.callback_query(ImageFlow.settings, F.data.startswith("image:resolution:"))
async def select_image_resolution(callback: CallbackQuery, state: FSMContext) -> None:
    resolution = callback.data.removeprefix("image:resolution:")
    if resolution not in IMAGE_RESOLUTIONS:
        await callback.answer("Такого качества нет", show_alert=True)
        return
    await state.update_data(resolution=resolution)
    await _edit_image_settings_from_callback(callback, state)
    await callback.answer()


@router.callback_query(ImageFlow.settings, F.data.startswith("image:aspect:"))
async def select_image_aspect_ratio(callback: CallbackQuery, state: FSMContext) -> None:
    aspect_ratio = callback.data.removeprefix("image:aspect:")
    if aspect_ratio not in IMAGE_ASPECT_RATIOS:
        await callback.answer("Такого формата нет", show_alert=True)
        return
    await state.update_data(aspect_ratio=aspect_ratio)
    await _edit_image_settings_from_callback(callback, state)
    await callback.answer()


@router.message(ImageFlow.settings, F.text)
async def receive_image_prompt(
    message: Message,
    context: AppContext,
    state: FSMContext,
    bot: Bot,
) -> None:
    prompt = message.text.strip()
    if not prompt:
        await message.answer("Напишите промпт текстом.")
        return
    await state.update_data(prompt=prompt)
    await _submit_image_task_from_message(message, context, state, bot)


@router.message(ImageFlow.settings, F.photo | F.document)
async def receive_image_settings_file(
    message: Message,
    context: AppContext,
    state: FSMContext,
) -> None:
    if await _enqueue_image_reference_album(message, context, state, create_status=False):
        return
    await _append_image_references_batch([message], context, state, create_status=False)



async def _edit_image_settings_from_callback(callback: CallbackQuery, state: FSMContext) -> None:
    if not callback.message:
        return
    data = await state.get_data()
    with suppress(TelegramBadRequest):
        await callback.message.edit_text(
            _image_settings_text(data),
            reply_markup=_image_settings_keyboard(data),
        )


async def _edit_image_settings_message(bot: Bot, chat_id: int, state: FSMContext) -> None:
    data = await state.get_data()
    status_message_id = data.get("status_message_id")
    if isinstance(status_message_id, int):
        with suppress(Exception):
            await bot.edit_message_text(
                _image_settings_text(data),
                chat_id=chat_id,
                message_id=status_message_id,
                reply_markup=_image_settings_keyboard(data),
            )
            return
    status_message = await bot.send_message(
        chat_id,
        _image_settings_text(data),
        reply_markup=_image_settings_keyboard(data),
    )
    await state.update_data(status_message_id=status_message.message_id)


def _image_settings_text(data: dict[str, Any]) -> str:
    resolution = _normalize_image_resolution(data.get("resolution"))
    aspect_ratio = str(data.get("aspect_ratio") or DEFAULT_IMAGE_ASPECT_RATIO)
    prompt = str(data.get("prompt") or "").strip()
    prompt_text = escape(_shorten(prompt, 900)) if prompt else "не задан"
    limits_text = _image_limits_text(data.get("image_limits"))
    references_count = len(_image_reference_items(data))
    max_references = _max_image_references_from_limits(data.get("image_limits"))
    if prompt:
        instruction = (
            "Нажмите «Запустить», чтобы списать кредиты и начать генерацию. "
            "Или отправьте новый промпт текстом, чтобы заменить текущий."
        )
    else:
        instruction = (
            "Теперь отправьте промпт текстом — после этого генерация запустится автоматически."
        )
    if references_count < max_references:
        instruction = (
            f"Можно добавить еще референсы: {references_count}/{max_references}. "
            f"Когда хватит — {instruction}"
        )
    header = "Референс загружен" if references_count == 1 else "Референсы загружены"
    return (
        f"{header}\n\n"
        f"Референсы: <b>{references_count}/{max_references}</b>\n"
        f"Качество: <b>{escape(resolution)}</b>\n"
        f"Формат: <b>{escape(aspect_ratio)}</b>\n"
        f"{limits_text}"
        f"Промпт:\n{prompt_text}\n\n"
        f"{instruction}"
    )


def _generation_limits_payload(user: User, model: Any) -> dict[str, Any]:
    price = int(getattr(model, "price_credits", 0) or 0)
    credit_type = model_credit_type(model)
    balance = user_credit_balance(user, credit_type)
    is_admin = bool(user.is_admin)
    free_generation = user_generates_for_free(user)
    if free_generation or price <= 0:
        available_generations = "безлимит"
    else:
        available_generations = str(max(0, balance // price))
    unlimited_until = user.unlimited_until.strftime("%Y-%m-%d %H:%M") if user.unlimited_until else None
    return {
        "model_title": str(getattr(model, "title", "Модель")),
        "price_credits": price,
        "credits_balance": balance,
        "credit_type": credit_type,
        "available_generations": available_generations,
        "is_admin": is_admin,
        "unlimited_until": unlimited_until,
        "max_images": _max_image_references_from_config(getattr(model, "config", None)),
    }


def _image_limits_text(raw_limits: Any) -> str:
    if not isinstance(raw_limits, dict):
        return ""
    title = escape(str(raw_limits.get("model_title") or "Модель"))
    price = int(raw_limits.get("price_credits") or 0)
    balance = int(raw_limits.get("credits_balance") or 0)
    credit_type = str(raw_limits.get("credit_type") or "common")
    available = escape(str(raw_limits.get("available_generations") or "0"))
    price_text = (
        f"{_credit_amount_text(0, credit_type)} для админа"
        if raw_limits.get("is_admin")
        else _credit_amount_text(price, credit_type)
    )
    lines = [
        f"Модель: <b>{title}</b>",
        f"Стоимость: <b>{price_text}</b>",
        f"Баланс: <b>{_credit_amount_text(balance, credit_type)}</b>",
        f"Доступно: <b>{available}</b>",
    ]
    if raw_limits.get("unlimited_until"):
        lines.append(f"Безлимит до: <b>{escape(str(raw_limits['unlimited_until']))}</b>")
    return "\n".join(lines) + "\n"


def _image_reference_payload(
    *,
    file_id: str,
    filename: str,
    mime_type: str | None,
    size: int | None,
) -> dict[str, Any]:
    return {
        "telegram_file_id": file_id,
        "filename": filename,
        "mime_type": mime_type or "image/jpeg",
        "size": size,
    }


def _task_is_image_generation(task: GenerationTask) -> bool:
    payload = task.input_payload or {}
    if isinstance(payload, dict) and payload.get("references"):
        return True
    return str(task.model_code or "") in MINI_APP_IMAGE_MODELS


def _repeat_image_state_payload(task: GenerationTask) -> dict[str, Any] | None:
    payload = task.input_payload or {}
    if not isinstance(payload, dict):
        return None
    references = payload.get("references")
    if not isinstance(references, list) or not references:
        legacy_reference = payload.get("reference")
        references = [legacy_reference] if isinstance(legacy_reference, dict) else []
    reference_items: list[dict[str, Any]] = []
    for item in references:
        if not isinstance(item, dict):
            continue
        file_id = str(item.get("telegram_file_id") or "").strip()
        if not file_id:
            continue
        reference_items.append(
            _image_reference_payload(
                file_id=file_id,
                filename=str(item.get("filename") or "image"),
                mime_type=str(item.get("mime_type") or "image/jpeg"),
                size=item.get("size"),
            )
        )
    if not reference_items:
        return None
    first_reference = reference_items[0]
    model_code = str(task.model_code or DEFAULT_MINI_APP_IMAGE_MODEL)
    max_images = payload.get("max_reference_images") or payload.get("max_images") or len(reference_items)
    return {
        "model_code": model_code,
        "prompt": task.prompt or str(payload.get("prompt") or ""),
        "aspect_ratio": payload.get("aspect_ratio") or DEFAULT_IMAGE_ASPECT_RATIO,
        "resolution": _normalize_image_resolution(payload.get("resolution")),
        "image_file_id": first_reference["telegram_file_id"],
        "image_filename": first_reference.get("filename"),
        "image_mime_type": first_reference.get("mime_type"),
        "image_references": _trim_image_reference_items(reference_items, max_images),
        "image_limits": {"max_images": max_images},
    }


def _image_reference_items(data: dict[str, Any]) -> list[dict[str, Any]]:
    raw_references = data.get("image_references")
    references: list[dict[str, Any]] = []
    if isinstance(raw_references, list):
        for item in raw_references:
            if not isinstance(item, dict):
                continue
            file_id = str(item.get("telegram_file_id") or "").strip()
            if not file_id:
                continue
            references.append(
                {
                    "telegram_file_id": file_id,
                    "filename": str(item.get("filename") or "image"),
                    "mime_type": str(item.get("mime_type") or "image/jpeg"),
                    "size": item.get("size"),
                }
            )
    if references:
        return references

    legacy_file_id = str(data.get("image_file_id") or "").strip()
    if not legacy_file_id:
        return []
    return [
        _image_reference_payload(
            file_id=legacy_file_id,
            filename=str(data.get("image_filename") or "image"),
            mime_type=str(data.get("image_mime_type") or "image/jpeg"),
            size=None,
        )
    ]


def _trim_image_reference_items(
    references: list[dict[str, Any]],
    max_images: int,
) -> list[dict[str, Any]]:
    return references[: _normalize_max_images(max_images)]


def _max_image_references_from_limits(raw_limits: Any) -> int:
    if not isinstance(raw_limits, dict):
        return 1
    return _normalize_max_images(raw_limits.get("max_images"))


def _max_image_references_from_config(raw_config: Any) -> int:
    if not isinstance(raw_config, dict):
        return 1
    return _normalize_max_images(raw_config.get("max_images"))


def _normalize_max_images(value: Any) -> int:
    try:
        max_images = int(value)
    except (TypeError, ValueError):
        return 1
    return max(1, min(max_images, 30))


def _references_count_text(count: int) -> str:
    if count == 1:
        return "1 референс"
    return f"{count} референсов"


def _image_settings_keyboard(data: dict[str, Any]) -> InlineKeyboardMarkup:
    current_resolution = _normalize_image_resolution(data.get("resolution"))
    current_aspect_ratio = str(data.get("aspect_ratio") or DEFAULT_IMAGE_ASPECT_RATIO)
    builder = InlineKeyboardBuilder()
    for resolution in IMAGE_RESOLUTIONS:
        builder.button(
            text=_option_label(resolution, current_resolution),
            callback_data=f"image:resolution:{resolution}",
        )
    for aspect_ratio in IMAGE_ASPECT_RATIOS:
        builder.button(
            text=_option_label(aspect_ratio, current_aspect_ratio),
            callback_data=f"image:aspect:{aspect_ratio}",
        )
    submit_rows = []
    if str(data.get("prompt") or "").strip():
        builder.button(text="Запустить", callback_data="image:submit")
        submit_rows.append(1)
    nav_count = add_navigation_buttons(builder, back_callback="menu:image")
    builder.adjust(len(IMAGE_RESOLUTIONS), len(IMAGE_ASPECT_RATIOS), *submit_rows, nav_count)
    return builder.as_markup()


def _option_label(value: str, selected_value: str) -> str:
    if value == selected_value:
        return f"[{value}]"
    return value


def _normalize_image_resolution(value: Any) -> str:
    return normalize_image_resolution(value)


def _shorten(value: str, limit: int) -> str:
    if len(value) <= limit:
        return value
    return f"{value[: limit - 3].rstrip()}..."


@router.message(MotionFlow.image, F.photo | F.document)
async def receive_motion_image(message: Message, context: AppContext, state: FSMContext) -> None:
    user = await ensure_user_for_message(message, context)
    file_id, filename, mime_type, size = _extract_image_file(message)
    normalized_mime_type = _normalize_motion_image_mime_type(mime_type, filename)
    if not file_id or not normalized_mime_type:
        await message.answer(
            "Нужно изображение JPEG, PNG или JPG до 10 MB.",
            reply_markup=navigation_keyboard(back_callback="menu:motion"),
        )
        return
    if size and size > MOTION_CONTROL_IMAGE_MAX_BYTES:
        await message.answer(
            "Изображение для Motion Control должно быть до 10 MB.",
            reply_markup=navigation_keyboard(back_callback="menu:motion"),
        )
        return
    async with session_scope(context.session_factory) as session:
        session.add(
            UploadedFile(
                user_id=user.id,
                file_type="motion_image",
                telegram_file_id=file_id,
                original_name=filename,
                mime_type=normalized_mime_type,
                size_bytes=size,
                kie_file_url=f"telegram://{file_id}",
                expires_at=datetime.now(timezone.utc) + timedelta(days=3),
            )
        )
    await state.update_data(
        image_file_id=file_id,
        image_filename=filename,
        image_mime_type=normalized_mime_type,
    )
    data = await state.get_data()
    if str(data.get("prompt") or "").strip():
        if _is_seedance_model_code(str(data.get("model_code") or "")):
            await state.set_state(MotionFlow.duration)
            await message.answer(
                "Промпт из BANANA сохранен.\nДлительность Seedance 2:",
                reply_markup=options_keyboard("motion:duration", VIDEO_DURATIONS, back="menu:motion"),
            )
            return
        await state.set_state(MotionFlow.motion_video)
        await message.answer(
            "Промпт сохранен.\nОтправьте видео-референс движения: MP4/MOV/MKV, 3-30 сек. "
            "Стоимость посчитаю по длительности.",
            reply_markup=navigation_keyboard(back_callback="menu:motion"),
        )
    else:
        await state.set_state(MotionFlow.prompt)
        await message.answer(
            "Напишите prompt для видео или нажмите «Пропустить».",
            reply_markup=options_keyboard("motion:prompt", ["Пропустить"], back="menu:motion"),
        )


@router.callback_query(MotionFlow.prompt, F.data == "motion:prompt:Пропустить")
async def skip_motion_prompt(callback: CallbackQuery, state: FSMContext) -> None:
    await state.update_data(prompt="")
    data = await state.get_data()
    if _is_seedance_model_code(str(data.get("model_code") or "")):
        await state.set_state(MotionFlow.duration)
        if callback.message:
            await callback.message.answer(
                "Длительность Seedance 2:",
                reply_markup=options_keyboard("motion:duration", VIDEO_DURATIONS, back="menu:motion"),
        )
        await callback.answer()
        return
    await state.set_state(MotionFlow.motion_video)
    if callback.message:
        await callback.message.answer(
            "Отправьте видео-референс движения: MP4/MOV/MKV, 3-30 сек. "
            "Стоимость посчитаю по длительности.",
            reply_markup=navigation_keyboard(back_callback="menu:motion"),
        )
    await callback.answer()


@router.message(MotionFlow.prompt, F.text)
async def receive_motion_prompt(message: Message, state: FSMContext) -> None:
    await state.update_data(prompt=message.text.strip())
    data = await state.get_data()
    if _is_seedance_model_code(str(data.get("model_code") or "")):
        await state.set_state(MotionFlow.duration)
        await message.answer(
            "Длительность Seedance 2:",
            reply_markup=options_keyboard("motion:duration", VIDEO_DURATIONS, back="menu:motion"),
        )
        return
    await state.set_state(MotionFlow.motion_video)
    await message.answer(
        "Отправьте видео-референс движения: MP4/MOV/MKV, 3-30 сек. "
        "Стоимость посчитаю по длительности.",
        reply_markup=navigation_keyboard(back_callback="menu:motion"),
    )


@router.message(MotionFlow.motion_video, F.video | F.document)
async def receive_motion_video_reference(
    message: Message,
    context: AppContext,
    state: FSMContext,
    bot: Bot,
) -> None:
    user = await ensure_user_for_message(message, context)
    file_id, filename, mime_type, size, telegram_duration = _extract_video_file(message)
    normalized_mime_type = _normalize_motion_video_mime_type(mime_type, filename)
    if not file_id or not normalized_mime_type:
        await message.answer(
            "Нужно видео-референс движения: MP4, MOV или MKV, 3-30 сек., до 100 MB.",
            reply_markup=navigation_keyboard(back_callback="menu:motion"),
        )
        return
    if size and size > MOTION_CONTROL_VIDEO_MAX_BYTES:
        await message.answer(
            "Видео-референс для Motion Control должен быть до 100 MB.",
            reply_markup=navigation_keyboard(back_callback="menu:motion"),
        )
        return
    status_message = await message.answer(_status_text("Проверяю видео-референс", 20))
    try:
        video_content = await _download_telegram_file(bot=bot, file_id=file_id)
        if len(video_content) > MOTION_CONTROL_VIDEO_MAX_BYTES:
            await status_message.edit_text(
                "Видео-референс для Motion Control должен быть до 100 MB.",
                reply_markup=navigation_keyboard(back_callback="menu:motion"),
            )
            return
        duration_seconds = telegram_duration or await _probe_video_duration_seconds(
            video_content,
            normalized_mime_type,
        )
    except Exception:
        logger.exception("Motion reference video download/probe failed")
        await status_message.edit_text(
            "Не получилось прочитать длительность видео. Отправьте MP4/MOV/MKV файл 3-30 сек.",
            reply_markup=navigation_keyboard(back_callback="menu:motion"),
        )
        return
    billable_seconds = _billable_motion_seconds(duration_seconds)
    if billable_seconds < MOTION_CONTROL_MIN_SECONDS or billable_seconds > MOTION_CONTROL_MAX_SECONDS:
        await status_message.edit_text(
            f"Видео должно быть от {MOTION_CONTROL_MIN_SECONDS} до {MOTION_CONTROL_MAX_SECONDS} сек. "
            f"Сейчас получилось: {duration_seconds:.1f} сек.",
            reply_markup=navigation_keyboard(back_callback="menu:motion"),
        )
        return
    await state.update_data(
        motion_video_file_id=file_id,
        motion_video_filename=filename,
        motion_video_mime_type=normalized_mime_type,
        motion_video_size=size,
        motion_video_duration_seconds=duration_seconds,
        motion_video_billable_seconds=billable_seconds,
        status_message_id=status_message.message_id,
    )
    await _submit_motion_control_task_from_message(
        message=message,
        context=context,
        state=state,
        bot=bot,
        user_id=user.id,
        chat_id=message.chat.id,
        motion_video_content=video_content,
    )


@router.message(MotionFlow.motion_video)
async def receive_motion_video_reference_fallback(message: Message) -> None:
    await message.answer(
        "Отправьте видео-референс движения: MP4, MOV или MKV, 3-30 сек., до 100 MB.",
        reply_markup=navigation_keyboard(back_callback="menu:motion"),
    )


@router.callback_query(MotionFlow.mode, F.data.startswith("motion:mode:"))
async def receive_motion_mode(callback: CallbackQuery, state: FSMContext) -> None:
    mode = callback.data.removeprefix("motion:mode:")
    if mode not in VIDEO_MODES:
        await callback.answer("Такого режима нет", show_alert=True)
        return
    await state.update_data(mode=mode)
    await state.set_state(MotionFlow.duration)
    if callback.message:
        await callback.message.answer(
            "Длительность:",
            reply_markup=options_keyboard("motion:duration", VIDEO_DURATIONS, back="menu:motion"),
        )
    await callback.answer()


@router.callback_query(MotionFlow.duration, F.data.startswith("motion:duration:"))
async def receive_motion_duration(
    callback: CallbackQuery,
    context: AppContext,
    state: FSMContext,
    bot: Bot,
) -> None:
    duration = callback.data.removeprefix("motion:duration:")
    if duration not in VIDEO_DURATIONS:
        await callback.answer("Такой длительности нет", show_alert=True)
        return
    await state.update_data(duration=duration)
    await _submit_motion_task(callback, context, state, bot)


async def _submit_image_task(
    callback: CallbackQuery,
    context: AppContext,
    state: FSMContext,
    bot: Bot,
) -> None:
    user = await ensure_user_for_callback(callback, context)
    await _submit_image_task_common(
        message=callback.message,
        callback=callback,
        context=context,
        state=state,
        bot=bot,
        user_id=user.id,
        chat_id=callback.message.chat.id if callback.message else user.telegram_id,
    )


async def _submit_image_task_from_message(
    message: Message,
    context: AppContext,
    state: FSMContext,
    bot: Bot,
) -> None:
    user = await ensure_user_for_message(message, context)
    await _submit_image_task_common(
        message=message,
        callback=None,
        context=context,
        state=state,
        bot=bot,
        user_id=user.id,
        chat_id=message.chat.id,
    )


async def _submit_image_task_common(
    *,
    message: Message | None,
    callback: CallbackQuery | None,
    context: AppContext,
    state: FSMContext,
    bot: Bot,
    user_id: int,
    chat_id: int,
) -> None:
    data = await state.get_data()
    prompt = str(data.get("prompt") or "").strip()
    if not prompt:
        await _notify_image_submit_error(message, callback, "Сначала напишите промпт текстом")
        return

    model_code = str(data.get("model_code") or "")
    if not model_code:
        async with session_scope(context.session_factory) as session:
            models = await list_enabled_models(session, "image")
        if not models:
            await _notify_image_submit_error(
                message,
                callback,
                "Сейчас нет включенных моделей изображений",
                show_alert=True,
            )
            return
        model_code = models[0].code
        await state.update_data(model_code=model_code)
        data = await state.get_data()

    reference_images: list[CometImageReference] = []
    reference_items = _image_reference_items(data)
    if not reference_items:
        await _notify_image_submit_error(message, callback, "Сначала отправьте фото-референс", show_alert=True)
        return

    if message and isinstance(data.get("status_message_id"), int):
        with suppress(Exception):
            await message.bot.edit_message_text(
                _status_text("Готовлю референсы", 45),
                chat_id=chat_id,
                message_id=data["status_message_id"],
            )
    try:
        for item in reference_items:
            reference_content = await _download_telegram_file(
                bot=bot,
                file_id=str(item["telegram_file_id"]),
            )
            reference_images.append(
                CometImageReference(
                    content=reference_content,
                    mime_type=str(item.get("mime_type") or "image/jpeg"),
                )
            )
    except Exception:
        logger.exception("Image reference download failed")
        if message:
            await message.answer(
                "Не получилось получить референс из Telegram. Отправьте фото заново.",
                reply_markup=navigation_keyboard(back_callback="menu:image"),
            )
        if callback:
            await callback.answer()
        return

    first_reference = reference_items[0]
    references_payload = [
        {
            "telegram_file_id": item["telegram_file_id"],
            "filename": item.get("filename"),
            "mime_type": item.get("mime_type"),
            "size": item.get("size"),
        }
        for item in reference_items
    ]

    input_payload: dict[str, Any] = {
        "prompt": prompt,
        "aspect_ratio": data.get("aspect_ratio") or DEFAULT_IMAGE_ASPECT_RATIO,
        "resolution": _normalize_image_resolution(data.get("resolution")),
        "reference": {
            "telegram_file_id": first_reference["telegram_file_id"],
            "filename": first_reference.get("filename"),
            "mime_type": first_reference.get("mime_type"),
        },
        "references": references_payload,
        "reference_count": len(references_payload),
    }
    if data.get("source_feed_task_id"):
        input_payload["source_feed_task_id"] = int(data["source_feed_task_id"])

    await _create_comet_image_task(
        message=message,
        callback=callback,
        context=context,
        state=state,
        user_id=user_id,
        chat_id=chat_id,
        model_code=model_code,
        prompt=prompt,
        input_payload=input_payload,
        reference_images=reference_images,
    )


async def _notify_image_submit_error(
    message: Message | None,
    callback: CallbackQuery | None,
    text: str,
    *,
    show_alert: bool = False,
) -> None:
    if callback:
        await callback.answer(text, show_alert=show_alert)
    elif message:
        await message.answer(f"{text}.", reply_markup=navigation_keyboard(back_callback="menu:image"))


async def _submit_motion_task(
    callback: CallbackQuery,
    context: AppContext,
    state: FSMContext,
    bot: Bot,
) -> None:
    user = await ensure_user_for_callback(callback, context)
    data = await state.get_data()
    image_file_id = data.get("image_file_id")
    if not image_file_id:
        await callback.answer("Сначала отправьте изображение-референс", show_alert=True)
        return
    try:
        image_content = await _download_telegram_file(bot=bot, file_id=str(image_file_id))
    except Exception:
        logger.exception("Kling reference image download failed")
        await callback.answer("Не получилось получить изображение из Telegram", show_alert=True)
        return

    input_payload: dict[str, Any] = {
        "prompt": _shorten(str(data.get("prompt") or ""), 500),
        "image": base64.b64encode(image_content).decode("ascii"),
        "reference": {
            "telegram_file_id": image_file_id,
            "filename": data.get("image_filename"),
            "mime_type": data.get("image_mime_type"),
        },
        "mode": data.get("mode") or "pro",
        "duration": data.get("duration") or "5",
        "aspect_ratio": _normalize_video_aspect(data.get("aspect_ratio")),
        "resolution": DEFAULT_VIDEO_RESOLUTION,
    }
    if data.get("source_feed_task_id"):
        input_payload["source_feed_task_id"] = int(data["source_feed_task_id"])
    await _create_comet_video_task(
        callback=callback,
        context=context,
        state=state,
        user_id=user.id,
        chat_id=callback.message.chat.id if callback.message else user.telegram_id,
        model_code=data["model_code"],
        prompt=input_payload["prompt"],
        input_payload=input_payload,
    )


async def _submit_motion_control_task_from_message(
    *,
    message: Message,
    context: AppContext,
    state: FSMContext,
    bot: Bot,
    user_id: int,
    chat_id: int,
    motion_video_content: bytes,
) -> None:
    data = await state.get_data()
    image_file_id = data.get("image_file_id")
    if not image_file_id:
        await message.answer(
            "Сначала отправьте изображение персонажа.",
            reply_markup=navigation_keyboard(back_callback="menu:motion"),
        )
        return
    try:
        image_content = await _download_telegram_file(bot=bot, file_id=str(image_file_id))
    except Exception:
        logger.exception("Motion control reference image download failed")
        await message.answer(
            "Не получилось получить изображение из Telegram. Отправьте фото заново.",
            reply_markup=navigation_keyboard(back_callback="menu:motion"),
        )
        return

    input_payload: dict[str, Any] = {
        "prompt": _shorten(str(data.get("prompt") or ""), 500),
        "reference": {
            "telegram_file_id": image_file_id,
            "filename": data.get("image_filename"),
            "mime_type": data.get("image_mime_type"),
        },
        "motion_reference": {
            "telegram_file_id": data.get("motion_video_file_id"),
            "filename": data.get("motion_video_filename"),
            "mime_type": data.get("motion_video_mime_type"),
            "size": data.get("motion_video_size"),
        },
        "duration": data.get("motion_video_duration_seconds"),
        "billable_seconds": data.get("motion_video_billable_seconds"),
        "mode": MOTION_CONTROL_MODE,
        "character_orientation": MOTION_CONTROL_CHARACTER_ORIENTATION,
    }
    if data.get("source_feed_task_id"):
        input_payload["source_feed_task_id"] = int(data["source_feed_task_id"])

    await _create_kie_motion_control_task(
        message=message,
        context=context,
        state=state,
        user_id=user_id,
        chat_id=chat_id,
        model_code=str(data["model_code"]),
        prompt=input_payload["prompt"],
        input_payload=input_payload,
        image_content=image_content,
        image_mime_type=str(data.get("image_mime_type") or "image/jpeg"),
        image_filename=str(data.get("image_filename") or "motion-control-image.jpg"),
        video_content=motion_video_content,
        video_mime_type=str(data.get("motion_video_mime_type") or "video/mp4"),
        video_filename=str(data.get("motion_video_filename") or "motion-reference.mp4"),
    )


async def _create_comet_image_task(
    *,
    message: Message | None = None,
    callback: CallbackQuery | None = None,
    context: AppContext,
    state: FSMContext,
    user_id: int,
    chat_id: int,
    model_code: str,
    prompt: str,
    input_payload: dict[str, Any],
    reference_images: list[CometImageReference],
) -> None:
    target_message = message or (callback.message if callback else None)
    status_message_id = None
    data = await state.get_data()
    if isinstance(data.get("status_message_id"), int):
        status_message_id = data["status_message_id"]
    if target_message and status_message_id:
        with suppress(Exception):
            await target_message.bot.edit_message_text(
                _status_text("Запускаю генерацию", 55),
                chat_id=chat_id,
                message_id=status_message_id,
            )
    elif target_message:
        status_message = await target_message.answer(_status_text("Запускаю генерацию", 55))
        status_message_id = status_message.message_id

    async with session_scope(context.session_factory) as session:
        user = await session.get(User, user_id, with_for_update=True)
        model = await get_model(session, model_code)
        if not user or not model or not model.is_enabled or model.category != "image":
            await _notify_image_submit_error(target_message, callback, "Модель недоступна", show_alert=True)
            return
        has_unlimited = user_has_unlimited(user)
        free_generation = user_generates_for_free(user)
        credit_type = model_credit_type(model)
        available_balance = user_credit_balance(user, credit_type)
        can_pay = model.price_credits <= 0 or free_generation or available_balance >= model.price_credits
        if not can_pay:
            await _notify_image_submit_error(
                target_message,
                callback,
                "Недостаточно фото-кредитов. Откройте раздел «Пакеты»",
                show_alert=True,
            )
            return

        charged_credits = 0 if model.price_credits <= 0 or free_generation else model.price_credits
        credit_spend = spend_user_credits(user, credit_type=credit_type, amount=charged_credits)
        if credit_spend is None:
            await _notify_image_submit_error(
                target_message,
                callback,
                "Недостаточно фото-кредитов. Откройте раздел «Пакеты»",
                show_alert=True,
            )
            return
        charge_details = _charge_details_text(user, charged_credits, has_unlimited, credit_type)
        provider_model = _resolve_image_provider_model(model_code, model.config, context)
        max_reference_images = _max_image_references_from_config(model.config)
        reference_images = _limit_reference_images(reference_images, max_reference_images)
        task_input = {
            **input_payload,
            "provider": "comet",
            "provider_model": provider_model,
            "max_reference_images": max_reference_images,
            "credit_type": credit_type,
            "credit_spend": credit_spend,
        }
        task = GenerationTask(
            user_id=user.id,
            model_code=model.code,
            provider_task_id=None,
            status="generating",
            prompt=prompt,
            input_payload=task_input,
            cost_credits=charged_credits,
            chat_id=chat_id,
            message_id=status_message_id,
            source_feed_task_id=input_payload.get("source_feed_task_id"),
        )
        session.add(task)
        await session.flush()
        task_id = task.id

    await state.clear()
    if callback:
        await callback.answer()
    if target_message and status_message_id:
        with suppress(Exception):
            await target_message.bot.edit_message_text(
                _status_text(
                    "Создаю изображение",
                    75,
                    f"{charge_details}\nОбычно это занимает несколько секунд.",
                ),
                chat_id=chat_id,
                message_id=status_message_id,
            )

    try:
        result = await context.comet.generate_image(
            model=provider_model,
            prompt=prompt,
            reference_images=reference_images,
            aspect_ratio=str(input_payload.get("aspect_ratio") or "auto"),
            image_size=_normalize_image_resolution(input_payload.get("resolution")),
            output_mime_type=_output_mime_type(input_payload),
        )
        delivered = await _send_comet_image_result(
            bot=target_message.bot if target_message else context.bot,
            chat_id=chat_id,
            task_id=task_id,
            result=result,
        )
    except Exception as exc:
        logger.exception("Comet image generation failed")
        if context.kie.is_configured:
            with suppress(Exception):
                if target_message and status_message_id:
                    await target_message.bot.edit_message_text(
                        _status_text(
                            "Пробую резервный провайдер",
                            78,
                            f"{charge_details}\nComet не ответил, переключаюсь на KIE.",
                        ),
                        chat_id=chat_id,
                        message_id=status_message_id,
                    )
            try:
                kie_provider_model = _resolve_kie_image_provider_model(model_code, context)
                provider_task_id, uploaded_urls = await _create_kie_image_provider_task(
                    context=context,
                    provider_model=kie_provider_model,
                    prompt=prompt,
                    input_payload=input_payload,
                    reference_images=reference_images,
                    max_reference_images=max_reference_images,
                )
            except Exception as fallback_exc:
                logger.exception("KIE image fallback failed")
                await _fail_comet_image_task(
                    context=context,
                    task_id=task_id,
                    chat_id=chat_id,
                    message_id=status_message_id,
                    error_message=(
                        f"Comet: {exc}\n"
                        f"KIE fallback: {fallback_exc}"
                    ),
                )
                return

            async with session_scope(context.session_factory) as session:
                task = await session.get(GenerationTask, task_id)
                if task:
                    task.provider_task_id = provider_task_id
                    task.status = "submitted"
                    task.result_payload = {}
                    task.input_payload = {
                        **dict(task.input_payload or {}),
                        "provider": "kie",
                        "provider_model": kie_provider_model,
                        "fallback_from": "comet",
                        "comet_error": str(exc),
                        "uploaded_reference_urls": uploaded_urls,
                    }
            if target_message and status_message_id:
                with suppress(Exception):
                    await target_message.bot.edit_message_text(
                        _status_text(
                            f"Генерация #{task_id} запущена",
                            65,
                            "Резервный провайдер KIE принял задачу. Я обновлю статус.",
                        ),
                        chat_id=chat_id,
                        message_id=status_message_id,
                    )
            return
        await _fail_comet_image_task(
            context=context,
            task_id=task_id,
            chat_id=chat_id,
            message_id=status_message_id,
            error_message=str(exc),
        )
        return

    async with session_scope(context.session_factory) as session:
        task = await session.get(GenerationTask, task_id)
        if task:
            task.status = "success"
            task.result_payload = {
                **result.metadata,
                "telegram_preview_file_ids": delivered.preview_file_ids,
                "telegram_source_file_ids": delivered.source_file_ids,
            }
            task.result_urls = delivered.preview_file_ids
            if delivered.preview_file_ids:
                session.add(
                    GalleryItem(
                        generation_task_id=task.id,
                        user_id=task.user_id,
                        title=f"Работа #{task.id}",
                        prompt=task.prompt,
                        media_url=delivered.preview_file_ids[0],
                        media_type="image",
                        model_code=task.model_code,
                        is_public=False,
                    )
                )

    if target_message and status_message_id:
        with suppress(Exception):
            await target_message.bot.edit_message_text(
                _status_text("Готово", 100, "Превью и исходник отправлены ниже."),
                chat_id=chat_id,
                message_id=status_message_id,
            )


async def _create_kie_motion_control_task(
    *,
    message: Message,
    context: AppContext,
    state: FSMContext,
    user_id: int,
    chat_id: int,
    model_code: str,
    prompt: str,
    input_payload: dict[str, Any],
    image_content: bytes,
    image_mime_type: str,
    image_filename: str,
    video_content: bytes,
    video_mime_type: str,
    video_filename: str,
) -> None:
    data = await state.get_data()
    status_message_id = data.get("status_message_id") if isinstance(data.get("status_message_id"), int) else None
    if status_message_id:
        with suppress(Exception):
            await message.bot.edit_message_text(
                _status_text("Загружаю motion reference", 45),
                chat_id=chat_id,
                message_id=status_message_id,
            )
    else:
        status_message = await message.answer(_status_text("Загружаю motion reference", 45))
        status_message_id = status_message.message_id

    async with session_scope(context.session_factory) as session:
        user = await session.get(User, user_id, with_for_update=True)
        model = await get_model(session, model_code)
        if not user or not model or not model.is_enabled or model.category != "video" or not model.code.startswith("kling"):
            await message.answer("Модель Motion Control недоступна.", reply_markup=navigation_keyboard(back_callback="menu:motion"))
            return
        if not context.kie.is_configured:
            await message.answer(
                "Motion Control сейчас недоступен: KIE API не настроен.",
                reply_markup=navigation_keyboard(back_callback="menu:motion"),
            )
            return

        free_generation = user_generates_for_free(user)
        billable_seconds = int(input_payload.get("billable_seconds") or 0)
        price_per_second = int(model.price_credits or 0)
        charged_credits = _motion_control_cost_credits(
            price_per_second=price_per_second,
            billable_seconds=billable_seconds,
            free_generation=free_generation,
        )
        credit_type = model_credit_type(model)
        available_balance = user_credit_balance(user, credit_type)
        if charged_credits > 0 and available_balance < charged_credits:
            await message.answer(
                "Недостаточно видео-кредитов.\n"
                f"Видео: {billable_seconds} сек × {_credit_amount_text(price_per_second, credit_type)} = "
                f"{_credit_amount_text(charged_credits, credit_type)}.\n"
                f"Баланс: {_credit_amount_text(available_balance, credit_type)}.",
                reply_markup=navigation_keyboard(back_callback="menu:packages"),
            )
            return

        model_config = model.config or {}
        provider_model = _resolve_kie_motion_control_provider_model(model.code, context)
        motion_mode = str(
            model_config.get("motion_control_mode") or input_payload.get("mode") or MOTION_CONTROL_MODE
        )
        character_orientation = _normalize_motion_control_character_orientation(
            input_payload.get("character_orientation") or model_config.get("character_orientation")
        )
        if (
            character_orientation == "image"
            and billable_seconds > MOTION_CONTROL_IMAGE_ORIENTATION_MAX_SECONDS
        ):
            character_orientation = "video"
        background_source = str(model_config.get("background_source") or "").strip() or None
        credit_spend = spend_user_credits(user, credit_type=credit_type, amount=charged_credits)
        if credit_spend is None:
            await message.answer(
                "Недостаточно видео-кредитов.",
                reply_markup=navigation_keyboard(back_callback="menu:packages"),
            )
            return
        stored_input_payload = {
            **input_payload,
            "mode": motion_mode,
            "character_orientation": character_orientation,
        }
        task = GenerationTask(
            user_id=user.id,
            model_code=model.code,
            provider_task_id=None,
            status="submitting",
            prompt=prompt,
            input_payload={
                **stored_input_payload,
                "provider": "kie",
                "provider_family": "kling-motion-control",
                "provider_model": provider_model,
                "price_per_second": price_per_second,
                "charged_credits": charged_credits,
                "credit_type": credit_type,
                "credit_spend": credit_spend,
                **({"background_source": background_source} if background_source else {}),
            },
            result_payload={},
            cost_credits=charged_credits,
            chat_id=chat_id,
            message_id=status_message_id,
            source_feed_task_id=input_payload.get("source_feed_task_id"),
        )
        session.add(task)
        await session.flush()
        task_id = task.id

    try:
        image_url = await context.kie.upload_base64_file(
            KieUploadReference(
                content=image_content,
                mime_type=image_mime_type,
                filename=image_filename,
            )
        )
        video_url = await context.kie.upload_base64_file(
            KieUploadReference(
                content=video_content,
                mime_type=video_mime_type,
                filename=video_filename,
            )
        )
        provider_task_id = await context.kie.create_kling_motion_control_task(
            model=provider_model,
            prompt=_video_prompt_for_provider(input_payload),
            input_urls=[image_url],
            video_urls=[video_url],
            mode=motion_mode,
            character_orientation=character_orientation,
            background_source=background_source,
            callback_url=context.settings.comet_callback_url,
        )
    except Exception as exc:
        logger.exception("KIE motion control task creation failed")
        await _fail_comet_image_task(
            context=context,
            task_id=task_id,
            chat_id=chat_id,
            message_id=status_message_id,
            error_message=f"Не получилось запустить Motion Control: {exc}",
        )
        return

    async with session_scope(context.session_factory) as session:
        task = await session.get(GenerationTask, task_id)
        if not task or task.status in {"success", "fail"}:
            return
        task.provider_task_id = provider_task_id
        task.status = "submitted"
        task.input_payload = {
            **dict(task.input_payload or {}),
            "uploaded_reference_urls": [image_url],
            "uploaded_motion_video_urls": [video_url],
        }

    await state.clear()
    with suppress(Exception):
        await message.bot.edit_message_text(
            _status_text(
                f"Motion Control #{task_id} запущен",
                65,
                f"Видео: {billable_seconds} сек × {_credit_amount_text(price_per_second, 'video')} = "
                f"{_credit_amount_text(charged_credits, 'video')}.\nЯ обновлю статус и пришлю результат.",
            ),
            chat_id=chat_id,
            message_id=status_message_id,
        )


async def _create_comet_video_task(
    *,
    message: Message | None = None,
    callback: CallbackQuery | None = None,
    context: AppContext,
    state: FSMContext,
    user_id: int,
    chat_id: int,
    model_code: str,
    prompt: str,
    input_payload: dict[str, Any],
) -> None:
    target_message = message or (callback.message if callback else None)
    status_message_id = None
    data = await state.get_data()
    if isinstance(data.get("status_message_id"), int):
        status_message_id = data["status_message_id"]
    if target_message:
        if status_message_id:
            with suppress(Exception):
                await target_message.bot.edit_message_text(
                    _status_text("Запускаю генерацию", 55),
                    chat_id=chat_id,
                    message_id=status_message_id,
                )
        else:
            status_message = await target_message.answer(_status_text("Запускаю генерацию", 55))
            status_message_id = status_message.message_id
    async with session_scope(context.session_factory) as session:
        user = await session.get(User, user_id, with_for_update=True)
        model = await get_model(session, model_code)
        if not user or not model or not model.is_enabled or model.category != "video":
            if callback:
                await callback.answer("Модель недоступна", show_alert=True)
            elif target_message:
                await target_message.answer("Модель недоступна.", reply_markup=navigation_keyboard(back_callback="menu:motion"))
            return
        free_generation = user_generates_for_free(user)
        credit_type = model_credit_type(model)
        available_balance = user_credit_balance(user, credit_type)
        can_pay = model.price_credits <= 0 or free_generation or available_balance >= model.price_credits
        if not can_pay:
            if callback:
                await callback.answer("Недостаточно видео-кредитов. Откройте раздел «Пакеты».", show_alert=True)
            elif target_message:
                await target_message.answer(
                    "Недостаточно видео-кредитов. Откройте раздел «Пакеты».",
                    reply_markup=navigation_keyboard(back_callback="menu:motion"),
                )
            return
        charged_credits = 0 if model.price_credits <= 0 or free_generation else model.price_credits
        credit_spend = spend_user_credits(user, credit_type=credit_type, amount=charged_credits)
        if credit_spend is None:
            if callback:
                await callback.answer("Недостаточно видео-кредитов. Откройте раздел «Пакеты».", show_alert=True)
            elif target_message:
                await target_message.answer(
                    "Недостаточно видео-кредитов. Откройте раздел «Пакеты».",
                    reply_markup=navigation_keyboard(back_callback="menu:motion"),
                )
            return
        provider_image = str(input_payload["image"])
        stored_input_payload = {
            **input_payload,
            "image": {
                "source": "telegram",
                "base64_length": len(provider_image),
            },
        }
        provider = "comet"
        provider_family = _resolve_video_provider_family(model.code, model.config)
        provider_model = _resolve_video_provider_model(model.code, model.config, context)
        provider_prompt = _video_prompt_for_provider(input_payload)
        duration = str(input_payload.get("duration") or "5")
        aspect_ratio = _video_aspect_ratio(input_payload, model.config)
        resolution = _video_resolution(input_payload, model.config)
        image_mime_type = str(input_payload.get("reference", {}).get("mime_type") or "image/jpeg")
        image_bytes = base64.b64decode(provider_image)
        task = GenerationTask(
            user_id=user.id,
            model_code=model.code,
            provider_task_id=None,
            status="submitting",
            prompt=prompt,
            input_payload={
                **stored_input_payload,
                "provider": provider,
                "provider_family": provider_family,
                "provider_model": provider_model,
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
                "credit_type": credit_type,
                "credit_spend": credit_spend,
                **({"fallback_from": "comet"} if provider == "kie" else {}),
            },
            result_payload={},
            cost_credits=charged_credits,
            chat_id=chat_id,
            message_id=status_message_id,
            source_feed_task_id=input_payload.get("source_feed_task_id"),
        )
        session.add(task)
        await session.flush()
        task_id = task.id

    result_payload: dict[str, Any] = {}
    try:
        if provider_family == "seedance":
            provider_task_id = await context.comet.create_seedance_video_task(
                model=provider_model,
                prompt=provider_prompt,
                image=image_bytes,
                image_mime_type=image_mime_type,
                image_filename=_provider_reference_filename(input_payload, "seedance-reference"),
                duration=duration,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
            )
        else:
            provider_task_id = await context.comet.create_kling_image_to_video_task(
                model_name=provider_model,
                image=provider_image,
                prompt=provider_prompt,
                mode=str(input_payload.get("mode") or "pro"),
                duration=duration,
                callback_url=context.settings.comet_callback_url,
            )
    except Exception as exc:
        logger.exception("Comet video task creation failed")
        if not context.kie.is_configured:
            await _fail_comet_image_task(
                context=context,
                task_id=task_id,
                chat_id=chat_id,
                message_id=status_message_id,
                error_message=f"Не получилось запустить генерацию: {exc}",
            )
            return
        with suppress(Exception):
            if target_message and status_message_id:
                await target_message.bot.edit_message_text(
                    _status_text(
                        "Пробую резервный провайдер",
                        62,
                        "Comet не принял видео-задачу, переключаюсь на KIE.",
                    ),
                    chat_id=chat_id,
                    message_id=status_message_id,
                )
        try:
            provider = "kie"
            provider_model = _resolve_kie_video_provider_model(model_code, context)
            uploaded_reference_url = await context.kie.upload_base64_image(
                KieUploadReference(
                    content=image_bytes,
                    mime_type=image_mime_type,
                    filename=(
                        f"{provider_family}-reference-{user_id}."
                        f"{_mime_extension(image_mime_type)}"
                    ),
                )
            )
            if provider_family == "seedance":
                provider_task_id = await context.kie.create_seedance_video_task(
                    model=provider_model,
                    prompt=provider_prompt,
                    first_frame_url=uploaded_reference_url,
                    duration=duration,
                    aspect_ratio=aspect_ratio,
                    resolution=resolution,
                    callback_url=context.settings.comet_callback_url,
                )
            else:
                provider_task_id = await context.kie.create_kling_image_to_video_task(
                    model=provider_model,
                    prompt=provider_prompt,
                    image_urls=[uploaded_reference_url],
                    mode=str(input_payload.get("mode") or "pro"),
                    duration=duration,
                    callback_url=context.settings.comet_callback_url,
                )
            result_payload = {
                "fallback_from": "comet",
                "comet_error": str(exc),
                "uploaded_reference_urls": [uploaded_reference_url],
            }
        except Exception as fallback_exc:
            logger.exception("KIE video fallback failed")
            await _fail_comet_image_task(
                context=context,
                task_id=task_id,
                chat_id=chat_id,
                message_id=status_message_id,
                error_message=(
                    f"Не получилось запустить генерацию ни через Comet, ни через KIE.\n"
                    f"Comet: {exc}\n"
                    f"KIE: {fallback_exc}"
                ),
            )
            return

    async with session_scope(context.session_factory) as session:
        task = await session.get(GenerationTask, task_id)
        if not task or task.status in {"success", "fail"}:
            return
        task.provider_task_id = provider_task_id
        task.status = "submitted"
        task.result_payload = result_payload
        task.input_payload = {
            **dict(task.input_payload or {}),
            "provider": provider,
            "provider_model": provider_model,
            **({"fallback_from": "comet"} if provider == "kie" else {}),
            **({"uploaded_reference_urls": result_payload["uploaded_reference_urls"]} if result_payload.get("uploaded_reference_urls") else {}),
        }

    await state.clear()
    if target_message:
        if status_message_id:
            with suppress(Exception):
                await target_message.bot.edit_message_text(
                    _status_text(
                        f"Генерация #{task_id} запущена",
                        65,
                        "Я буду обновлять этот статус.",
                    ),
                    chat_id=chat_id,
                    message_id=status_message_id,
                )
        else:
            await target_message.answer(
                f"Генерация #{task_id} запущена.\n"
                "Я пришлю результат сюда, когда он будет готов."
            )
    if callback:
        await callback.answer()


def _resolve_image_provider_model(
    model_code: str,
    model_config: dict[str, Any] | None,
    context: AppContext,
) -> str:
    provider_model = (model_config or {}).get("provider_model")
    if provider_model:
        configured = str(provider_model)
    else:
        configured = model_code
    settings_mapping = {
        "nano-banana": context.settings.comet_image_simple_model,
        "nano-banana-pro": context.settings.comet_image_pro_model,
        "nano-banana-2": context.settings.comet_image_2_model,
    }
    return settings_mapping.get(model_code, configured)


def _resolve_kie_image_provider_model(model_code: str, context: AppContext) -> str:
    settings_mapping = {
        "nano-banana": context.settings.kie_image_simple_model,
        "nano-banana-pro": context.settings.kie_image_pro_model,
        "nano-banana-2": context.settings.kie_image_2_model,
    }
    return settings_mapping.get(model_code, context.settings.kie_image_2_model)


async def _create_kie_image_provider_task(
    *,
    context: AppContext,
    provider_model: str,
    prompt: str,
    input_payload: dict[str, Any],
    reference_images: list[CometImageReference],
    max_reference_images: int,
) -> tuple[str, list[str]]:
    uploaded_urls: list[str] = []
    for index, image in enumerate(_limit_reference_images(reference_images, max_reference_images), start=1):
        uploaded_urls.append(
            await context.kie.upload_base64_image(
                KieUploadReference(
                    content=image.content,
                    mime_type=image.mime_type,
                    filename=f"reference-{index}.{_mime_extension(image.mime_type)}",
                )
            )
        )
    provider_task_id = await context.kie.create_image_task(
        model=provider_model,
        prompt=prompt,
        image_urls=uploaded_urls,
        aspect_ratio=str(input_payload.get("aspect_ratio") or "auto"),
        resolution=_normalize_image_resolution(input_payload.get("resolution")),
        output_format=str(input_payload.get("output_format") or "png"),
        callback_url=context.settings.comet_callback_url,
    )
    return provider_task_id, uploaded_urls


def _limit_reference_images(
    reference_images: list[CometImageReference],
    max_reference_images: int,
) -> list[CometImageReference]:
    return reference_images[: _normalize_max_images(max_reference_images)]


def _resolve_video_provider_model(
    model_code: str,
    model_config: dict[str, Any] | None,
    context: AppContext,
) -> str:
    provider_model = (model_config or {}).get("provider_model")
    configured = str(provider_model) if provider_model else model_code
    settings_mapping = {
        "kling-2.6/video": context.settings.comet_kling_2_6_model,
        "kling-3.0/video": context.settings.comet_kling_3_0_model,
        "seedance-2/video": context.settings.comet_seedance_2_model,
    }
    return settings_mapping.get(model_code, configured)


def _resolve_kie_video_provider_model(model_code: str, context: AppContext) -> str:
    settings_mapping = {
        "kling-2.6/video": context.settings.kie_kling_2_6_model,
        "kling-3.0/video": context.settings.kie_kling_3_0_model,
        "seedance-2/video": context.settings.kie_seedance_2_model,
    }
    return settings_mapping.get(model_code, context.settings.kie_kling_3_0_model)


def _resolve_kie_motion_control_provider_model(model_code: str, context: AppContext) -> str:
    settings_mapping = {
        "kling-2.6/video": context.settings.kie_kling_2_6_motion_control_model,
        "kling-3.0/video": context.settings.kie_kling_3_0_motion_control_model,
    }
    return settings_mapping.get(model_code, context.settings.kie_kling_3_0_motion_control_model)


def _resolve_video_provider_family(model_code: str, model_config: dict[str, Any] | None) -> str:
    configured = str((model_config or {}).get("provider_family") or "").strip().lower()
    if configured:
        return configured
    if _is_seedance_model_code(model_code):
        return "seedance"
    if model_code.startswith("kling"):
        return "kling"
    return "video"


def _is_seedance_model_code(model_code: str) -> bool:
    return model_code.startswith("seedance")


def _normalize_motion_control_character_orientation(value: Any) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in {"image", "video"}:
        return normalized
    return MOTION_CONTROL_CHARACTER_ORIENTATION


def _video_prompt_for_provider(input_payload: dict[str, Any]) -> str:
    prompt = str(input_payload.get("prompt") or "").strip()
    if prompt:
        return prompt
    return "Animate the reference image with subtle cinematic motion."


def _video_aspect_ratio(input_payload: dict[str, Any], model_config: dict[str, Any] | None) -> str:
    value = str(input_payload.get("aspect_ratio") or "").strip()
    allowed = (model_config or {}).get("aspect_ratios")
    if isinstance(allowed, list) and value in allowed:
        return value
    default = str((model_config or {}).get("default_aspect_ratio") or DEFAULT_VIDEO_ASPECT_RATIO)
    if isinstance(allowed, list) and default in allowed:
        return default
    return _normalize_video_aspect(value)


def _video_resolution(input_payload: dict[str, Any], model_config: dict[str, Any] | None) -> str:
    value = str(input_payload.get("resolution") or "").strip()
    allowed = (model_config or {}).get("resolutions")
    if isinstance(allowed, list) and value in allowed:
        return value
    default = str((model_config or {}).get("default_resolution") or DEFAULT_VIDEO_RESOLUTION)
    if isinstance(allowed, list) and default in allowed:
        return default
    return value or DEFAULT_VIDEO_RESOLUTION


def _normalize_video_aspect(value: Any) -> str:
    aspect_ratio = str(value or DEFAULT_VIDEO_ASPECT_RATIO)
    if aspect_ratio in VIDEO_ASPECT_RATIOS:
        return aspect_ratio
    return DEFAULT_VIDEO_ASPECT_RATIO


def _provider_reference_filename(input_payload: dict[str, Any], fallback_stem: str) -> str:
    reference = input_payload.get("reference")
    if isinstance(reference, dict):
        filename = str(reference.get("filename") or "").strip()
        if filename:
            return filename
        mime_type = str(reference.get("mime_type") or "image/jpeg")
    else:
        mime_type = "image/jpeg"
    return f"{fallback_stem}.{_mime_extension(mime_type)}"


def _output_mime_type(input_payload: dict[str, Any]) -> str | None:
    output_format = str(input_payload.get("output_format") or "").lower()
    if output_format in {"jpg", "jpeg"}:
        return "image/jpeg"
    if output_format == "png":
        return "image/png"
    if output_format == "webp":
        return "image/webp"
    return None


def _charge_details_text(
    user: User,
    charged_credits: int,
    has_unlimited: bool,
    credit_type: str,
) -> str:
    if user.is_admin:
        return "Админ-доступ: кредиты не списаны."
    if has_unlimited:
        return "Безлимит активен, кредиты не списаны."
    if charged_credits <= 0:
        return "Генерация без списания кредитов."
    balance = user_credit_balance(user, credit_type)
    return (
        f"Списано: {_credit_amount_text(charged_credits, credit_type)}. "
        f"Остаток: {_credit_amount_text(balance, credit_type)}."
    )


def _credit_amount_text(value: int, credit_type: str | None) -> str:
    normalized = str(credit_type or "").strip().lower()
    if normalized in {"photo", "image"}:
        return f"{value} фото-кредитов"
    if normalized in {"video", "motion"}:
        return f"{value} видео-кредитов"
    return f"{value} кредитов"


def _motion_control_cost_credits(
    *,
    price_per_second: int,
    billable_seconds: int,
    free_generation: bool = False,
) -> int:
    if free_generation or price_per_second <= 0 or billable_seconds <= 0:
        return 0
    return price_per_second * billable_seconds


async def _send_comet_image_result(
    *,
    bot: Bot | None,
    chat_id: int,
    task_id: int,
    result: CometImageResult,
) -> SentImageFiles:
    if not bot:
        raise RuntimeError("Telegram bot is not available")
    await bot.send_message(chat_id, f"Генерация #{task_id} готова. Ниже превью и исходник.")
    preview_file_ids: list[str] = []
    source_file_ids: list[str] = []
    for index, image in enumerate(result.images[:5], start=1):
        filename = f"generation-{task_id}-{index}.{_image_extension(image)}"
        preview_content, preview_filename = _telegram_preview_payload(image, filename)
        try:
            sent = await bot.send_photo(
                chat_id,
                BufferedInputFile(
                    preview_content,
                    filename=preview_filename,
                ),
                caption=f"Превью #{index}",
                reply_markup=_image_result_keyboard(task_id) if index == 1 else None,
            )
        except Exception:
            logger.warning("Telegram preview upload failed, retrying with compact JPEG", exc_info=True)
            preview_content, preview_filename = _telegram_preview_payload(image, filename, force_compact=True)
            sent = await bot.send_photo(
                chat_id,
                BufferedInputFile(
                    preview_content,
                    filename=preview_filename,
                ),
                caption=f"Превью #{index}",
                reply_markup=_image_result_keyboard(task_id) if index == 1 else None,
            )
        if sent.photo:
            preview_file_ids.append(sent.photo[-1].file_id)
        source = await bot.send_document(
            chat_id,
            BufferedInputFile(image.content, filename=filename),
            caption=f"Исходник #{index}",
        )
        if source.document:
            source_file_ids.append(source.document.file_id)
    if not preview_file_ids or not source_file_ids:
        raise RuntimeError("Telegram did not return file ids for generated images")
    return SentImageFiles(preview_file_ids=preview_file_ids, source_file_ids=source_file_ids)


def _image_result_keyboard(task_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Еще вариант (тот же реф)", callback_data=f"image:again:{task_id}")
    builder.button(text="В ленту", callback_data=f"feed:publish:confirm:{task_id}")
    builder.button(text="Главное меню", callback_data="menu:main")
    builder.adjust(1, 1, 1)
    return builder.as_markup()


def _callback_int(value: str | None, prefix: str, *, default: int) -> int:
    if not value or not value.startswith(prefix):
        return default
    try:
        return int(value.removeprefix(prefix))
    except ValueError:
        return default


def _telegram_preview_payload(
    image: CometGeneratedImage,
    filename: str,
    *,
    force_compact: bool = False,
) -> tuple[bytes, str]:
    if not force_compact and len(image.content) <= TELEGRAM_PHOTO_MAX_BYTES:
        return image.content, filename

    with Image.open(BytesIO(image.content)) as source:
        source = ImageOps.exif_transpose(source)
        last_preview = image.content
        for max_side, quality in TELEGRAM_PREVIEW_VARIANTS:
            preview = source.copy()
            preview.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            preview = _image_to_rgb(preview)
            buffer = BytesIO()
            preview.save(buffer, format="JPEG", quality=quality, optimize=True)
            last_preview = buffer.getvalue()
            if len(last_preview) <= TELEGRAM_PHOTO_MAX_BYTES:
                break
    return last_preview, _preview_filename(filename)


def _image_to_rgb(image: Image.Image) -> Image.Image:
    if image.mode == "RGB":
        return image
    if "A" in image.getbands() or image.mode == "P":
        rgba = image.convert("RGBA")
        background = Image.new("RGB", rgba.size, (255, 255, 255))
        background.paste(rgba, mask=rgba.getchannel("A"))
        return background
    return image.convert("RGB")


def _preview_filename(filename: str) -> str:
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    return f"{stem}-preview.jpg"


async def _fail_comet_image_task(
    *,
    context: AppContext,
    task_id: int,
    chat_id: int,
    message_id: int | None,
    error_message: str,
) -> None:
    async with session_scope(context.session_factory) as session:
        task = await session.get(GenerationTask, task_id)
        if not task:
            return
        if task.status in {"success", "fail"}:
            return
        task_provider = str((task.input_payload or {}).get("provider") or "comet")
        task.status = "fail"
        task.error_message = error_message
        task.result_payload = {"provider": task_provider, "error": error_message}
        await refund_task_credits(session, task=task)

    if context.bot and message_id:
        with suppress(Exception):
            await context.bot.edit_message_text(
                _status_text("Ошибка генерации", 100, f"{error_message}\nСписанные кредиты возвращены."),
                chat_id=chat_id,
                message_id=message_id,
            )
    elif context.bot:
        with suppress(Exception):
            await context.bot.send_message(
                chat_id,
                f"Генерация #{task_id} завершилась ошибкой.\n"
                f"{error_message}\n"
                "Списанные кредиты возвращены.",
            )


def _image_extension(image: CometGeneratedImage) -> str:
    if image.mime_type == "image/jpeg":
        return "jpg"
    if image.mime_type == "image/webp":
        return "webp"
    return "png"


def _mime_extension(mime_type: str) -> str:
    if mime_type in {"image/jpeg", "image/jpg"}:
        return "jpg"
    if mime_type == "image/webp":
        return "webp"
    if mime_type in {"video/mp4", "video/mpeg"}:
        return "mp4"
    if mime_type in {"video/quicktime", "video/mov"}:
        return "mov"
    if mime_type in {"video/x-matroska", "video/matroska"}:
        return "mkv"
    return "png"


def _normalize_motion_image_mime_type(mime_type: str | None, filename: str | None) -> str | None:
    normalized = _clean_mime_type(mime_type)
    if normalized in MOTION_CONTROL_IMAGE_MIME_TYPES:
        return "image/jpeg" if normalized == "image/jpg" else normalized
    if normalized.startswith("image/"):
        return None
    return MOTION_CONTROL_IMAGE_EXTENSION_MIME_TYPES.get(_file_suffix(filename))


def _normalize_motion_video_mime_type(mime_type: str | None, filename: str | None) -> str | None:
    normalized = _clean_mime_type(mime_type)
    if normalized in MOTION_CONTROL_VIDEO_MIME_TYPES:
        return normalized
    if normalized in {"video/mov", "video/quicktime"}:
        return "video/quicktime"
    if normalized in {"video/matroska", "video/x-matroska"}:
        return "video/x-matroska"
    if normalized.startswith("video/"):
        return None
    return MOTION_CONTROL_VIDEO_EXTENSION_MIME_TYPES.get(_file_suffix(filename))


def _clean_mime_type(mime_type: str | None) -> str:
    return str(mime_type or "").split(";", 1)[0].strip().lower()


def _file_suffix(filename: str | None) -> str:
    value = str(filename or "").strip().lower()
    if "." not in value:
        return ""
    return f".{value.rsplit('.', 1)[-1]}"


async def _download_telegram_file(*, bot: Bot, file_id: str) -> bytes:
    file = await bot.get_file(file_id)
    buffer = BytesIO()
    await bot.download_file(file.file_path, destination=buffer)
    return buffer.getvalue()


def _extract_image_file(message: Message) -> tuple[str | None, str, str | None, int | None]:
    if message.photo:
        photo = message.photo[-1]
        return photo.file_id, f"telegram-photo-{photo.file_unique_id}.jpg", "image/jpeg", photo.file_size
    if message.document and (message.document.mime_type or "").startswith("image/"):
        return (
            message.document.file_id,
            message.document.file_name or f"telegram-image-{message.document.file_unique_id}",
            message.document.mime_type,
            message.document.file_size,
        )
    return None, "image", None, None


def _extract_video_file(message: Message) -> tuple[str | None, str, str | None, int | None, float | None]:
    if message.video:
        video = message.video
        return (
            video.file_id,
            video.file_name or f"telegram-video-{video.file_unique_id}.mp4",
            video.mime_type or "video/mp4",
            video.file_size,
            float(video.duration) if video.duration else None,
        )
    if message.document and (message.document.mime_type or "").startswith("video/"):
        document = message.document
        return (
            document.file_id,
            document.file_name or f"telegram-video-{document.file_unique_id}.{_mime_extension(document.mime_type or 'video/mp4')}",
            document.mime_type,
            document.file_size,
            None,
        )
    return None, "video", None, None, None


async def _probe_video_duration_seconds(content: bytes, mime_type: str | None) -> float:
    suffix = f".{_mime_extension(mime_type or 'video/mp4')}"
    with tempfile.NamedTemporaryFile(suffix=suffix) as handle:
        handle.write(content)
        handle.flush()
        process = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            handle.name,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(stderr.decode("utf-8", "ignore")[:500] or "ffprobe failed")
    return float(stdout.decode("utf-8", "ignore").strip())


def _billable_motion_seconds(duration_seconds: float | int | None) -> int:
    if not duration_seconds:
        return 0
    return max(0, math.ceil(float(duration_seconds)))


def _status_text(title: str, percent: int, details: str | None = None) -> str:
    percent = max(0, min(100, percent))
    filled = max(0, min(5, round(percent / 20)))
    bar = "▰" * filled + "▱" * (5 - filled)
    text = f"{title}\n{bar} {percent}%"
    if details:
        text += f"\n\n{escape(str(details))}"
    return text


def setup(dispatcher: Dispatcher, context: AppContext) -> None:
    dispatcher.include_router(router)
