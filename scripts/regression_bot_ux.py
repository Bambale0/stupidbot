from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

if __package__ in {None, ""}:
    from _bootstrap import add_project_root_to_path

    add_project_root_to_path()

from app.bot import register_bot_commands
from app.models import CreditPackage
from app.plugins.admin import plugin as admin_plugin
from app.plugins.ux import plugin as ux_plugin
from app.ui import (
    account_menu,
    main_menu,
    mini_app_keyboard,
    navigation_keyboard,
    packages_keyboard,
)


def _buttons(markup) -> list:
    return [button for row in markup.inline_keyboard for button in row]


def _texts(markup) -> list[str]:
    return [str(button.text) for button in _buttons(markup)]


def _callbacks(markup) -> list[str]:
    return [str(button.callback_data) for button in _buttons(markup) if button.callback_data]


def _assert_unique_buttons(markup, *, screen: str) -> None:
    texts = _texts(markup)
    assert len(texts) == len(set(texts)), f"{screen}: duplicate button labels: {texts}"
    callbacks = _callbacks(markup)
    assert len(callbacks) == len(set(callbacks)), f"{screen}: duplicate callbacks: {callbacks}"


class _FakeBot:
    def __init__(self) -> None:
        self.calls: list[tuple[list, object]] = []

    async def set_my_commands(self, commands, scope) -> None:
        self.calls.append((list(commands), scope))


def _check_public_navigation() -> None:
    main = main_menu(is_admin=False, mini_app_url="https://example.test/miniapp/")
    _assert_unique_buttons(main, screen="main")
    assert _texts(main) == [
        "Открыть студию",
        "Создать фото",
        "Создать видео",
        "Лента",
        "Профиль",
    ]
    assert _callbacks(main) == ["menu:image", "menu:motion", "menu:feed", "menu:account"]
    assert len(_buttons(main)) == 5

    account = account_menu(is_admin=True)
    _assert_unique_buttons(account, screen="account")
    assert _texts(account) == [
        "Баланс",
        "Пополнить",
        "Партнёрская программа",
        "Поддержка",
        "Админка",
        "Главная",
    ]
    assert len(_buttons(account)) <= 6
    assert "menu:more" not in _callbacks(account)

    mini_app = mini_app_keyboard("https://example.test/miniapp/")
    _assert_unique_buttons(mini_app, screen="mini_app")
    assert _texts(mini_app) == ["Открыть студию", "Главная"]
    assert sum(1 for button in _buttons(mini_app) if button.web_app) == 1
    assert not any(button.url for button in _buttons(mini_app))

    nav = navigation_keyboard(back_callback="menu:account")
    assert _texts(nav) == ["← Назад", "Главная"]
    assert _callbacks(nav) == ["menu:account", "menu:main"]


def _check_packages() -> None:
    package = CreditPackage(
        id=1,
        code="ux-package",
        title="Старт",
        photo_credits=10,
        video_credits=2,
        credits=0,
        price_rub=990,
        is_enabled=True,
        is_unlimited=False,
    )
    markup = packages_keyboard([package])
    _assert_unique_buttons(markup, screen="packages")
    assert _callbacks(markup)[0] == "pay:preview:1"
    assert "pay:package:1" not in _callbacks(markup)


def _check_admin_information_architecture() -> None:
    ux_plugin._install_admin_navigation()
    home = admin_plugin._admin_keyboard()
    _assert_unique_buttons(home, screen="admin_home")
    assert _texts(home) == [
        "Обзор",
        "Пользователи",
        "Генерации",
        "Платежи",
        "Каталог",
        "Партнёрка",
        "Коммуникации",
        "Система",
        "Главная",
    ]
    assert len(_buttons(home)) == 9
    assert "Финансы" not in _texts(home)
    assert "Начислить" not in _texts(home)
    assert "Бан / Разбан" not in _texts(home)

    sections = {
        "overview": ux_plugin._section_keyboard(
            [("Статистика", "admin:stats"), ("Аналитика", "admin:analytics"), ("Финансы", "admin:finance")]
        ),
        "catalog": ux_plugin._section_keyboard(
            [("Модели и цены", "admin:models"), ("Пакеты", "admin:packages"), ("Публичные работы", "admin:gallery")]
        ),
        "affiliate": ux_plugin._section_keyboard(
            [("Рефералы", "admin:referrals"), ("Заявки на вывод", "admin:withdrawals"), ("Партнёрские ссылки", "admin:partners")]
        ),
    }
    for name, markup in sections.items():
        _assert_unique_buttons(markup, screen=f"admin_{name}")
        assert len(_buttons(markup)) <= 5
        assert _callbacks(markup)[-2:] == ["admin:menu", "menu:main"]


def _check_source_contracts() -> None:
    feed_source = Path("app/plugins/feed/plugin.py").read_text(encoding="utf-8")
    gallery_source = Path("app/plugins/gallery/plugin.py").read_text(encoding="utf-8")
    core_source = Path("app/plugins/core/plugin.py").read_text(encoding="utf-8")
    finance_source = Path("app/plugins/finance/plugin.py").read_text(encoding="utf-8")

    assert "increment_feed_share" not in feed_source
    assert 'text=f"Share' not in feed_source
    assert "Галерея объединена с лентой" in gallery_source
    assert 'F.data.in_({"menu:account", "menu:more"})' in core_source
    assert "_install_admin_finance_button" not in finance_source

    ux_plugin._install_generation_navigation()
    ux_plugin._install_feed_refresh()
    from app.plugins.feed import plugin as feed_plugin
    from app.plugins.generation import plugin as generation_plugin

    assert getattr(generation_plugin._send_image_request_screen, "_ux_model_choice_installed", False)
    assert getattr(feed_plugin._refresh_feed_card, "_ux_edit_caption_installed", False)


async def _check_commands() -> None:
    bot = _FakeBot()
    settings = SimpleNamespace(admin_ids=[1])
    await register_bot_commands(bot, settings)
    assert len(bot.calls) == 2
    default_commands = [command.command for command in bot.calls[0][0]]
    admin_commands = [command.command for command in bot.calls[1][0]]
    assert default_commands == [
        "start",
        "menu",
        "app",
        "image",
        "motion",
        "feed",
        "balance",
        "packages",
        "partners",
        "help",
    ]
    assert "gallery" not in default_commands
    assert admin_commands[-2:] == ["admin", "finance"]


async def amain() -> None:
    _check_public_navigation()
    _check_packages()
    _check_admin_information_architecture()
    _check_source_contracts()
    await _check_commands()
    print("Bot UX regression passed")


if __name__ == "__main__":
    asyncio.run(amain())
