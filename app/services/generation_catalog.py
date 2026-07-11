from __future__ import annotations

from typing import Any

IMAGE_RESOLUTIONS = ["2K", "4K"]
IMAGE_ASPECT_RATIOS = ["9:16", "16:9", "1:1", "4:3"]
DEFAULT_IMAGE_RESOLUTION = "2K"
DEFAULT_IMAGE_ASPECT_RATIO = "9:16"

DEFAULT_MODELS: list[dict[str, Any]] = [
    {
        "code": "nano-banana",
        "title": "Banana",
        "category": "image",
        "description": "Banana Image через Comet API.",
        "position": 10,
        "price_credits": 2,
        "config": {
            "provider": "comet",
            "provider_model": "gemini-3.1-flash-image-preview",
            "aspect_ratios": list(IMAGE_ASPECT_RATIOS),
            "resolutions": list(IMAGE_RESOLUTIONS),
            "output_formats": ["png", "jpg"],
            "max_images": 1,
        },
    },
    {
        "code": "nano-banana-pro",
        "title": "Banana Pro",
        "category": "image",
        "description": "Banana Pro через Comet API.",
        "position": 20,
        "price_credits": 4,
        "config": {
            "provider": "comet",
            "provider_model": "gemini-3-pro-image-preview",
            "aspect_ratios": list(IMAGE_ASPECT_RATIOS),
            "resolutions": list(IMAGE_RESOLUTIONS),
            "output_formats": ["png", "jpg"],
            "max_images": 8,
        },
    },
    {
        "code": "nano-banana-2",
        "title": "Banana 2",
        "category": "image",
        "description": "Banana 2 через Comet API.",
        "position": 30,
        "price_credits": 3,
        "config": {
            "provider": "comet",
            "provider_model": "gemini-3.1-flash-image-preview",
            "aspect_ratios": list(IMAGE_ASPECT_RATIOS),
            "resolutions": list(IMAGE_RESOLUTIONS),
            "output_formats": ["jpg", "png"],
            "max_images": 14,
        },
    },
    {
        "code": "kling-2.6/video",
        "title": "Kling 2.6",
        "category": "video",
        "description": "Kling 2.6 Motion Control через KIE. Цена указана за секунду видео-референса.",
        "position": 40,
        "price_credits": 12,
        "config": {
            "provider": "kie",
            "provider_family": "kling-motion-control",
            "provider_model": "kling-2.6/motion-control",
            "price_unit": "second",
            "motion_control_mode": "720p",
            "character_orientation": "video",
            "min_duration_seconds": 3,
            "max_duration_seconds": 30,
            "max_images": 1,
        },
    },
    {
        "code": "kling-3.0/video",
        "title": "Клинг 3 Std",
        "category": "video",
        "description": "Клинг 3 Std Motion Control через KIE. Цена указана за секунду видео-референса.",
        "position": 50,
        "price_credits": 16,
        "config": {
            "provider": "kie",
            "provider_family": "kling-motion-control",
            "provider_model": "kling-3.0/motion-control",
            "price_unit": "second",
            "motion_control_mode": "720p",
            "character_orientation": "video",
            "background_source": "input_video",
            "min_duration_seconds": 3,
            "max_duration_seconds": 30,
            "max_images": 1,
        },
    },
    {
        "code": "seedance-2/video",
        "title": "Seedance 2",
        "category": "video",
        "description": "Seedance 2 Image-to-Video через Comet API с fallback на KIE.",
        "position": 60,
        "price_credits": 18,
        "config": {
            "provider": "comet",
            "provider_family": "seedance",
            "provider_model": "doubao-seedance-2-0",
            "fallback_provider": "kie",
            "fallback_model": "bytedance/seedance-2",
            "durations": ["5", "10"],
            "aspect_ratios": ["16:9", "9:16", "1:1"],
            "default_aspect_ratio": "16:9",
            "resolutions": ["720p", "1080p"],
            "default_resolution": "720p",
            "max_images": 1,
        },
    },
]

ALLOWED_MODEL_CODES = {item["code"] for item in DEFAULT_MODELS}
MINI_APP_IMAGE_MODELS = {item["code"] for item in DEFAULT_MODELS if item["category"] == "image"}
MINI_APP_VIDEO_MODELS = {item["code"] for item in DEFAULT_MODELS if item["category"] == "video"}
DEFAULT_MINI_APP_IMAGE_MODEL = "nano-banana-2"
DEFAULT_MINI_APP_VIDEO_MODEL = "seedance-2/video"


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
