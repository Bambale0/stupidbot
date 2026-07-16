from __future__ import annotations

from typing import Any

from app import db as app_db

CREATOR_PHOTO_CREDITS = 50
CREATOR_VIDEO_CREDITS = 20
CREATOR_DESCRIPTION = (
    "Гибридный пакет: 50 фото-кредитов и 20 видео-кредитов. "
    "Видео-баланс покрывает как минимум один запуск Seedance 2."
)
ONE_TIME_TERMS = (
    "Разовая покупка без автопродления. Кредиты зачисляются после подтверждения оплаты."
)
SUBSCRIPTION_CODE = "unlimited_30"
SUBSCRIPTION_TITLE = "Подписка на 30 дней"
SUBSCRIPTION_DESCRIPTION = (
    "Платная подписка с безлимитным доступом к включённым фото- и видео-моделям на 30 дней."
)
SUBSCRIPTION_TERMS = (
    "Разовая оплата без автопродления. Подписка активируется после подтверждения оплаты "
    "и действует 30 дней. Продлить её можно повторной покупкой."
)


def _is_legacy_unlimited_disable(statement: str) -> bool:
    normalized = " ".join(str(statement).lower().split())
    return (
        "update credit_packages" in normalized
        and "set is_enabled = false" in normalized
        and "where is_unlimited = true" in normalized
        and "duration_days" not in normalized
    )


# Older current-policy code disabled every unlimited package on every init_db call.
# Hybrid billing replaces that policy, so remove the legacy mutation before schema
# compatibility statements are executed. Invalid subscriptions are still hidden by
# package_is_user_visible when they have no positive duration.
app_db.SCHEMA_COMPAT_SQL = tuple(
    statement
    for statement in app_db.SCHEMA_COMPAT_SQL
    if not _is_legacy_unlimited_disable(statement)
)

HYBRID_BILLING_COMPAT_SQL: tuple[str, ...] = (
    f"""
    UPDATE credit_packages
    SET title = '{SUBSCRIPTION_TITLE}',
        description = '{SUBSCRIPTION_DESCRIPTION}',
        terms = '{SUBSCRIPTION_TERMS}',
        is_enabled = TRUE,
        is_unlimited = TRUE,
        duration_days = 30
    WHERE code = '{SUBSCRIPTION_CODE}'
      AND title = 'Безлимит на 30 дней'
    """,
)

if not any(SUBSCRIPTION_CODE in statement for statement in app_db.SCHEMA_COMPAT_SQL):
    app_db.SCHEMA_COMPAT_SQL = (*app_db.SCHEMA_COMPAT_SQL, *HYBRID_BILLING_COMPAT_SQL)


def install_billing_catalog_patches(repositories: Any) -> None:
    """Normalize default hybrid tariffs before repository seeding runs."""

    if getattr(repositories, "_billing_catalog_patches_installed", False):
        return

    for package in repositories.DEFAULT_PACKAGES:
        code = str(package.get("code") or "")
        if code == "starter":
            package["terms"] = ONE_TIME_TERMS
        elif code == "creator":
            package.update(
                {
                    "description": CREATOR_DESCRIPTION,
                    "terms": ONE_TIME_TERMS,
                    "credits": 0,
                    "photo_credits": CREATOR_PHOTO_CREDITS,
                    "video_credits": CREATOR_VIDEO_CREDITS,
                }
            )
        elif code == SUBSCRIPTION_CODE:
            package.update(
                {
                    "title": SUBSCRIPTION_TITLE,
                    "description": SUBSCRIPTION_DESCRIPTION,
                    "terms": SUBSCRIPTION_TERMS,
                    "credits": 0,
                    "photo_credits": 0,
                    "video_credits": 0,
                    "is_unlimited": True,
                    "duration_days": 30,
                    "is_enabled": True,
                }
            )

    original_should_sync = repositories._should_sync_default_package_split

    def should_sync_default_package_split(package: Any, defaults: dict[str, Any]) -> bool:
        if str(getattr(package, "code", "") or "") == "creator":
            common = int(getattr(package, "credits", 0) or 0)
            photo = int(getattr(package, "photo_credits", 0) or 0)
            video = int(getattr(package, "video_credits", 0) or 0)
            legacy_layout = (
                (common == 50 and photo == 0 and video == 0)
                or (common == 0 and photo == 50 and video == 10)
            )
            if legacy_layout and int(defaults.get("video_credits") or 0) >= CREATOR_VIDEO_CREDITS:
                return True
        return original_should_sync(package, defaults)

    repositories._should_sync_default_package_split = should_sync_default_package_split
    repositories._billing_catalog_patches_installed = True
