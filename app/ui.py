from __future__ import annotations

from aiogram.types import InlineKeyboardMarkup, WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.models import CreditPackage, GenerationModel, PartnerLink

MAIN_MENU_CALLBACK = "menu:main"
ACCOUNT_MENU_CALLBACK = "menu:account"
BANANA_UNIT = "🍌"
MAIN_MENU_BUTTON_TEXT = "Главная"
BACK_BUTTON_TEXT = "← Назад"


def banana_amount(value: int | float | str) -> str:
    return f"{value} {BANANA_UNIT}"


def model_price_text(model: GenerationModel, *, short: bool = False) -> str:
    price = max(0, int(model.price_credits or 0))
    if model.category == "image":
        unit = "фото-кр." if short else "фото-кредитов"
    elif model.category == "video":
        unit = "видео-кр." if short else "видео-кредитов"
    else:
        unit = "кр." if short else "кредитов"
    return f"{price} {unit}"


def package_credits_text(package: CreditPackage, *, short: bool = False) -> str:
    parts: list[str] = []
    if package.is_unlimited:
        days = int(package.duration_days or 0)
        parts.append("∞" if short else f"безлимит на {days} д.")
    photo_credits = int(getattr(package, "photo_credits", 0) or 0)
    video_credits = int(getattr(package, "video_credits", 0) or 0)
    common_credits = int(package.credits or 0)
    if photo_credits > 0:
        parts.append(f"{photo_credits} фото")
    if video_credits > 0:
        parts.append(f"{video_credits} видео")
    if common_credits > 0:
        label = "кр." if short else "универсальных кредитов"
        parts.append(f"{common_credits} {label}")
    return " + ".join(parts) if parts else "0 кредитов"


def main_menu(is_admin: bool = False, mini_app_url: str | None = None) -> InlineKeyboardMarkup:
    del is_admin
    builder = InlineKeyboardBuilder()
    if mini_app_url:
        builder.button(text="Открыть студию", web_app=WebAppInfo(url=mini_app_url))
    builder.button(text="Создать фото", callback_data="menu:image")
    builder.button(text="Создать видео", callback_data="menu:motion")
    builder.button(text="Лента", callback_data="menu:feed")
    builder.button(text="Профиль", callback_data=ACCOUNT_MENU_CALLBACK)
    if mini_app_url:
        builder.adjust(1, 2, 2)
    else:
        builder.adjust(2, 2)
    return builder.as_markup()


def account_menu(is_admin: bool = False) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Баланс", callback_data="menu:balance")
    builder.button(text="Пополнить", callback_data="menu:packages")
    builder.button(text="Партнёрская программа", callback_data="menu:partners")
    builder.button(text="Поддержка", callback_data="menu:support")
    if is_admin:
        builder.button(text="Админка", callback_data="admin:menu")
    nav_count = add_navigation_buttons(builder, back_callback=MAIN_MENU_CALLBACK)
    rows = [2, 1, 1]
    if is_admin:
        rows.append(1)
    rows.append(nav_count)
    builder.adjust(*rows)
    return builder.as_markup()


def more_menu(is_admin: bool = False, mini_app_url: str | None = None) -> InlineKeyboardMarkup:
    """Backward-compatible alias for the former «Ещё» menu."""

    del mini_app_url
    return account_menu(is_admin=is_admin)


def balance_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Пополнить", callback_data="menu:packages")
    builder.button(text="Партнёрская программа", callback_data="menu:partners")
    nav_count = add_navigation_buttons(builder, back_callback=ACCOUNT_MENU_CALLBACK)
    builder.adjust(2, nav_count)
    return builder.as_markup()


def mini_app_keyboard(mini_app_url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Открыть студию", web_app=WebAppInfo(url=mini_app_url))
    builder.button(text=MAIN_MENU_BUTTON_TEXT, callback_data=MAIN_MENU_CALLBACK)
    builder.adjust(1, 1)
    return builder.as_markup()


def add_navigation_buttons(
    builder: InlineKeyboardBuilder,
    *,
    back_callback: str | None = None,
    home_callback: str = MAIN_MENU_CALLBACK,
) -> int:
    count = 0
    if back_callback and back_callback != home_callback:
        builder.button(text=BACK_BUTTON_TEXT, callback_data=back_callback)
        count += 1
    builder.button(text=MAIN_MENU_BUTTON_TEXT, callback_data=home_callback)
    return count + 1


def navigation_keyboard(
    *,
    back_callback: str | None = MAIN_MENU_CALLBACK,
    home_callback: str = MAIN_MENU_CALLBACK,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    nav_count = add_navigation_buttons(
        builder,
        back_callback=back_callback,
        home_callback=home_callback,
    )
    builder.adjust(nav_count)
    return builder.as_markup()


def model_keyboard(
    models: list[GenerationModel],
    prefix: str = "gen:model",
    back_callback: str = MAIN_MENU_CALLBACK,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for model in models:
        unit = "/сек" if (model.config or {}).get("price_unit") == "second" else ""
        builder.button(
            text=f"{model.title} · {model_price_text(model, short=True)}{unit}",
            callback_data=f"{prefix}:{model.code}",
        )
    has_image_models = any(model.category == "image" for model in models)
    if prefix == "gen:model" and has_image_models:
        builder.button(text="Мои референсы", callback_data="menu:references")
    nav_count = add_navigation_buttons(builder, back_callback=back_callback)
    rows = [1] * len(models)
    if prefix == "gen:model" and has_image_models:
        rows.append(1)
    rows.append(nav_count)
    builder.adjust(*rows)
    return builder.as_markup()


def options_keyboard(prefix: str, values: list[str], back: str | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for value in values:
        builder.button(text=value, callback_data=f"{prefix}:{value}")
    nav_count = add_navigation_buttons(builder, back_callback=back)
    builder.adjust(*_chunk_sizes(len(values), 3), nav_count)
    return builder.as_markup()


def packages_keyboard(packages: list[CreditPackage]) -> InlineKeyboardMarkup:
    visible_packages = [package for package in packages if not package.is_unlimited]
    builder = InlineKeyboardBuilder()
    for package in visible_packages:
        price = f"{float(package.price_rub):.0f} ₽"
        amount = package_credits_text(package, short=True)
        builder.button(
            text=f"{package.title} · {amount} · {price}",
            callback_data=f"pay:preview:{package.id}",
        )
    nav_count = add_navigation_buttons(builder, back_callback=ACCOUNT_MENU_CALLBACK)
    rows = [1] * len(visible_packages)
    rows.append(nav_count)
    builder.adjust(*rows)
    return builder.as_markup()


def partner_keyboard(links: list[PartnerLink]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for link in links:
        builder.button(text=link.title, callback_data=f"partner:open:{link.id}")
    nav_count = add_navigation_buttons(builder, back_callback=ACCOUNT_MENU_CALLBACK)
    builder.adjust(*([1] * len(links)), nav_count)
    return builder.as_markup()


def _chunk_sizes(count: int, size: int) -> list[int]:
    sizes = []
    remaining = count
    while remaining > 0:
        row_size = min(size, remaining)
        sizes.append(row_size)
        remaining -= row_size
    return sizes
