from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

import httpx

from app.config import Settings
from app.services import model_contracts
from app.services.generation_catalog import (
    DEFAULT_MODELS,
    GEMINI_FLASH_ASPECT_RATIOS,
    GEMINI_FLASH_LITE_ASPECT_RATIOS,
    GEMINI_PRO_ASPECT_RATIOS,
    SEEDANCE_ASPECT_RATIOS,
    SEEDANCE_DURATIONS,
    model_default_config,
)
from app.services.kie import KieClient


def _model(code: str) -> dict[str, Any]:
    return next(item for item in DEFAULT_MODELS if item["code"] == code)


class _FakeHttpClient:
    last_json: dict[str, Any] | None = None

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        del args, kwargs

    async def __aenter__(self) -> "_FakeHttpClient":
        return self

    async def __aexit__(self, *args: Any) -> None:
        del args

    async def post(self, path: str, **kwargs: Any) -> httpx.Response:
        assert path == "/api/v1/jobs/createTask"
        _FakeHttpClient.last_json = kwargs.get("json")
        return httpx.Response(
            200,
            json={"code": 200, "msg": "success", "data": {"taskId": "lite-task"}},
            request=httpx.Request("POST", f"https://api.kie.ai{path}"),
        )


def check_catalog() -> None:
    settings = Settings(_env_file=None)
    assert settings.comet_image_simple_model == "gemini-3.1-flash-lite-image"
    assert settings.comet_image_pro_model == "gemini-3-pro-image"
    assert settings.comet_image_2_model == "gemini-3.1-flash-image"
    assert settings.kie_image_simple_model == "nano-banana-2-lite"

    lite = _model("nano-banana")["config"]
    assert lite["provider_model"] == "gemini-3.1-flash-lite-image"
    assert lite["resolutions"] == ["1K"]
    assert lite["aspect_ratios"] == GEMINI_FLASH_LITE_ASPECT_RATIOS
    assert lite["min_images"] == 0
    assert lite["max_images"] == 14

    flash = _model("nano-banana-2")["config"]
    assert flash["provider_model"] == "gemini-3.1-flash-image"
    assert flash["resolutions"] == ["512", "1K", "2K", "4K"]
    assert flash["aspect_ratios"] == GEMINI_FLASH_ASPECT_RATIOS
    assert flash["max_images"] == 14

    pro = _model("nano-banana-pro")["config"]
    assert pro["provider_model"] == "gemini-3-pro-image"
    assert pro["resolutions"] == ["1K", "2K", "4K"]
    assert pro["aspect_ratios"] == GEMINI_PRO_ASPECT_RATIOS
    assert pro["max_images"] == 14

    seedance = _model("seedance-2/video")["config"]
    assert seedance["durations"] == [str(value) for value in range(4, 16)]
    assert seedance["durations"] == SEEDANCE_DURATIONS
    assert seedance["aspect_ratios"] == SEEDANCE_ASPECT_RATIOS
    assert seedance["resolutions"] == ["480p", "720p", "1080p"]
    assert seedance["min_images"] == 0
    assert seedance["max_images"] == 1

    kling_26 = _model("kling-2.6/video")["config"]
    assert "video/x-matroska" in kling_26["reference_video_mime_types"]
    assert kling_26["character_orientation"] == "image"
    assert kling_26["min_duration_seconds"] == 3
    assert kling_26["max_duration_seconds"] == 30

    kling_30 = _model("kling-3.0/video")["config"]
    assert "video/x-matroska" not in kling_30["reference_video_mime_types"]
    assert kling_30["reference_video_mime_types"] == ["video/mp4", "video/quicktime"]
    assert kling_30["min_reference_dimension_px"] == 341
    assert kling_30["min_reference_aspect_ratio"] == 0.4
    assert kling_30["max_reference_aspect_ratio"] == 2.5
    assert kling_30["background_source"] == "input_video"


def check_normalization_and_geometry() -> None:
    assert model_contracts.image_resolution("nano-banana", "4K") == "1K"
    assert model_contracts.image_resolution("nano-banana-2", "512") == "512"
    assert model_contracts.image_aspect_ratio("nano-banana-pro", "1:8") == "1:1"
    assert model_contracts.image_aspect_ratio("nano-banana-2", "1:8") == "1:8"
    assert model_contracts.seedance_duration("4") == "4"
    assert model_contracts.seedance_duration("16") == "5"
    assert model_contracts.seedance_aspect_ratio("21:9") == "21:9"
    assert model_contracts.seedance_resolution("bad") == "720p"

    contract = model_default_config("kling-3.0/video")
    assert model_contracts._geometry_error(contract, (341, 341)) is None
    assert model_contracts._geometry_error(contract, (340, 500)) is not None
    assert model_contracts._geometry_error(contract, (1000, 341)) is not None
    assert model_contracts._geometry_error(contract, (341, 1000)) is not None


async def check_kie_lite_payload() -> None:
    model_contracts.install_kie_image_contract()
    original_client = model_contracts.httpx.AsyncClient
    model_contracts.httpx.AsyncClient = _FakeHttpClient  # type: ignore[assignment]
    try:
        client = KieClient("test-key")
        task_id = await client.create_image_task(
            model="nano-banana-2-lite",
            prompt="test prompt",
            image_urls=[f"https://example.com/{index}.png" for index in range(12)],
            aspect_ratio="21:9",
            resolution="4K",
            output_format="jpg",
            callback_url="https://example.com/callback",
        )
    finally:
        model_contracts.httpx.AsyncClient = original_client
    assert task_id == "lite-task"
    payload = _FakeHttpClient.last_json
    assert payload is not None
    assert payload["model"] == "nano-banana-2-lite"
    assert payload["callBackUrl"] == "https://example.com/callback"
    assert set(payload["input"]) == {"prompt", "image_urls", "aspect_ratio"}
    assert payload["input"]["aspect_ratio"] == "21:9"
    assert len(payload["input"]["image_urls"]) == 10
    assert "resolution" not in payload["input"]
    assert "output_format" not in payload["input"]
    assert "image_input" not in payload["input"]


def check_frontend_contract() -> None:
    root = Path(__file__).resolve().parents[1]
    catalog = (root / "app/static/miniapp/assets/catalog.js").read_text(encoding="utf-8")
    payload = (root / "app/static/miniapp/assets/payload.js").read_text(encoding="utf-8")
    ui = (root / "app/static/miniapp/assets/model-contracts-ui.js").read_text(encoding="utf-8")
    index = (root / "app/static/miniapp/index.html").read_text(encoding="utf-8")

    for contract in (
        'providerKey: "gemini-3.1-flash-lite-image"',
        'providerKey: "gemini-3-pro-image"',
        'providerKey: "gemini-3.1-flash-image"',
        'resolutions: ["512", "1K", "2K", "4K"]',
        'durations: ["4", "5", "6", "7", "8", "9", "10", "11", "12", "13", "14", "15"]',
        'videoFormats: ["MP4", "MOV"]',
    ):
        assert contract in catalog
    assert "window.BANANA_MODEL_SETTINGS" in payload
    assert 'resolution: "2K"' not in payload
    assert 'aspect: "9:16"' not in payload
    assert 'data-model-setting="aspectRatio"' in ui
    assert 'data-model-setting="resolution"' in ui
    assert 'data-model-setting="duration"' in ui
    assert "model-contracts-ui.js?v=20260724-provider1" in index


async def amain() -> None:
    check_catalog()
    check_normalization_and_geometry()
    await check_kie_lite_payload()
    check_frontend_contract()
    print("model provider contracts regression passed")


if __name__ == "__main__":
    asyncio.run(amain())
