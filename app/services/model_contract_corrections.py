from __future__ import annotations

import json
from contextlib import suppress
from contextvars import ContextVar
from html import escape
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Dispatcher, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message, TelegramObject
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.services import model_contracts
from app.services.generation_catalog import DEFAULT_MINI_APP_IMAGE_MODEL

router = Router(name="model-contract-corrections")
_MOTION_ORIENTATION: ContextVar[str | None] = ContextVar(
    "motion_character_orientation",
    default=None,
)
_ORIGINAL_KIE_MOTION_TASK: Any | None = None
_ORIGINAL_MODEL_CONTRACT_MOTION_SUBMIT: Any | None = None


def _allowed(contract: dict[str, Any], key: str, fallback: list[str]) -> list[str]:
    value = contract.get(key)
    if isinstance(value, list):
        return [str(item) for item in value]
    return list(fallback)


def image_output_format(model_code: str, value: Any) -> str:
    contract = model_contracts.contract_for(model_code)
    allowed = _allowed(contract, "output_formats", [])
    if not allowed:
        return ""
    normalized = str(value or "").strip()
    return normalized if normalized in allowed else allowed[0]


def character_orientation(model_code: str, value: Any) -> str:
    contract = model_contracts.contract_for(model_code)
    allowed = _allowed(contract, "character_orientations", ["image"])
    normalized = str(value or "").strip()
    configured_default = str(contract.get("character_orientation") or "image")
    if normalized in allowed:
        return normalized
    if configured_default in allowed:
        return configured_default
    return allowed[0]


def _image_settings_keyboard(data: dict[str, Any]) -> InlineKeyboardMarkup:
    from app.plugins.generation import plugin as generation

    model_code = str(data.get("model_code") or DEFAULT_MINI_APP_IMAGE_MODEL)
    contract = model_contracts.contract_for(model_code)
    resolutions = _allowed(contract, "resolutions", ["1K"])
    aspects = _allowed(contract, "aspect_ratios", ["auto"])
    current_resolution = model_contracts.image_resolution(model_code, data.get("resolution"))
    current_aspect = model_contracts.image_aspect_ratio(model_code, data.get("aspect_ratio"))

    builder = InlineKeyboardBuilder()
    for value in resolutions:
        builder.button(
            text=model_contracts._option_label(value, current_resolution),
            callback_data=f"image:resolution:{value}",
        )
    for value in aspects:
        builder.button(
            text=model_contracts._option_label(value, current_aspect),
            callback_data=f"image:aspect:{value}",
        )
    rows: list[int] = []
    rows.extend(model_contracts._chunk_sizes(len(resolutions), 4))
    rows.extend(model_contracts._chunk_sizes(len(aspects), 3))
    if str(data.get("prompt") or "").strip():
        builder.button(text="Запустить", callback_data="image:submit")
        rows.append(1)
    nav_count = generation.add_navigation_buttons(builder, back_callback="menu:image")
    rows.append(nav_count)
    builder.adjust(*rows)
    return builder.as_markup()


def _image_settings_text(data: dict[str, Any]) -> str:
    from app.plugins.generation import plugin as generation

    model_code = str(data.get("model_code") or DEFAULT_MINI_APP_IMAGE_MODEL)
    resolution = model_contracts.image_resolution(model_code, data.get("resolution"))
    aspect = model_contracts.image_aspect_ratio(model_code, data.get("aspect_ratio"))
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
        f"Соотношение сторон: <b>{escape(aspect)}</b>\n"
        "Формат файла определяется ответом основного Gemini-провайдера.\n"
        f"{generation._image_limits_text(data.get('image_limits'))}"
        f"Промпт:\n{prompt_text}\n\n"
        f"{instruction}"
    )


def _orientation_keyboard(model_code: str, selected: str) -> InlineKeyboardMarkup:
    allowed = _allowed(
        model_contracts.contract_for(model_code),
        "character_orientations",
        ["image"],
    )
    builder = InlineKeyboardBuilder()
    labels = {
        "image": "Ориентация по фото",
        "video": "Ориентация по видео",
    }
    for value in allowed:
        prefix = "✓ " if value == selected else ""
        builder.button(
            text=f"{prefix}{labels.get(value, value)}",
            callback_data=f"motion:orientation:{value}",
        )
    builder.adjust(1)
    return builder.as_markup()


@router.callback_query(F.data.startswith("motion:orientation:"))
async def select_motion_orientation(callback: CallbackQuery, state: FSMContext) -> None:
    data = await state.get_data()
    model_code = str(data.get("model_code") or "")
    selected = callback.data.removeprefix("motion:orientation:")
    allowed = _allowed(
        model_contracts.contract_for(model_code),
        "character_orientations",
        [],
    )
    if selected not in allowed:
        await callback.answer("Эта ориентация моделью не поддерживается", show_alert=True)
        return
    await state.update_data(character_orientation=selected)
    if callback.message:
        with suppress(Exception):
            await callback.message.edit_reply_markup(
                reply_markup=_orientation_keyboard(model_code, selected)
            )
    await callback.answer("Настройка сохранена")


class OrientationPayloadMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        state = data.get("state")
        previous_state = await state.get_state() if isinstance(state, FSMContext) else None
        payload: dict[str, Any] = {}
        web_app_data = getattr(getattr(event, "web_app_data", None), "data", None)
        if web_app_data:
            with suppress(json.JSONDecodeError):
                decoded = json.loads(web_app_data)
                if isinstance(decoded, dict):
                    payload = decoded

        result = await handler(event, data)
        if not isinstance(state, FSMContext):
            return result

        current = await state.get_data()
        model_code = str(current.get("model_code") or payload.get("model_code") or "")
        orientations = _allowed(
            model_contracts.contract_for(model_code),
            "character_orientations",
            [],
        )
        if payload and orientations:
            selected = character_orientation(model_code, payload.get("character_orientation"))
            await state.update_data(character_orientation=selected)

        should_offer = False
        if isinstance(event, CallbackQuery) and str(event.data or "").startswith("gen:model:kling-"):
            should_offer = True
        if isinstance(event, Message) and previous_state and str(previous_state).endswith(":prompt"):
            should_offer = bool(orientations)
        if should_offer and orientations:
            selected = character_orientation(
                model_code,
                (await state.get_data()).get("character_orientation"),
            )
            target_message = event.message if isinstance(event, CallbackQuery) else event
            if target_message:
                await target_message.answer(
                    "Как модель должна ориентировать персонажа?",
                    reply_markup=_orientation_keyboard(model_code, selected),
                )
        return result


async def _motion_submit_with_orientation(*args: Any, **kwargs: Any) -> None:
    assert _ORIGINAL_MODEL_CONTRACT_MOTION_SUBMIT is not None
    state: FSMContext = kwargs["state"]
    data = await state.get_data()
    model_code = str(data.get("model_code") or "")
    selected = character_orientation(model_code, data.get("character_orientation"))
    token = _MOTION_ORIENTATION.set(selected)
    try:
        await _ORIGINAL_MODEL_CONTRACT_MOTION_SUBMIT(*args, **kwargs)
    finally:
        _MOTION_ORIENTATION.reset(token)


def install_model_contract_corrections(dispatcher: Dispatcher) -> None:
    from app.services.kie import KieClient

    global _ORIGINAL_KIE_MOTION_TASK
    global _ORIGINAL_MODEL_CONTRACT_MOTION_SUBMIT

    model_contracts._allowed = _allowed
    model_contracts.image_output_format = image_output_format
    model_contracts._image_settings_keyboard = _image_settings_keyboard
    model_contracts._image_settings_text = _image_settings_text

    if _ORIGINAL_MODEL_CONTRACT_MOTION_SUBMIT is None:
        _ORIGINAL_MODEL_CONTRACT_MOTION_SUBMIT = (
            model_contracts._submit_motion_control_task_from_message
        )
        model_contracts._submit_motion_control_task_from_message = (
            _motion_submit_with_orientation
        )

    if _ORIGINAL_KIE_MOTION_TASK is None:
        _ORIGINAL_KIE_MOTION_TASK = KieClient.create_kling_motion_control_task
        original = _ORIGINAL_KIE_MOTION_TASK

        async def create_kling_motion_control_task(
            self: Any,
            **kwargs: Any,
        ) -> str:
            selected = _MOTION_ORIENTATION.get()
            if selected:
                kwargs["character_orientation"] = selected
            return await original(self, **kwargs)

        KieClient.create_kling_motion_control_task = create_kling_motion_control_task

    dispatcher.message.middleware(OrientationPayloadMiddleware())
    dispatcher.callback_query.middleware(OrientationPayloadMiddleware())
    dispatcher.include_router(router)
