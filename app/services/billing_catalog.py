from __future__ import annotations

from typing import Any

CREATOR_PHOTO_CREDITS = 50
CREATOR_VIDEO_CREDITS = 20
CREATOR_DESCRIPTION = (
    "Гибридный пакет: 50 фото-кредитов и 20 видео-кредитов. "
    "Видео-баланс покрывает как минимум один запуск Seedance 2."
)
ONE_TIME_TERMS = (
    "Разовая покупка без автопродления. Кредиты зачисляются после подтверждения оплаты."
)


def install_billing_catalog_patches(repositories: Any) -> None:
    """Normalize default one-time tariffs before repository seeding runs."""

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
        elif bool(package.get("is_unlimited")):
            package["is_enabled"] = False

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
