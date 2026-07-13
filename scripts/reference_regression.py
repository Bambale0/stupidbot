from __future__ import annotations

if __package__ in {None, ""}:
    from _bootstrap import add_project_root_to_path

    add_project_root_to_path()

from app.config import Settings
from app.models import GenerationTask
from app.plugins.generation.plugin import _repeat_image_state_payload
from app.plugins.references.plugin import (
    _callback_task_id,
    collect_reference_tasks,
    reference_signature,
)


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

    saved = collect_reference_tasks([original, duplicate, video, unique], limit=10)
    assert [task.id for task in saved] == [101, 103]

    assert _callback_task_id("image:again:101", "image:again:") == 101
    assert _callback_task_id("image:again:not-a-number", "image:again:") is None
    assert _callback_task_id("refs:use:0", "refs:use:") is None

    settings = Settings(enabled_plugins="core,generation,admin")
    assert settings.enabled_plugins == ["core", "generation", "references", "admin"]

    print("reference reuse regression passed")


if __name__ == "__main__":
    main()
