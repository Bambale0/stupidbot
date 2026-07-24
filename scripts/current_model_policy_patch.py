from __future__ import annotations

from copy import deepcopy
from typing import Any

from app.services.generation_catalog import (
    GEMINI_FLASH_ASPECT_RATIOS,
    GEMINI_FLASH_LITE_ASPECT_RATIOS,
    GEMINI_PRO_ASPECT_RATIOS,
    SEEDANCE_ASPECT_RATIOS,
    SEEDANCE_DURATIONS,
)
from app.services.model_contracts import install_model_repository_contracts


def _legacy_resolution(value: Any) -> str:
    normalized = str(value or "").strip()
    return normalized if normalized in {"2K", "4K"} else "2K"


def _legacy_aspect(value: Any) -> str:
    normalized = str(value or "").strip()
    allowed = ["9:16", "16:9", "4:3", "1:1"]
    return normalized if normalized in allowed else "9:16"


def _legacy_models(current: list[dict[str, Any]]) -> list[dict[str, Any]]:
    models = deepcopy(current)
    image_contracts = {
        "nano-banana": ("gemini-3.1-flash-image-preview", 1),
        "nano-banana-pro": ("gemini-3-pro-image-preview", 8),
        "nano-banana-2": ("gemini-3.1-flash-image-preview", 14),
    }
    for item in models:
        config = dict(item.get("config") or {})
        if item["code"] in image_contracts:
            provider_model, max_images = image_contracts[item["code"]]
            config.update(
                provider="comet",
                provider_model=provider_model,
                resolutions=["2K", "4K"],
                max_images=max_images,
            )
        if item["code"].startswith("kling"):
            config["character_orientation"] = "video"
        item["config"] = config
    return models


def install(adapter: Any) -> None:
    previous_static = adapter.LEGACY_STATIC_LOGIC
    install_model_repository_contracts()

    def static_logic(regression: Any) -> None:
        legacy = adapter.legacy
        settings = legacy.get_settings()
        settings_fields = {
            "comet_image_simple_model": settings.comet_image_simple_model,
            "comet_image_pro_model": settings.comet_image_pro_model,
            "comet_image_2_model": settings.comet_image_2_model,
        }
        saved_models = legacy.DEFAULT_MODELS
        saved_resolutions = legacy.IMAGE_RESOLUTIONS
        saved_aspects = legacy.IMAGE_ASPECT_RATIOS
        saved_resolution_normalizer = legacy.normalize_image_resolution
        saved_aspect_normalizer = legacy.normalize_image_aspect_ratio
        saved_orientation = legacy.generation_plugin.MOTION_CONTROL_CHARACTER_ORIENTATION
        try:
            settings.comet_image_simple_model = "gemini-3.1-flash-image-preview"
            settings.comet_image_pro_model = "gemini-3-pro-image-preview"
            settings.comet_image_2_model = "gemini-3.1-flash-image-preview"
            legacy.DEFAULT_MODELS = _legacy_models(saved_models)
            legacy.IMAGE_RESOLUTIONS = ["2K", "4K"]
            legacy.IMAGE_ASPECT_RATIOS = ["9:16", "16:9", "4:3", "1:1"]
            legacy.normalize_image_resolution = _legacy_resolution
            legacy.normalize_image_aspect_ratio = _legacy_aspect
            legacy.generation_plugin.MOTION_CONTROL_CHARACTER_ORIENTATION = "video"
            previous_static(regression)
        finally:
            for field, value in settings_fields.items():
                setattr(settings, field, value)
            legacy.DEFAULT_MODELS = saved_models
            legacy.IMAGE_RESOLUTIONS = saved_resolutions
            legacy.IMAGE_ASPECT_RATIOS = saved_aspects
            legacy.normalize_image_resolution = saved_resolution_normalizer
            legacy.normalize_image_aspect_ratio = saved_aspect_normalizer
            legacy.generation_plugin.MOTION_CONTROL_CHARACTER_ORIENTATION = saved_orientation

        models = {item["code"]: item for item in adapter.legacy.DEFAULT_MODELS}

        lite = models["nano-banana"]["config"]
        name = regression.scenario("current Nano Banana 2 Lite contract")
        regression.check(name, lite.get("provider_model") == "gemini-3.1-flash-lite-image")
        regression.check(name, lite.get("fallback_model") == "nano-banana-2-lite")
        regression.check(name, lite.get("resolutions") == ["1K"], str(lite.get("resolutions")))
        regression.check(
            name,
            lite.get("aspect_ratios") == GEMINI_FLASH_LITE_ASPECT_RATIOS,
            str(lite.get("aspect_ratios")),
        )
        regression.check(name, lite.get("output_formats") == [])
        regression.check(name, lite.get("min_images") == 0)
        regression.check(name, lite.get("max_images") == 14)
        regression.check(name, lite.get("fallback_max_images") == 10)

        pro = models["nano-banana-pro"]["config"]
        name = regression.scenario("current Nano Banana Pro contract")
        regression.check(name, pro.get("provider_model") == "gemini-3-pro-image")
        regression.check(name, pro.get("resolutions") == ["1K", "2K", "4K"])
        regression.check(name, pro.get("aspect_ratios") == GEMINI_PRO_ASPECT_RATIOS)
        regression.check(name, pro.get("output_formats") == [])
        regression.check(name, pro.get("max_images") == 14)

        flash = models["nano-banana-2"]["config"]
        name = regression.scenario("current Nano Banana 2 contract")
        regression.check(name, flash.get("provider_model") == "gemini-3.1-flash-image")
        regression.check(name, flash.get("resolutions") == ["512", "1K", "2K", "4K"])
        regression.check(name, flash.get("aspect_ratios") == GEMINI_FLASH_ASPECT_RATIOS)
        regression.check(name, flash.get("output_formats") == [])
        regression.check(name, flash.get("max_images") == 14)

        seedance = models["seedance-2/video"]["config"]
        name = regression.scenario("current Seedance 2.0 contract")
        regression.check(name, seedance.get("provider_family") == "seedance")
        regression.check(name, seedance.get("fallback_model") == "bytedance/seedance-2")
        regression.check(name, seedance.get("durations") == SEEDANCE_DURATIONS)
        regression.check(name, seedance.get("aspect_ratios") == SEEDANCE_ASPECT_RATIOS)
        regression.check(name, seedance.get("resolutions") == ["480p", "720p", "1080p"])
        regression.check(name, seedance.get("min_images") == 0)
        regression.check(name, seedance.get("max_images") == 1)
        regression.check(name, seedance.get("fallback_max_images") == 2)

        kling_26 = models["kling-2.6/video"]["config"]
        name = regression.scenario("current Kling 2.6 Motion Control contract")
        regression.check(name, kling_26.get("provider_model") == "kling-2.6/motion-control")
        regression.check(name, "video/x-matroska" in kling_26.get("reference_video_mime_types", []))
        regression.check(name, kling_26.get("character_orientation") == "image")
        regression.check(name, kling_26.get("character_orientations") == ["image", "video"])

        kling_30 = models["kling-3.0/video"]["config"]
        name = regression.scenario("current Kling 3.0 Motion Control contract")
        regression.check(name, kling_30.get("provider_model") == "kling-3.0/motion-control")
        regression.check(name, kling_30.get("reference_video_mime_types") == ["video/mp4", "video/quicktime"])
        regression.check(name, kling_30.get("character_orientations") == ["image", "video"])
        regression.check(name, kling_30.get("min_reference_dimension_px") == 341)
        regression.check(name, kling_30.get("min_reference_aspect_ratio") == 0.4)
        regression.check(name, kling_30.get("max_reference_aspect_ratio") == 2.5)

    async def seeded_models(regression: Any, session_factory: Any) -> None:
        codes = [item["code"] for item in adapter.legacy.DEFAULT_MODELS]
        async with session_factory() as session:
            rows = list(
                await session.scalars(
                    adapter.legacy.select(adapter.legacy.GenerationModel).where(
                        adapter.legacy.GenerationModel.code.in_(codes)
                    )
                )
            )
        models = {row.code: row for row in rows}
        defaults = {item["code"]: item for item in adapter.legacy.DEFAULT_MODELS}
        for code in codes:
            name = regression.scenario(f"seeded generation model {code}")
            model = models.get(code)
            regression.check(name, model is not None, "model missing")
            if not model:
                continue
            expected = defaults[code]
            regression.check(name, model.title == expected["title"], model.title)
            regression.check(name, model.category == expected["category"], model.category)
            expected_config = expected.get("config") or {}
            for key, expected_value in expected_config.items():
                regression.check(
                    name,
                    model.config.get(key) == expected_value,
                    f"{key}={model.config.get(key)!r} expected={expected_value!r}",
                )

    adapter.LEGACY_STATIC_LOGIC = static_logic
    adapter.legacy._check_seeded_models = seeded_models
