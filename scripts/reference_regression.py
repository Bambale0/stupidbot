from __future__ import annotations

from pathlib import Path

if __package__ in {None, ""}:
    from _bootstrap import add_project_root_to_path

    add_project_root_to_path()

from app.models import GenerationModel, GenerationTask
from app.plugins.generation.plugin import _image_settings_keyboard, _repeat_image_state_payload
from app.plugins.loader import normalized_plugin_names
from app.plugins.references.plugin import (
    _callback_task_id,
    collect_reference_tasks,
    preserve_reference_origin,
    reference_signature,
    submit_image_from_settings,
)
from app.ui import model_keyboard


def _image_task(
    task_id: int,
    *,
    file_ids: list[str],
    prompt: str = "portrait",
) -> GenerationTask:
    references = [
        {
            "telegram_file_id": file_id,
            "filename": f"{file_id}.jpg",
            "mime_type": "image/jpeg",
            "size": 1024,
        }
        for file_id in file_ids
    ]
    task = GenerationTask(
        user_id=1,
        model_code="nano-banana-2",
        status="success",
        prompt=prompt,
        input_payload={
            "prompt": prompt,
            "aspect_ratio": "9:16",
            "resolution": "4K",
            "references": references,
            "max_reference_images": 14,
        },
        result_urls=["telegram-result-file-id"],
    )
    task.id = task_id
    return task


def _callbacks(markup) -> list[str]:
    return [
        str(button.callback_data)
        for row in markup.inline_keyboard
        for button in row
        if button.callback_data
    ]


def _texts(markup) -> list[str]:
    return [str(button.text) for row in markup.inline_keyboard for button in row]


def main() -> None:
    original = _image_task(101, file_ids=["ref-a", "ref-b"])
    duplicate = _image_task(102, file_ids=["ref-a", "ref-b"], prompt="another prompt")
    unique = _image_task(103, file_ids=["ref-c"])
    video = GenerationTask(
        user_id=1,
        model_code="seedance-2/video",
        status="success",
        prompt="video",
        input_payload={"reference": {"telegram_file_id": "video-ref"}},
    )
    video.id = 104

    payload = _repeat_image_state_payload(original)
    assert payload is not None
    assert payload["prompt"] == "portrait"
    assert payload["resolution"] == "4K"
    assert [item["telegram_file_id"] for item in payload["image_references"]] == [
        "ref-a",
        "ref-b",
    ]
    assert reference_signature(original) == ("ref-a", "ref-b")

    keyboard_payload = {**payload, "image_limits": {"max_images": 14}}
    assert "image:submit" in _callbacks(_image_settings_keyboard(keyboard_payload))
    assert callable(submit_image_from_settings)

    saved = collect_reference_tasks([original, duplicate, video, unique], limit=10)
    assert [task.id for task in saved] == [101, 103]
    assert collect_reference_tasks([original], limit=0) == []

    assert _callback_task_id("image:again:101", "image:again:") == 101
    assert _callback_task_id("image:again:not-a-number", "image:again:") is None
    assert _callback_task_id("refs:use:0", "refs:use:") is None
    assert _callback_task_id("refs:use:-1", "refs:use:") is None

    derivative = _image_task(105, file_ids=["ref-foreign"])
    derivative.source_feed_task_id = 77
    origin_payload: dict[str, object] = {}
    preserve_reference_origin(origin_payload, derivative)
    assert origin_payload["source_feed_task_id"] == 77

    configured = ["core", "generation", "gallery", "ux", "generation"]
    assert normalized_plugin_names(configured) == [
        "core",
        "generation",
        "references",
        "gallery",
        "ux",
    ]
    assert normalized_plugin_names(["core", "admin", "ux"]) == ["core", "admin", "ux"]

    image_model = GenerationModel(
        code="nano-banana-2",
        title="Banana 2",
        category="image",
        price_credits=5,
        is_enabled=True,
        position=1,
    )
    video_model = GenerationModel(
        code="seedance-2",
        title="Seedance 2",
        category="video",
        price_credits=10,
        is_enabled=True,
        position=2,
    )
    image_picker = model_keyboard([image_model])
    assert "Мои референсы" in _texts(image_picker)
    assert "menu:references" in _callbacks(image_picker)
    video_picker = model_keyboard([video_model])
    assert "Мои референсы" not in _texts(video_picker)
    assert "menu:references" not in _callbacks(video_picker)

    source = Path("app/plugins/references/plugin.py").read_text(encoding="utf-8")
    assert "menu:more" not in source
    assert 'F.data.startswith("image:again:")' in source
    assert 'F.data == "image:submit"' in source
    assert 'GenerationTask.user_id == user_id' in source

    print("Reference reuse regression passed")


if __name__ == "__main__":
    main()
