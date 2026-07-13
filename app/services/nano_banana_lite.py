from __future__ import annotations

import logging
from contextlib import suppress
from typing import Any

import httpx

from app.db import session_scope
from app.models import GenerationTask, User
from app.services.kie import KieApiError, KieUploadReference

logger = logging.getLogger(__name__)

MODEL_CODE = "nano-banana"
PROVIDER_MODEL = "nano-banana-2-lite"
MAX_REFERENCE_IMAGES = 10
MAX_PROMPT_LENGTH = 20_000
MAX_IMAGE_BYTES = 30_000_000
ALLOWED_IMAGE_MIME_TYPES = {"image/jpeg", "image/jpg", "image/png", "image/webp"}
ALLOWED_ASPECT_RATIOS = {
    "1:1",
    "1:4",
    "1:8",
    "2:3",
    "3:2",
    "3:4",
    "4:1",
    "4:3",
    "4:5",
    "5:4",
    "8:1",
    "9:16",
    "16:9",
    "21:9",
    "auto",
}
UI_ASPECT_RATIOS = ("9:16", "16:9", "1:1", "4:3")


def build_nano_banana_2_lite_payload(
    *,
    prompt: str,
    image_urls: list[str] | None = None,
    aspect_ratio: str | None = None,
    callback_url: str | None = None,
) -> dict[str, Any]:
    normalized_prompt = str(prompt or "").strip()
    if not normalized_prompt:
        raise ValueError("prompt is required")
    if len(normalized_prompt) > MAX_PROMPT_LENGTH:
        raise ValueError("prompt exceeds 20000 characters")

    normalized_urls = [str(url).strip() for url in (image_urls or []) if str(url).strip()]
    if len(normalized_urls) > MAX_REFERENCE_IMAGES:
        raise ValueError("nano-banana-2-lite accepts at most 10 images")

    ratio = str(aspect_ratio or "auto").strip()
    if ratio not in ALLOWED_ASPECT_RATIOS:
        ratio = "auto"

    payload: dict[str, Any] = {
        "model": PROVIDER_MODEL,
        "input": {
            "prompt": normalized_prompt,
            "image_urls": normalized_urls,
            "aspect_ratio": ratio,
        },
    }
    if callback_url:
        payload["callBackUrl"] = callback_url
    return payload


async def create_nano_banana_2_lite_task(
    kie: Any,
    *,
    prompt: str,
    image_urls: list[str] | None = None,
    aspect_ratio: str | None = None,
    callback_url: str | None = None,
) -> str:
    payload = build_nano_banana_2_lite_payload(
        prompt=prompt,
        image_urls=image_urls,
        aspect_ratio=aspect_ratio,
        callback_url=callback_url,
    )
    async with httpx.AsyncClient(base_url=kie.base_url, timeout=kie.timeout) as client:
        response = await client.post(
            "/api/v1/jobs/createTask",
            headers=kie._headers(),
            json=payload,
        )
    data = kie._decode_response(response, provider="KIE Nano Banana 2 Lite")
    task_data = data.get("data") if isinstance(data.get("data"), dict) else {}
    task_id = task_data.get("taskId") or task_data.get("task_id")
    if not task_id:
        raise KieApiError(f"KIE Nano Banana 2 Lite response does not contain taskId: {data}")
    return str(task_id)


def _normalized_mime_type(value: Any) -> str:
    return str(value or "").split(";", 1)[0].strip().lower()


def _is_lite_state(data: dict[str, Any]) -> bool:
    return str(data.get("model_code") or "") == MODEL_CODE


def install_nano_banana_lite_patch() -> None:
    """Route the legacy ordinary Banana product code to KIE Nano Banana 2 Lite."""

    from app.plugins.generation import plugin as generation

    if getattr(generation, "_nano_banana_lite_patch_installed", False):
        return

    original_create_image_task = generation._create_comet_image_task
    original_limits_payload = generation._generation_limits_payload
    original_settings_text = generation._image_settings_text
    original_settings_keyboard = generation._image_settings_keyboard

    async def create_image_task(*args: Any, **kwargs: Any) -> None:
        model_code = str(kwargs.get("model_code") or "")
        if model_code != MODEL_CODE:
            await original_create_image_task(*args, **kwargs)
            return
        await _create_lite_image_task(generation=generation, **kwargs)

    def generation_limits_payload(user: User, model: Any) -> dict[str, Any]:
        payload = original_limits_payload(user, model)
        if str(getattr(model, "code", "") or "") == MODEL_CODE:
            payload.update(
                {
                    "max_images": MAX_REFERENCE_IMAGES,
                    "resolution": "1K",
                    "aspect_ratios": list(UI_ASPECT_RATIOS),
                    "provider": "kie",
                    "provider_model": PROVIDER_MODEL,
                }
            )
        return payload

    def image_settings_text(data: dict[str, Any]) -> str:
        text = original_settings_text(data)
        if _is_lite_state(data):
            current = generation._normalize_image_resolution(data.get("resolution"))
            text = text.replace(
                f"Качество: <b>{current}</b>",
                "Качество: <b>1K</b>",
                1,
            )
        return text

    def image_settings_keyboard(data: dict[str, Any]):
        if not _is_lite_state(data):
            return original_settings_keyboard(data)
        current_aspect_ratio = str(data.get("aspect_ratio") or generation.DEFAULT_IMAGE_ASPECT_RATIO)
        builder = generation.InlineKeyboardBuilder()
        for aspect_ratio in UI_ASPECT_RATIOS:
            builder.button(
                text=generation._option_label(aspect_ratio, current_aspect_ratio),
                callback_data=f"image:aspect:{aspect_ratio}",
            )
        submit_rows: list[int] = []
        if str(data.get("prompt") or "").strip():
            builder.button(text="Запустить", callback_data="image:submit")
            submit_rows.append(1)
        nav_count = generation.add_navigation_buttons(builder, back_callback="menu:image")
        builder.adjust(len(UI_ASPECT_RATIOS), *submit_rows, nav_count)
        return builder.as_markup()

    generation._create_comet_image_task = create_image_task
    generation._generation_limits_payload = generation_limits_payload
    generation._image_settings_text = image_settings_text
    generation._image_settings_keyboard = image_settings_keyboard
    generation._nano_banana_lite_patch_installed = True


async def _create_lite_image_task(
    *,
    generation: Any,
    message: Any | None = None,
    callback: Any | None = None,
    context: Any,
    state: Any,
    user_id: int,
    chat_id: int,
    model_code: str,
    prompt: str,
    input_payload: dict[str, Any],
    reference_images: list[Any],
) -> None:
    target_message = message or (callback.message if callback else None)
    data = await state.get_data()
    status_message_id = data.get("status_message_id") if isinstance(data.get("status_message_id"), int) else None

    if not context.kie.is_configured:
        await generation._notify_image_submit_error(
            target_message,
            callback,
            "Nano Banana 2 Lite сейчас недоступна: KIE API не настроен",
            show_alert=True,
        )
        return

    if target_message and status_message_id:
        with suppress(Exception):
            await target_message.bot.edit_message_text(
                generation._status_text("Запускаю Nano Banana 2 Lite", 55),
                chat_id=chat_id,
                message_id=status_message_id,
            )
    elif target_message:
        status_message = await target_message.answer(
            generation._status_text("Запускаю Nano Banana 2 Lite", 55)
        )
        status_message_id = status_message.message_id

    async with session_scope(context.session_factory) as session:
        user = await session.get(User, user_id, with_for_update=True)
        model = await generation.get_model(session, model_code)
        if not user or not model or not model.is_enabled or model.category != "image":
            await generation._notify_image_submit_error(
                target_message,
                callback,
                "Модель недоступна",
                show_alert=True,
            )
            return

        has_unlimited = generation.user_has_unlimited(user)
        free_generation = generation.user_generates_for_free(user)
        credit_type = generation.model_credit_type(model)
        available_balance = generation.user_credit_balance(user, credit_type)
        charged_credits = 0 if model.price_credits <= 0 or free_generation else int(model.price_credits)
        if charged_credits > available_balance:
            await generation._notify_image_submit_error(
                target_message,
                callback,
                "Недостаточно фото-кредитов. Откройте раздел «Пакеты»",
                show_alert=True,
            )
            return

        credit_spend = generation.spend_user_credits(
            user,
            credit_type=credit_type,
            amount=charged_credits,
        )
        if credit_spend is None:
            await generation._notify_image_submit_error(
                target_message,
                callback,
                "Недостаточно фото-кредитов. Откройте раздел «Пакеты»",
                show_alert=True,
            )
            return

        charge_details = generation._charge_details_text(
            user,
            charged_credits,
            has_unlimited,
            credit_type,
        )
        provider_model = str((model.config or {}).get("provider_model") or PROVIDER_MODEL)
        max_reference_images = min(
            MAX_REFERENCE_IMAGES,
            generation._max_image_references_from_config(model.config),
        )
        limited_references = list(reference_images[:max_reference_images])
        stored_input = dict(input_payload)
        stored_input.pop("output_format", None)
        stored_input.update(
            {
                "resolution": "1K",
                "provider": "kie",
                "provider_family": "image",
                "provider_model": provider_model,
                "max_reference_images": max_reference_images,
                "credit_type": credit_type,
                "credit_spend": credit_spend,
            }
        )
        task = GenerationTask(
            user_id=user.id,
            model_code=model.code,
            provider_task_id=None,
            status="submitting",
            prompt=prompt,
            input_payload=stored_input,
            result_payload={},
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
                generation._status_text(
                    "Загружаю референсы в KIE",
                    65,
                    f"{charge_details}\nКачество: 1K.",
                ),
                chat_id=chat_id,
                message_id=status_message_id,
            )

    try:
        uploaded_urls: list[str] = []
        for index, image in enumerate(limited_references, start=1):
            mime_type = _normalized_mime_type(image.mime_type)
            if mime_type not in ALLOWED_IMAGE_MIME_TYPES:
                raise ValueError(f"unsupported image type: {mime_type}")
            if len(image.content) > MAX_IMAGE_BYTES:
                raise ValueError("reference image exceeds 30 MB")
            uploaded_urls.append(
                await context.kie.upload_base64_image(
                    KieUploadReference(
                        content=image.content,
                        mime_type=mime_type,
                        filename=f"nano-banana-lite-reference-{index}.{generation._mime_extension(mime_type)}",
                    )
                )
            )

        provider_task_id = await create_nano_banana_2_lite_task(
            context.kie,
            prompt=prompt,
            image_urls=uploaded_urls,
            aspect_ratio=str(input_payload.get("aspect_ratio") or "auto"),
            callback_url=context.settings.comet_callback_url,
        )
    except Exception:
        logger.exception("KIE Nano Banana 2 Lite task creation failed")
        await generation._fail_comet_image_task(
            context=context,
            task_id=task_id,
            chat_id=chat_id,
            message_id=status_message_id,
            error_message="Не получилось запустить Nano Banana 2 Lite. Попробуйте позже.",
        )
        return

    async with session_scope(context.session_factory) as session:
        task = await session.get(GenerationTask, task_id, with_for_update=True)
        if not task or task.status in {"success", "fail"}:
            return
        task.provider_task_id = provider_task_id
        task.status = "submitted"
        task.input_payload = {
            **dict(task.input_payload or {}),
            "uploaded_reference_urls": uploaded_urls,
        }

    if target_message and status_message_id:
        with suppress(Exception):
            await target_message.bot.edit_message_text(
                generation._status_text(
                    f"Nano Banana 2 Lite #{task_id} запущена",
                    70,
                    "KIE принял задачу. Я пришлю результат после завершения.",
                ),
                chat_id=chat_id,
                message_id=status_message_id,
            )
