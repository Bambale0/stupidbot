from __future__ import annotations


def bind_corrected_model_handlers() -> None:
    """Replace function objects already captured by the loaded generation plugin."""

    from app.plugins.generation import plugin as generation
    from app.services.model_contract_corrections import (
        _image_settings_keyboard,
        _image_settings_text,
        _motion_submit_with_orientation,
    )

    generation._image_settings_keyboard = _image_settings_keyboard
    generation._image_settings_text = _image_settings_text
    generation._submit_motion_control_task_from_message = _motion_submit_with_orientation
