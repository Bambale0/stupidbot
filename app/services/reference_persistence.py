from __future__ import annotations

from typing import Any

from aiogram.fsm.context import FSMContext


PERSISTED_IMAGE_STATE_KEYS = (
    "model_code",
    "resolution",
    "aspect_ratio",
    "output_format",
    "image_limits",
    "explicit_model_selected",
    "source_feed_task_id",
)


def image_reference_continuation_payload(data: dict[str, Any]) -> dict[str, Any] | None:
    """Build the reusable image state that survives one generation submission.

    The prompt and transient status-message fields are intentionally excluded: the
    next user text becomes the new prompt while the same references/settings stay
    active.
    """

    from app.plugins.generation import plugin as generation

    references = generation._image_reference_items(data)
    if not references:
        return None

    first_reference = references[0]
    payload: dict[str, Any] = {
        "prompt": "",
        "image_references": references,
        "image_file_id": first_reference.get("telegram_file_id"),
        "image_filename": first_reference.get("filename"),
        "image_mime_type": first_reference.get("mime_type"),
    }
    for key in PERSISTED_IMAGE_STATE_KEYS:
        value = data.get(key)
        if value is not None:
            payload[key] = value
    return payload


async def restore_image_reference_continuation(
    state: FSMContext,
    snapshot: dict[str, Any],
) -> bool:
    """Restore refs only when the generation path actually cleared the FSM.

    If another update already moved the user into a new state while the provider
    call was running, that newer state wins and is never overwritten here.
    """

    payload = image_reference_continuation_payload(snapshot)
    if not payload:
        return False
    if await state.get_state() is not None:
        return False

    from app.plugins.generation import plugin as generation

    await state.set_state(generation.ImageFlow.settings)
    await state.update_data(**payload)
    return True


def install_reference_persistence_patch() -> None:
    """Keep active image references/settings available for the next text prompt."""

    from app.plugins.generation import plugin as generation

    if getattr(generation, "_reference_persistence_patch_installed", False):
        return

    original_create_image_task = generation._create_comet_image_task

    async def create_image_task(*args: Any, **kwargs: Any) -> None:
        state = kwargs.get("state")
        snapshot: dict[str, Any] = {}
        if isinstance(state, FSMContext):
            snapshot = dict(await state.get_data())

        await original_create_image_task(*args, **kwargs)

        if isinstance(state, FSMContext) and snapshot:
            await restore_image_reference_continuation(state, snapshot)

    generation._create_comet_image_task = create_image_task
    generation._reference_persistence_patch_installed = True
