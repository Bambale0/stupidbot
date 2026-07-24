from __future__ import annotations

from typing import Any

# Union values keep stale callbacks safe. User-facing keyboards are filtered by the
# selected model's own config in app.services.model_contracts.
IMAGE_RESOLUTIONS = ["512", "1K", "2K", "4K"]
IMAGE_ASPECT_RATIOS = [
    "auto",
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
]
DEFAULT_IMAGE_RESOLUTION = "1K"
DEFAULT_IMAGE_ASPECT_RATIO = "1:1"

GEMINI_FLASH_LITE_ASPECT_RATIOS = [
    "1:1",
    "3:2",
    "2:3",
    "3:4",
    "4:3",
    "4:5",
    "5:4",
    "9:16",
    "16:9",
    "21:9",
]
GEMINI_FLASH_ASPECT_RATIOS = [
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
]
GEMINI_PRO_ASPECT_RATIOS = [
    "1:1",
    "2:3",
    "3:2",
    "3:4",
    "4:3",
    "4:5",
    "5:4",
    "9:16",
    "16:9",
    "21:9",
]
SEEDANCE_ASPECT_RATIOS = ["21:9", "16:9", "4:3", "1:1", "3:4", "9:16"]
SEEDANCE_DURATIONS = [str(value) for value in range(4, 16)]

DEFAULT_MODELS: list[dict[str, Any]] = [
    {
        "code": "nano-banana",
        "title": "Nano Banana 2 Lite",
        "category": "image",
        "description": "Gemini 3.1 Flash Lite Image: 1K, до 14 референсов.",
        "position": 10,
        "price_credits": 2,
        "config": {
            "provider": "comet",
            "provider_family": "gemini-image",
            "provider_model": "gemini-3.1-flash-lite-image",
            "fallback_provider": "kie",
            "fallback_model": "nano-banana-2-lite",
            "documentation_url": "https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-lite-image",
            "aspect_ratios": GEMINI_FLASH_LITE_ASPECT_RATIOS,
            "resolutions": ["1K"],
            "default_aspect_ratio": "1:1",
            "default_resolution": "1K",
            "output_formats": ["png", "jpg"],
            "default_output_format": "png",
            "min_images": 0,
            "max_images": 14,
            "reference_mime_types": ["image/jpeg", "image/png", "image/webp"],
        },
    },
    {
        "code": "nano-banana-pro",
        "title": "Nano Banana Pro",
        "category": "image",
        "description": "Gemini 3 Pro Image: 1K/2K/4K, до 14 референсов.",
        "position": 20,
        "price_credits": 4,
        "config": {
            "provider": "comet",
            "provider_family": "gemini-image",
            "provider_model": "gemini-3-pro-image",
            "fallback_provider": "kie",
            "fallback_model": "nano-banana-pro",
            "documentation_url": "https://ai.google.dev/gemini-api/docs/image-generation",
            "aspect_ratios": GEMINI_PRO_ASPECT_RATIOS,
            "resolutions": ["1K", "2K", "4K"],
            "default_aspect_ratio": "1:1",
            "default_resolution": "1K",
            "output_formats": ["png", "jpg"],
            "default_output_format": "png",
            "min_images": 0,
            "max_images": 14,
            "reference_mime_types": ["image/jpeg", "image/png", "image/webp"],
        },
    },
    {
        "code": "nano-banana-2",
        "title": "Nano Banana 2",
        "category": "image",
        "description": "Gemini 3.1 Flash Image: 512/1K/2K/4K, до 14 референсов.",
        "position": 30,
        "price_credits": 3,
        "config": {
            "provider": "comet",
            "provider_family": "gemini-image",
            "provider_model": "gemini-3.1-flash-image",
            "fallback_provider": "kie",
            "fallback_model": "nano-banana-2",
            "documentation_url": "https://ai.google.dev/gemini-api/docs/models/gemini-3.1-flash-image",
            "aspect_ratios": GEMINI_FLASH_ASPECT_RATIOS,
            "resolutions": ["512", "1K", "2K", "4K"],
            "default_aspect_ratio": "1:1",
            "default_resolution": "1K",
            "output_formats": ["png", "jpg"],
            "default_output_format": "png",
            "min_images": 0,
            "max_images": 14,
            "reference_mime_types": ["image/jpeg", "image/png", "image/webp"],
        },
    },
    {
        "code": "kling-2.6/video",
        "title": "Kling 2.6 Motion Control",
        "category": "video",
        "description": "Один персонаж и одно motion-видео 3–30 сек. через KIE.",
        "position": 40,
        "price_credits": 12,
        "config": {
            "provider": "kie",
            "provider_family": "kling-motion-control",
            "provider_model": "kling-2.6/motion-control",
            "documentation_url": "https://docs.kie.ai/market/kling/motion-control",
            "price_unit": "second",
            "motion_control_mode": "720p",
            "character_orientation": "image",
            "character_orientations": ["image", "video"],
            "min_duration_seconds": 3,
            "max_duration_seconds": 30,
            "min_images": 1,
            "max_images": 1,
            "max_videos": 1,
            "reference_image_mime_types": ["image/jpeg", "image/png"],
            "reference_video_mime_types": [
                "video/mp4",
                "video/quicktime",
                "video/x-matroska",
            ],
            "max_reference_image_bytes": 10_000_000,
            "max_reference_video_bytes": 100_000_000,
        },
    },
    {
        "code": "kling-3.0/video",
        "title": "Kling 3.0 Motion Control",
        "category": "video",
        "description": "Один персонаж и одно motion-видео 3–30 сек. через KIE.",
        "position": 50,
        "price_credits": 16,
        "config": {
            "provider": "kie",
            "provider_family": "kling-motion-control",
            "provider_model": "kling-3.0/motion-control",
            "documentation_url": "https://docs.kie.ai/market/kling/motion-control-v3",
            "price_unit": "second",
            "motion_control_mode": "720p",
            "character_orientation": "image",
            "character_orientations": ["image", "video"],
            "background_source": "input_video",
            "min_duration_seconds": 3,
            "max_duration_seconds": 30,
            "min_images": 1,
            "max_images": 1,
            "max_videos": 1,
            "reference_image_mime_types": ["image/jpeg", "image/png"],
            "reference_video_mime_types": ["video/mp4", "video/quicktime"],
            "max_reference_image_bytes": 10_000_000,
            "max_reference_video_bytes": 100_000_000,
            "min_reference_dimension_px": 341,
            "min_reference_aspect_ratio": 0.4,
            "max_reference_aspect_ratio": 2.5,
        },
    },
    {
        "code": "seedance-2/video",
        "title": "Seedance 2.0",
        "category": "video",
        "description": "Text/Image-to-Video через Comet API с fallback на KIE.",
        "position": 60,
        "price_credits": 18,
        "config": {
            "provider": "comet",
            "provider_family": "seedance",
            "provider_model": "doubao-seedance-2-0",
            "fallback_provider": "kie",
            "fallback_model": "bytedance/seedance-2",
            "documentation_url": "https://www.cometapi.com/models/doubao/doubao-seedance-2-0/",
            "durations": SEEDANCE_DURATIONS,
            "default_duration": "5",
            "aspect_ratios": SEEDANCE_ASPECT_RATIOS,
            "default_aspect_ratio": "16:9",
            "resolutions": ["480p", "720p", "1080p"],
            "default_resolution": "720p",
            "min_images": 0,
            "max_images": 1,
            "reference_image_mime_types": ["image/jpeg", "image/png", "image/webp"],
            "input_reference_optional": True,
        },
    },
]

ALLOWED_MODEL_CODES = {item["code"] for item in DEFAULT_MODELS}
MINI_APP_IMAGE_MODELS = {item["code"] for item in DEFAULT_MODELS if item["category"] == "image"}
MINI_APP_VIDEO_MODELS = {item["code"] for item in DEFAULT_MODELS if item["category"] == "video"}
DEFAULT_MINI_APP_IMAGE_MODEL = "nano-banana-2"
DEFAULT_MINI_APP_VIDEO_MODEL = "seedance-2/video"


def model_default_config(model_code: str) -> dict[str, Any]:
    for item in DEFAULT_MODELS:
        if item["code"] == model_code:
            return dict(item.get("config") or {})
    return {}


def normalize_image_resolution(value: Any) -> str:
    resolution = str(value or DEFAULT_IMAGE_RESOLUTION)
    if resolution in IMAGE_RESOLUTIONS:
        return resolution
    return DEFAULT_IMAGE_RESOLUTION


def normalize_image_aspect_ratio(value: Any) -> str:
    aspect_ratio = str(value or DEFAULT_IMAGE_ASPECT_RATIO)
    if aspect_ratio in IMAGE_ASPECT_RATIOS:
        return aspect_ratio
    return DEFAULT_IMAGE_ASPECT_RATIO
