from __future__ import annotations

import asyncio
import json
import logging
import tempfile
from contextlib import suppress
from io import BytesIO
from pathlib import Path
from typing import Any, Awaitable, Callable

import httpx
from aiogram import BaseMiddleware, Dispatcher, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, TelegramObject
from aiogram.utils.keyboard import InlineKeyboardBuilder
from PIL import Image

from app.config import Settings
from app.context import AppContext
from app.db import session_scope
from app.models import GenerationTask, User
from app.repositories import (
    get_model,
    model_credit_type,
    spend_user_credits,
    user_credit_balance,
    user_generates_for_free,
)
from app.services.comet import CometImageReference
from app.services.generation_catalog import (
    DEFAULT_IMAGE_ASPECT_RATIO,
    DEFAULT_IMAGE_RESOLUTION,
    DEFAULT_MINI_APP_IMAGE_MODEL,
    GEMINI_FLASH_ASPECT_RATIOS,
    GEMINI_FLASH_LITE_ASPECT_RATIOS,
    GEMINI_PRO_ASPECT_RATIOS,
    IMAGE_ASPECT_RATIOS,
    IMAGE_RESOLUTIONS,
    SEEDANCE_ASPECT_RATIOS,
    SEEDANCE_DURATIONS,
    model_default_config,
)

logger = logging.getLogger(__name__)
router = Router(name="model-contracts")

_ORIGINAL_KIE_CREATE_IMAGE_TASK: Any | None = None
_ORIGINAL_IMAGE_SETTINGS_KEYBOARD: Any | None = None
_ORIGINAL_IMAGE_SETTINGS_TEXT: Any | None = None
_ORIGINAL_SEND_IMAGE_REQUEST_SCREEN: Any | None = None
_ORIGINAL_SUBMIT_IMAGE_TASK_COMMON: Any | None = None
_ORIGINAL_CREATE_COMET_IMAGE_TASK: Any | None = None
_ORIGINAL_SUBMIT_MOTION_TASK: Any | None = None
_ORIGINAL_SUBMIT_MOTION_CONTROL_TASK: Any | None = None


def contract_for(model_code: str) -> dict[str, Any]:
    return model_default_config(str(model_code or ""))


def _allowed(contract: dict[str, Any], key: str, fallback: list[str]) -> list[str]:
    value = contract.get(key)
    if isinstance(value, list) and value:
        return [str(item) for item in value]
    return list(fallback)


def normalize_model_value(
    model_code: str,
    key: str,
    value: Any,
    *,
    default_key: str,
    fallback: str,
) -> str:
    contract = contract_for(model_code)
    allowed = _allowed(contract, key, [fallback])
    normalized = str(value or "").strip()
    if normalized in allowed:
        return normalized
    configured_default = str(contract.get(default_key) or "").strip()
    if configured_default in allowed:
        return configured_default
    return allowed[0]


def image_resolution(model_code: str, value: Any) -> str:
    return normalize_model_value(
        model_code,
        "resolutions",
        value,
        default_key="default_resolution",
        fallback=DEFAULT_IMAGE_RESOLUTION,
    )


def image_aspect_ratio(model_code: str, value: Any) -> str:
    return normalize_model_value(
        model_code,
        "aspect_ratios",
        value,
        default_key="default_aspect_ratio",
        fallback=DEFAULT_IMAGE_ASPECT_RATIO,
    )


def image_output_format(model_code: str, value: Any) -> str:
    return normalize_model_value(
        model_code,
        "output_formats",
        value,
        default_key="default_output_format",
        fallback="png",
    )


def seedance_duration(value: Any) -> str:
    normalized = str(value or "").strip()
    return normalized if normalized in SEEDANCE_DURATIONS else "5"


def seedance_resolution(value: Any) -> str:
    return normalize_model_value(
        "seedance-2/video",
        "resolutions",
        value,
        default_key="default_resolution",
        fallback="720p",
    )


def seedance_aspect_ratio(value: Any) -> str:
    return normalize_model_value(
        "seedance-2/video",
        "aspect_ratios",
        value,
        default_key="default_aspect_ratio",
        fallback="16:9",
    )


def install_model_repository_contracts() -> None:
    """Make existing database rows converge to the reviewed provider contract."""

    from app import repositories

    def sync_default_model_config(model: Any, defaults: dict[str, Any]) -> None:
        default_config = defaults.get("config")
        if not isinstance(default_config, dict):
            return
        current = dict(model.config or {})
        current.update(default_config)
        model.config = current
        model.description = defaults.get("description") or model.description

    repositories._sync_default_model_config = sync_default_model_config


def install_kie_image_contract() -> None:
    """KIE Lite uses image_urls and does not accept full-model-only fields."""

    from app.services.kie import KieApiError, KieClient

    global _ORIGINAL_KIE_CREATE_IMAGE_TASK
    if _ORIGINAL_KIE_CREATE_IMAGE_TASK is None:
        _ORIGINAL_KIE_CREATE_IMAGE_TASK = KieClient.create_image_task
    original = _ORIGINAL_KIE_CREATE_IMAGE_TASK

    async def create_image_task(
        self: Any,
        *,
        model: str,
        prompt: str,
        image_urls: list[str] | None = None,
        aspect_ratio: str | None = None,
        resolution: str | None = None,
        output_format: str | None = None,
        callback_url: str | None = None,
    ) -> str:
        if model != "nano-banana-2-lite":
            return await original(
                self,
                model=model,
                prompt=prompt,
                image_urls=image_urls,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
                output_format=output_format,
                callback_url=callback_url,
            )

        allowed_aspects = ["auto", *GEMINI_FLASH_LITE_ASPECT_RATIOS]
        selected_aspect = str(aspect_ratio or "auto")
        if selected_aspect not in allowed_aspects:
            selected_aspect = "auto"
        payload: dict[str, Any] = {
            "model": model,
            "input": {
                "prompt": prompt,
                "image_urls": list(image_urls or [])[:10],
                "aspect_ratio": selected_aspect,
            },
        }
        if callback_url:
            payload["callBackUrl"] = callback_url
        async with httpx.AsyncClient(base_url=self.base_url, timeout=self.timeout) as client:
            response = await client.post(
                "/api/v1/jobs/createTask",
                headers=self._headers(),
                json=payload,
            )
        data = self._decode_response(response, provider="KIE")
        task_data = data.get("data") if isinstance(data.get("data"), dict) else {}
        task_id = task_data.get("taskId") or task_data.get("task_id")
        if not task_id:
            raise KieApiError(f"KIE Lite response does not contain taskId: {data}")
        return str(task_id)

    KieClient.create_image_task = create_image_task


class ModelContractWebAppMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        payload: dict[str, Any] = {}
        web_app_data = getattr(getattr(event, "web_app_data", None), "data", None)
        if web_app_data:
            with suppress(json.JSONDecodeError):
                decoded = json.loads(web_app_data)
                if isinstance(decoded, dict):
                    payload = decoded
        result = await handler(event, data)
        state = data.get("state")
        if not payload or not isinstance(state, FSMContext):
            return result

        model_code = str(payload.get("model_code") or "")
        updates: dict[str, Any] = {}
        if model_code in {"nano-banana", "nano-banana-pro", "nano-banana-2"}:
            updates.update(
                resolution=image_resolution(model_code, payload.get("resolution")),
                aspect_ratio=image_aspect_ratio(model_code, payload.get("aspect_ratio")),
                output_format=image_output_format(model_code, payload.get("output_format")),
            )
        elif model_code == "seedance-2/video":
            updates.update(
                duration=seedance_duration(payload.get("duration")),
                aspect_ratio=seedance_aspect_ratio(payload.get("aspect_ratio")),
                resolution=seedance_resolution(payload.get("resolution")),
            )
        if updates:
            await state.update_data(**updates)

        if isinstance(event, Message) and model_code in {
            "nano-banana",
            "nano-banana-pro",
            "nano-banana-2",
        }:
            await event.answer(
                "Референсы необязательны: можно продолжить только с промптом.",
                reply_markup=_single_callback_keyboard(
                    "Продолжить без референса",
                    "image:no_reference",
                ),
            )
        if isinstance(event, Message) and model_code == "seedance-2/video":
            await event.answer(
                "Seedance поддерживает text-to-video без стартового изображения.",
                reply_markup=_single_callback_keyboard(
                    "Продолжить без изображения",
                    "motion:no_reference",
                ),
            )
        return result


class ModelContractCallbackMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        result = await handler(event, data)
        if not isinstance(event, CallbackQuery) or not event.message:
            return result
        if event.data == "gen:model:seedance-2/video":
            await event.message.answer(
                "Стартовое изображение необязательно.",
                reply_markup=_single_callback_keyboard(
                    "Text-to-video без изображения",
                    "motion:no_reference",
                ),
            )
        return result


def _single_callback_keyboard(text: str, callback_data: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=text, callback_data=callback_data)
    builder.adjust(1)
    return builder.as_markup()


@router.callback_query(F.data == "image:no_reference")
async def image_without_reference(
    callback: CallbackQuery,
    context: AppContext,
    state: FSMContext,
) -> None:
    from app.plugins.common import ensure_user_for_callback
    from app.plugins.generation import plugin as generation

    user = await ensure_user_for_callback(callback, context)
    data = await state.get_data()
    model_code = str(data.get("model_code") or DEFAULT_MINI_APP_IMAGE_MODEL)
    async with session_scope(context.session_factory) as session:
        model = await get_model(session, model_code)
    if not model or model.category != "image" or not model.is_enabled:
        await callback.answer("Модель недоступна", show_alert=True)
        return
    contract = dict(model.config or contract_for(model_code))
    if int(contract.get("min_images") or 0) > 0:
        await callback.answer("Для этой модели нужен референс", show_alert=True)
        return
    limits = generation._generation_limits_payload(user, model)
    await state.set_state(generation.ImageFlow.settings)
    await state.update_data(
        model_code=model_code,
        image_references=[],
        image_limits=limits,
        resolution=image_resolution(model_code, data.get("resolution")),
        aspect_ratio=image_aspect_ratio(model_code, data.get("aspect_ratio")),
        output_format=image_output_format(model_code, data.get("output_format")),
        prompt=str(data.get("prompt") or ""),
    )
    current = await state.get_data()
    if callback.message:
        await callback.message.answer(
            generation._image_settings_text(current),
            reply_markup=generation._image_settings_keyboard(current),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("image:format:"))
async def select_image_output_format(callback: CallbackQuery, state: FSMContext) -> None:
    selected = callback.data.removeprefix("image:format:")
    data = await state.get_data()
    model_code = str(data.get("model_code") or DEFAULT_MINI_APP_IMAGE_MODEL)
    allowed = _allowed(contract_for(model_code), "output_formats", ["png"])
    if selected not in allowed:
        await callback.answer("Такого формата нет", show_alert=True)
        return
    await state.update_data(output_format=selected)
    if callback.message:
        data = await state.get_data()
        with suppress(Exception):
            await callback.message.edit_text(
                _image_settings_text(data),
                reply_markup=_image_settings_keyboard(data),
            )
    await callback.answer()


@router.callback_query(F.data == "motion:no_reference")
async def seedance_without_reference(callback: CallbackQuery, state: FSMContext) -> None:
    from app.plugins.generation import plugin as generation

    data = await state.get_data()
    model_code = str(data.get("model_code") or "seedance-2/video")
    if model_code != "seedance-2/video":
        await callback.answer("Этот режим доступен только для Seedance", show_alert=True)
        return
    if str(data.get("prompt") or "").strip():
        await state.set_state(generation.MotionFlow.duration)
        if callback.message:
            await callback.message.answer(
                "Длительность Seedance 2.0:",
                reply_markup=generation.options_keyboard(
                    "motion:duration",
                    SEEDANCE_DURATIONS,
                    back="menu:motion",
                ),
            )
    else:
        await state.set_state(generation.MotionFlow.prompt)
        if callback.message:
            await callback.message.answer(
                "Напишите промпт для text-to-video.",
                reply_markup=generation.navigation_keyboard(back_callback="menu:motion"),
            )
    await callback.answer()


def _option_label(value: str, selected: str) -> str:
    return f"[{value}]" if value == selected else value


def _image_settings_keyboard(data: dict[str, Any]) -> InlineKeyboardMarkup:
    from app.plugins.generation import plugin as generation

    model_code = str(data.get("model_code") or DEFAULT_MINI_APP_IMAGE_MODEL)
    contract = contract_for(model_code)
    resolutions = _allowed(contract, "resolutions", [DEFAULT_IMAGE_RESOLUTION])
    aspects = _allowed(contract, "aspect_ratios", [DEFAULT_IMAGE_ASPECT_RATIO])
    formats = _allowed(contract, "output_formats", ["png"])
    current_resolution = image_resolution(model_code, data.get("resolution"))
    current_aspect = image_aspect_ratio(model_code, data.get("aspect_ratio"))
    current_format = image_output_format(model_code, data.get("output_format"))

    builder = InlineKeyboardBuilder()
    for value in resolutions:
        builder.button(
            text=_option_label(value, current_resolution),
            callback_data=f"image:resolution:{value}",
        )
    for value in aspects:
        builder.button(
            text=_option_label(value, current_aspect),
            callback_data=f"image:aspect:{value}",
        )
    for value in formats:
        builder.button(
            text=_option_label(value.upper(), current_format.upper()),
            callback_data=f"image:format:{value}",
        )
    rows: list[int] = []
    rows.extend(_chunk_sizes(len(resolutions), 4))
    rows.extend(_chunk_sizes(len(aspects), 3))
    rows.extend(_chunk_sizes(len(formats), 2))
    if str(data.get("prompt") or "").strip():
        builder.button(text="Запустить", callback_data="image:submit")
        rows.append(1)
    nav_count = generation.add_navigation_buttons(builder, back_callback="menu:image")
    rows.append(nav_count)
    builder.adjust(*rows)
    return builder.as_markup()


def _image_settings_text(data: dict[str, Any]) -> str:
    from html import escape
    from app.plugins.generation import plugin as generation

    model_code = str(data.get("model_code") or DEFAULT_MINI_APP_IMAGE_MODEL)
    resolution = image_resolution(model_code, data.get("resolution"))
    aspect = image_aspect_ratio(model_code, data.get("aspect_ratio"))
    output_format = image_output_format(model_code, data.get("output_format"))
    prompt = str(data.get("prompt") or "").strip()
    references_count = len(generation._image_reference_items(data))
    max_references = generation._max_image_references_from_limits(data.get("image_limits"))
    prompt_text = escape(generation._shorten(prompt, 900)) if prompt else "не задан"
    instruction = (
        "Нажмите «Запустить» или отправьте новый промпт текстом."
        if prompt
        else "Отправьте промпт текстом — после этого генерация запустится."
    )
    if references_count < max_references:
        instruction = (
            f"Референсы необязательны; загружено {references_count}/{max_references}. "
            f"{instruction}"
        )
    return (
        "Настройки генерации\n\n"
        f"Референсы: <b>{references_count}/{max_references}</b>\n"
        f"Качество: <b>{escape(resolution)}</b>\n"
        f"Формат кадра: <b>{escape(aspect)}</b>\n"
        f"Файл: <b>{escape(output_format.upper())}</b>\n"
        f"{generation._image_limits_text(data.get('image_limits'))}"
        f"Промпт:\n{prompt_text}\n\n"
        f"{instruction}"
    )


def _chunk_sizes(count: int, width: int) -> list[int]:
    rows: list[int] = []
    remaining = count
    while remaining > 0:
        rows.append(min(width, remaining))
        remaining -= width
    return rows


async def _send_image_request_screen(
    message: Message,
    context: AppContext,
    state: FSMContext,
) -> None:
    assert _ORIGINAL_SEND_IMAGE_REQUEST_SCREEN is not None
    await _ORIGINAL_SEND_IMAGE_REQUEST_SCREEN(message, context, state)
    await message.answer(
        "Все три image-модели поддерживают text-to-image без референса.",
        reply_markup=_single_callback_keyboard(
            "Продолжить без референса",
            "image:no_reference",
        ),
    )


def _generation_limits_payload(user: User, model: Any) -> dict[str, Any]:
    from app.plugins.generation import plugin as generation

    payload = generation._generation_limits_payload_original(user, model)
    contract = dict(getattr(model, "config", None) or contract_for(model.code))
    payload.update(
        aspect_ratios=_allowed(contract, "aspect_ratios", [DEFAULT_IMAGE_ASPECT_RATIO]),
        resolutions=_allowed(contract, "resolutions", [DEFAULT_IMAGE_RESOLUTION]),
        output_formats=_allowed(contract, "output_formats", ["png"]),
        default_aspect_ratio=str(contract.get("default_aspect_ratio") or DEFAULT_IMAGE_ASPECT_RATIO),
        default_resolution=str(contract.get("default_resolution") or DEFAULT_IMAGE_RESOLUTION),
        default_output_format=str(contract.get("default_output_format") or "png"),
        min_images=int(contract.get("min_images") or 0),
    )
    return payload


async def _submit_image_task_common(*args: Any, **kwargs: Any) -> None:
    assert _ORIGINAL_SUBMIT_IMAGE_TASK_COMMON is not None
    state: FSMContext = kwargs["state"]
    data = await state.get_data()
    from app.plugins.generation import plugin as generation

    if generation._image_reference_items(data):
        await _ORIGINAL_SUBMIT_IMAGE_TASK_COMMON(*args, **kwargs)
        return

    model_code = str(data.get("model_code") or DEFAULT_MINI_APP_IMAGE_MODEL)
    contract = contract_for(model_code)
    if int(contract.get("min_images") or 0) > 0:
        await _ORIGINAL_SUBMIT_IMAGE_TASK_COMMON(*args, **kwargs)
        return
    prompt = str(data.get("prompt") or "").strip()
    if not prompt:
        await generation._notify_image_submit_error(
            kwargs.get("message"),
            kwargs.get("callback"),
            "Сначала напишите промпт текстом",
        )
        return
    input_payload: dict[str, Any] = {
        "prompt": prompt,
        "aspect_ratio": image_aspect_ratio(model_code, data.get("aspect_ratio")),
        "resolution": image_resolution(model_code, data.get("resolution")),
        "output_format": image_output_format(model_code, data.get("output_format")),
        "references": [],
        "reference_count": 0,
    }
    if data.get("source_feed_task_id"):
        input_payload["source_feed_task_id"] = int(data["source_feed_task_id"])
    await generation._create_comet_image_task(
        message=kwargs.get("message"),
        callback=kwargs.get("callback"),
        context=kwargs["context"],
        state=state,
        user_id=kwargs["user_id"],
        chat_id=kwargs["chat_id"],
        model_code=model_code,
        prompt=prompt,
        input_payload=input_payload,
        reference_images=[],
    )


async def _create_comet_image_task(*args: Any, **kwargs: Any) -> None:
    assert _ORIGINAL_CREATE_COMET_IMAGE_TASK is not None
    model_code = str(kwargs["model_code"])
    state: FSMContext = kwargs["state"]
    data = await state.get_data()
    payload = dict(kwargs["input_payload"])
    payload["aspect_ratio"] = image_aspect_ratio(model_code, payload.get("aspect_ratio"))
    payload["resolution"] = image_resolution(model_code, payload.get("resolution"))
    payload["output_format"] = image_output_format(
        model_code,
        payload.get("output_format") or data.get("output_format"),
    )
    max_images = int(contract_for(model_code).get("max_images") or 1)
    kwargs["reference_images"] = list(kwargs.get("reference_images") or [])[:max_images]
    kwargs["input_payload"] = payload
    await _ORIGINAL_CREATE_COMET_IMAGE_TASK(*args, **kwargs)


async def _submit_motion_task(
    callback: CallbackQuery,
    context: AppContext,
    state: FSMContext,
    bot: Any,
) -> None:
    assert _ORIGINAL_SUBMIT_MOTION_TASK is not None
    data = await state.get_data()
    if data.get("image_file_id") or str(data.get("model_code") or "") != "seedance-2/video":
        await _ORIGINAL_SUBMIT_MOTION_TASK(callback, context, state, bot)
        return
    await _submit_seedance_text_task(callback, context, state)


async def _submit_seedance_text_task(
    callback: CallbackQuery,
    context: AppContext,
    state: FSMContext,
) -> None:
    from app.plugins.common import ensure_user_for_callback
    from app.plugins.generation import plugin as generation

    user = await ensure_user_for_callback(callback, context)
    data = await state.get_data()
    prompt = generation._video_prompt_for_provider(data)
    duration = seedance_duration(data.get("duration"))
    aspect_ratio = seedance_aspect_ratio(data.get("aspect_ratio"))
    resolution = seedance_resolution(data.get("resolution"))
    status_message = None
    if callback.message:
        status_message = await callback.message.answer(generation._status_text("Запускаю Seedance", 55))

    async with session_scope(context.session_factory) as session:
        fresh_user = await session.get(User, user.id, with_for_update=True)
        model = await get_model(session, "seedance-2/video")
        if not fresh_user or not model or not model.is_enabled:
            await callback.answer("Модель недоступна", show_alert=True)
            return
        credit_type = model_credit_type(model)
        free_generation = user_generates_for_free(fresh_user)
        charged = 0 if model.price_credits <= 0 or free_generation else int(model.price_credits)
        if user_credit_balance(fresh_user, credit_type) < charged:
            await callback.answer("Недостаточно видео-кредитов", show_alert=True)
            return
        credit_spend = spend_user_credits(fresh_user, credit_type=credit_type, amount=charged)
        if credit_spend is None:
            await callback.answer("Недостаточно видео-кредитов", show_alert=True)
            return
        provider_model = generation._resolve_video_provider_model(model.code, model.config, context)
        task = GenerationTask(
            user_id=fresh_user.id,
            model_code=model.code,
            status="submitting",
            prompt=prompt,
            input_payload={
                "prompt": prompt,
                "provider": "comet",
                "provider_family": "seedance",
                "provider_model": provider_model,
                "duration": duration,
                "aspect_ratio": aspect_ratio,
                "resolution": resolution,
                "reference_count": 0,
                "credit_type": credit_type,
                "credit_spend": credit_spend,
            },
            result_payload={},
            cost_credits=charged,
            chat_id=callback.message.chat.id if callback.message else fresh_user.telegram_id,
            message_id=status_message.message_id if status_message else None,
        )
        session.add(task)
        await session.flush()
        task_id = task.id

    provider = "comet"
    result_payload: dict[str, Any] = {}
    try:
        provider_task_id = await context.comet.create_seedance_video_task(
            model=provider_model,
            prompt=prompt,
            image=None,
            duration=duration,
            aspect_ratio=aspect_ratio,
            resolution=resolution,
        )
    except Exception as exc:
        logger.exception("Comet Seedance text-to-video creation failed")
        if not context.kie.is_configured:
            await generation._fail_comet_image_task(
                context=context,
                task_id=task_id,
                chat_id=task.chat_id,
                message_id=task.message_id,
                error_message=str(exc),
            )
            await callback.answer()
            return
        try:
            provider = "kie"
            provider_model = context.settings.kie_seedance_2_model
            provider_task_id = await context.kie.create_seedance_video_task(
                model=provider_model,
                prompt=prompt,
                duration=duration,
                aspect_ratio=aspect_ratio,
                resolution=resolution,
                callback_url=context.settings.comet_callback_url,
            )
            result_payload = {"fallback_from": "comet", "comet_error": str(exc)}
        except Exception as fallback_exc:
            logger.exception("KIE Seedance text-to-video fallback failed")
            await generation._fail_comet_image_task(
                context=context,
                task_id=task_id,
                chat_id=task.chat_id,
                message_id=task.message_id,
                error_message=f"Comet: {exc}\nKIE: {fallback_exc}",
            )
            await callback.answer()
            return

    async with session_scope(context.session_factory) as session:
        stored = await session.get(GenerationTask, task_id)
        if stored:
            stored.provider_task_id = provider_task_id
            stored.status = "submitted"
            stored.result_payload = result_payload
            stored.input_payload = {
                **dict(stored.input_payload or {}),
                "provider": provider,
                "provider_model": provider_model,
                **({"fallback_from": "comet"} if provider == "kie" else {}),
            }
    await state.clear()
    if status_message:
        with suppress(Exception):
            await status_message.edit_text(
                generation._status_text(
                    f"Seedance #{task_id} запущен",
                    65,
                    f"{duration} сек · {resolution} · {aspect_ratio}",
                )
            )
    await callback.answer()


async def _submit_motion_control_task_from_message(*args: Any, **kwargs: Any) -> None:
    assert _ORIGINAL_SUBMIT_MOTION_CONTROL_TASK is not None
    state: FSMContext = kwargs["state"]
    message: Message = kwargs["message"]
    bot = kwargs["bot"]
    data = await state.get_data()
    model_code = str(data.get("model_code") or "")
    contract = contract_for(model_code)
    allowed_video_types = set(_allowed(contract, "reference_video_mime_types", []))
    video_mime = str(data.get("motion_video_mime_type") or "")
    if allowed_video_types and video_mime not in allowed_video_types:
        allowed_names = ", ".join(_mime_label(item) for item in allowed_video_types)
        await message.answer(f"Для этой модели видео должно быть: {allowed_names}.")
        return

    min_dimension = int(contract.get("min_reference_dimension_px") or 0)
    if min_dimension:
        image_file_id = str(data.get("image_file_id") or "")
        image_bytes = await kwargs.get("bot").download(
            await kwargs.get("bot").get_file(image_file_id)
        ) if False else None
        try:
            image_content = await __import__(
                "app.plugins.generation.plugin",
                fromlist=["_download_telegram_file"],
            )._download_telegram_file(bot=bot, file_id=image_file_id)
            with Image.open(BytesIO(image_content)) as image:
                image_size = image.size
            video_size = await _probe_video_dimensions(
                kwargs["motion_video_content"],
                video_mime,
            )
        except Exception:
            logger.exception("Reference geometry validation failed")
            await message.answer("Не получилось проверить размер кадра референсов.")
            return
        for label, dimensions in (("Изображение", image_size), ("Видео", video_size)):
            error = _geometry_error(contract, dimensions)
            if error:
                await message.answer(f"{label}: {error}")
                return

    await _ORIGINAL_SUBMIT_MOTION_CONTROL_TASK(*args, **kwargs)


def _geometry_error(contract: dict[str, Any], dimensions: tuple[int, int]) -> str | None:
    width, height = dimensions
    min_dimension = int(contract.get("min_reference_dimension_px") or 0)
    if min(width, height) < min_dimension:
        return f"каждая сторона должна быть не меньше {min_dimension}px ({width}×{height})."
    ratio = width / height if height else 0
    min_ratio = float(contract.get("min_reference_aspect_ratio") or 0)
    max_ratio = float(contract.get("max_reference_aspect_ratio") or 999)
    if ratio < min_ratio or ratio > max_ratio:
        return f"соотношение сторон должно быть от 2:5 до 5:2 ({width}×{height})."
    return None


async def _probe_video_dimensions(content: bytes, mime_type: str) -> tuple[int, int]:
    suffix = ".mov" if mime_type == "video/quicktime" else ".mkv" if "matroska" in mime_type else ".mp4"
    path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
            handle.write(content)
            path = Path(handle.name)
        process = await asyncio.create_subprocess_exec(
            "ffprobe",
            "-v",
            "error",
            "-select_streams",
            "v:0",
            "-show_entries",
            "stream=width,height",
            "-of",
            "json",
            str(path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(process.communicate(), timeout=20)
        if process.returncode != 0:
            raise RuntimeError("ffprobe failed")
        payload = json.loads(stdout.decode("utf-8"))
        streams = payload.get("streams") or []
        width = int(streams[0].get("width") or 0) if streams else 0
        height = int(streams[0].get("height") or 0) if streams else 0
        if width <= 0 or height <= 0:
            raise RuntimeError("video dimensions unavailable")
        return width, height
    finally:
        if path:
            path.unlink(missing_ok=True)


def _mime_label(value: str) -> str:
    return {
        "video/mp4": "MP4",
        "video/quicktime": "MOV/QuickTime",
        "video/x-matroska": "MKV",
    }.get(value, value)


def install_generation_model_contracts(dispatcher: Dispatcher, context: AppContext) -> None:
    del context
    from app.plugins.generation import plugin as generation

    global _ORIGINAL_IMAGE_SETTINGS_KEYBOARD
    global _ORIGINAL_IMAGE_SETTINGS_TEXT
    global _ORIGINAL_SEND_IMAGE_REQUEST_SCREEN
    global _ORIGINAL_SUBMIT_IMAGE_TASK_COMMON
    global _ORIGINAL_CREATE_COMET_IMAGE_TASK
    global _ORIGINAL_SUBMIT_MOTION_TASK
    global _ORIGINAL_SUBMIT_MOTION_CONTROL_TASK

    generation.IMAGE_RESOLUTIONS = list(IMAGE_RESOLUTIONS)
    generation.IMAGE_ASPECT_RATIOS = list(IMAGE_ASPECT_RATIOS)
    generation.DEFAULT_IMAGE_RESOLUTION = DEFAULT_IMAGE_RESOLUTION
    generation.DEFAULT_IMAGE_ASPECT_RATIO = DEFAULT_IMAGE_ASPECT_RATIO
    generation.VIDEO_DURATIONS = list(SEEDANCE_DURATIONS)
    generation.VIDEO_ASPECT_RATIOS = list(SEEDANCE_ASPECT_RATIOS)
    generation.DEFAULT_VIDEO_ASPECT_RATIO = "16:9"
    generation.DEFAULT_VIDEO_RESOLUTION = "720p"
    generation.MOTION_CONTROL_CHARACTER_ORIENTATION = "image"

    if not hasattr(generation, "_generation_limits_payload_original"):
        generation._generation_limits_payload_original = generation._generation_limits_payload
    generation._generation_limits_payload = _generation_limits_payload

    if _ORIGINAL_IMAGE_SETTINGS_KEYBOARD is None:
        _ORIGINAL_IMAGE_SETTINGS_KEYBOARD = generation._image_settings_keyboard
    if _ORIGINAL_IMAGE_SETTINGS_TEXT is None:
        _ORIGINAL_IMAGE_SETTINGS_TEXT = generation._image_settings_text
    if _ORIGINAL_SEND_IMAGE_REQUEST_SCREEN is None:
        _ORIGINAL_SEND_IMAGE_REQUEST_SCREEN = generation._send_image_request_screen
    if _ORIGINAL_SUBMIT_IMAGE_TASK_COMMON is None:
        _ORIGINAL_SUBMIT_IMAGE_TASK_COMMON = generation._submit_image_task_common
    if _ORIGINAL_CREATE_COMET_IMAGE_TASK is None:
        _ORIGINAL_CREATE_COMET_IMAGE_TASK = generation._create_comet_image_task
    if _ORIGINAL_SUBMIT_MOTION_TASK is None:
        _ORIGINAL_SUBMIT_MOTION_TASK = generation._submit_motion_task
    if _ORIGINAL_SUBMIT_MOTION_CONTROL_TASK is None:
        _ORIGINAL_SUBMIT_MOTION_CONTROL_TASK = generation._submit_motion_control_task_from_message

    generation._image_settings_keyboard = _image_settings_keyboard
    generation._image_settings_text = _image_settings_text
    generation._send_image_request_screen = _send_image_request_screen
    generation._submit_image_task_common = _submit_image_task_common
    generation._create_comet_image_task = _create_comet_image_task
    generation._submit_motion_task = _submit_motion_task
    generation._submit_motion_control_task_from_message = _submit_motion_control_task_from_message

    dispatcher.message.middleware(ModelContractWebAppMiddleware())
    dispatcher.callback_query.middleware(ModelContractCallbackMiddleware())
    dispatcher.include_router(router)
