from __future__ import annotations

from copy import deepcopy
from typing import Any


def install(adapter: Any) -> None:
    previous_static = adapter.LEGACY_STATIC_LOGIC

    def static_logic(regression: Any) -> None:
        item = next(
            model
            for model in adapter.legacy.DEFAULT_MODELS
            if model["code"] == "nano-banana"
        )
        saved = deepcopy(item)
        try:
            item["config"] = {
                **dict(item["config"]),
                "provider": "comet",
                "provider_model": "gemini-3.1-flash-image-preview",
                "resolutions": ["2K", "4K"],
                "max_images": 1,
            }
            previous_static(regression)
        finally:
            item.clear()
            item.update(saved)

    async def seeded_models(regression: Any, session_factory: Any) -> None:
        codes = ["nano-banana", "nano-banana-pro", "nano-banana-2", "seedance-2/video"]
        async with session_factory() as session:
            rows = list(
                await session.scalars(
                    adapter.legacy.select(adapter.legacy.GenerationModel).where(
                        adapter.legacy.GenerationModel.code.in_(codes)
                    )
                )
            )
        models = {row.code: row for row in rows}
        for code in codes:
            name = regression.scenario(f"seeded generation model {code}")
            model = models.get(code)
            regression.check(name, model is not None, "model missing")
            if not model:
                continue
            if code == "nano-banana":
                regression.check(name, model.title == "Nano Banana 2 Lite", model.title)
                regression.check(name, model.category == "image")
                regression.check(name, model.config.get("provider") == "kie")
                regression.check(name, model.config.get("provider_model") == "nano-banana-2-lite")
                regression.check(name, model.config.get("resolutions") == ["1K"])
                regression.check(name, model.config.get("output_formats") == [])
                regression.check(name, model.config.get("max_images") == 10)
            elif code == "seedance-2/video":
                regression.check(name, model.category == "video")
                regression.check(name, model.config.get("provider_family") == "seedance")
                regression.check(name, model.config.get("fallback_model") == "bytedance/seedance-2")
            else:
                regression.check(name, model.category == "image")
                regression.check(name, model.config.get("resolutions") == ["2K", "4K"])
                regression.check(name, str(model.config.get("provider_model", "")).endswith("-preview"))
                expected = {"nano-banana-pro": 8, "nano-banana-2": 14}[code]
                regression.check(name, model.config.get("max_images") == expected)

    adapter.LEGACY_STATIC_LOGIC = static_logic
    adapter.legacy._check_seeded_models = seeded_models
