from __future__ import annotations

import os
from typing import Any


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _env_non_negative_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"{name} must be an integer") from exc
    if value < 0:
        raise RuntimeError(f"{name} must be non-negative")
    return value


def custom_credit_sales_enabled(settings: Any) -> bool:
    return bool(
        getattr(settings, "custom_credit_sales_enabled", None)
        if getattr(settings, "custom_credit_sales_enabled", None) is not None
        else _env_bool("CUSTOM_CREDIT_SALES_ENABLED", False)
    )


def unlimited_sales_enabled(settings: Any) -> bool:
    return bool(
        getattr(settings, "unlimited_sales_enabled", None)
        if getattr(settings, "unlimited_sales_enabled", None) is not None
        else _env_bool("UNLIMITED_SALES_ENABLED", False)
    )


def orphan_task_timeout_seconds(settings: Any) -> int:
    configured = getattr(settings, "orphan_task_timeout_seconds", None)
    if configured is not None:
        return max(60, int(configured))
    return max(60, _env_non_negative_int("ORPHAN_TASK_TIMEOUT_SECONDS", 15 * 60))


def photo_credit_value_kopecks(settings: Any) -> int:
    configured = getattr(settings, "photo_credit_value_kopecks", None)
    if configured is not None:
        return max(0, int(configured))
    return _env_non_negative_int("PHOTO_CREDIT_VALUE_KOPECKS", 0)


def video_credit_value_kopecks(settings: Any) -> int:
    configured = getattr(settings, "video_credit_value_kopecks", None)
    if configured is not None:
        return max(0, int(configured))
    return _env_non_negative_int("VIDEO_CREDIT_VALUE_KOPECKS", 0)


def validate_production_security(settings: Any) -> None:
    env = str(getattr(settings, "app_env", "local") or "local").strip().lower()
    if env not in {"prod", "production"}:
        return

    missing: list[str] = []
    required = {
        "TELEGRAM_BOT_TOKEN": getattr(settings, "telegram_bot_token", None),
        "TELEGRAM_SECRET_TOKEN": getattr(settings, "telegram_secret_token", None),
        "COMET_CALLBACK_SECRET": getattr(settings, "comet_callback_secret", None),
    }
    for name, value in required.items():
        if not value:
            missing.append(name)
    if missing:
        raise RuntimeError(
            "Production security configuration is incomplete: " + ", ".join(missing)
        )

    public_base_url = str(getattr(settings, "public_base_url", "") or "")
    if not public_base_url.startswith("https://"):
        raise RuntimeError("PUBLIC_BASE_URL must use HTTPS in production")
    if "example.com" in public_base_url:
        raise RuntimeError("PUBLIC_BASE_URL must not use example.com in production")
