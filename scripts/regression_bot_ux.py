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


class _FakeStatusState:
    def __init__(self, data: dict[str, object]) -> None:
        self.data = dict(data)

    async def get_data(self) -> dict[str, object]:
        return dict(self.data)

    async def update_data(self, **updates: object) -> None:
        self.data.update(updates)


class _FakeStatusBot:
    def __init__(self, events: list[tuple[str, object]]) -> None:
        self.events = events

    async def edit_message_reply_markup(
        self,
        *,
        chat_id: int,
        message_id: int,
        reply_markup,
    ) -> None:
        self.events.append(
            (
                "disable-old-settings",
                {
                    "chat_id": chat_id,
                    "message_id": message_id,
                    "reply_markup": reply_markup,
                },
            )
        )


class _FakePromptMessage:
    def __init__(self, events: list[tuple[str, object]], bot: _FakeStatusBot) -> None:
        self.events = events
        self.bot = bot
        self.chat = SimpleNamespace(id=700)

    async def answer(self, text: str):
        self.events.append(("status-after-prompt", text))
        return SimpleNamespace(message_id=99)


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
    assert tuple(zip(_texts(home)[:-1], _callbacks(home)[:-1])) == ux_plugin.ADMIN_HOME_BUTTONS
    assert _texts(home)[-1] == "Главная"
    assert _callbacks(home)[-1] == "menu:main"
    assert len(_buttons(home)) == 10
    assert "Добавить тариф" in _texts(home)
    assert "admin:tariff:add" in _callbacks(home)
    assert "Финансы" not in _texts(home)
    assert "Начислить" not in _texts(home)
    assert "Бан / Разбан" not in _texts(home)

    expected_sections = {
        "admin:ux:overview": (
            "Обзор",
            ("Статистика", "Аналитика", "Финансы"),
            ("admin:stats", "admin:analytics", "admin:finance"),
        ),
        "admin:ux:catalog": (
            "Каталог",
            ("Добавить тариф", "Тарифы и подписки", "Модели и цены", "Публичные работы"),
            ("admin:tariff:add", "admin:packages", "admin:models", "admin:gallery"),
        ),
        "admin:ux:affiliate": (
            "Партнёрка",
            ("Рефералы", "Заявки на вывод", "Партнёрские ссылки"),
            ("admin:referrals", "admin:withdrawals", "admin:partners"),
        ),
        "admin:ux:communications": (
            "Коммуникации",
            ("Тексты и настройки", "Обращения", "Рассылка"),
            ("admin:settings", "admin:support", "admin:broadcast"),
        ),
        "admin:ux:system": (
            "Система",
            ("Логи ошибок",),
            ("admin:logs",),
        ),
    }
    assert set(ux_plugin.ADMIN_SECTIONS) == set(expected_sections)

    leaf_callbacks: list[str] = []
    for route, (expected_title, expected_texts, expected_callbacks) in expected_sections.items():
        title, description, items = ux_plugin.ADMIN_SECTIONS[route]
        assert title == expected_title
        assert description.strip()
        assert tuple(text for text, _ in items) == expected_texts
        assert tuple(callback for _, callback in items) == expected_callbacks
        markup = ux_plugin._section_keyboard(items)
        _assert_unique_buttons(markup, screen=route)
        assert len(_buttons(markup)) <= 6
        assert _callbacks(markup)[-2:] == ["admin:menu", "menu:main"]
        leaf_callbacks.extend(expected_callbacks)

    assert len(leaf_callbacks) == len(set(leaf_callbacks)), (
        "admin sections duplicate a leaf action: " + repr(leaf_callbacks)
    )
    assert "admin:orders" not in ux_plugin.ADMIN_SECTIONS["admin:ux:system"][2]


def _check_source_contracts() -> None:
    feed_source = Path("app/plugins/feed/plugin.py").read_text(encoding="utf-8")
    gallery_source = Path("app/plugins/gallery/plugin.py").read_text(encoding="utf-8")
    core_source = Path("app/plugins/core/plugin.py").read_text(encoding="utf-8")
    finance_source = Path("app/plugins/finance/plugin.py").read_text(encoding="utf-8")
    payments_source = Path("app/plugins/payments/plugin.py").read_text(encoding="utf-8")

    assert "increment_feed_share" not in feed_source
    assert 'text=f"Share' not in feed_source
    assert 'F.data.startswith("feed:dislike:")' in feed_source
    assert 'F.data.startswith("feed:profile:")' in feed_source
    assert "Галерея объединена с лентой" in gallery_source
    assert 'F.data.in_({"menu:account", "menu:more"})' in core_source
    assert "_install_admin_finance_button" not in finance_source
    assert 'F.data.startswith("pay:create:")' in payments_source
    assert 'F.data.startswith("pay:package:")' in payments_source

    ux_plugin._install_generation_navigation()
    ux_plugin._install_generation_status_after_prompt()
    ux_plugin._install_feed_refresh()
    from app.plugins.feed import plugin as feed_plugin
    from app.plugins.generation import plugin as generation_plugin

    assert getattr(generation_plugin._send_image_request_screen, "_ux_model_choice_installed", False)
    assert getattr(
        generation_plugin._submit_image_task_from_message,
        "_ux_status_after_prompt_installed",
        False,
    )
    assert getattr(feed_plugin._refresh_feed_card, "_ux_edit_caption_installed", False)


async def _check_generation_status_after_prompt() -> None:
    events: list[tuple[str, object]] = []
    bot = _FakeStatusBot(events)
    message = _FakePromptMessage(events, bot)
    state = _FakeStatusState({"status_message_id": 42})

    status_message_id = await ux_plugin._start_image_status_after_prompt(
        message,  # type: ignore[arg-type]
        state,  # type: ignore[arg-type]
        bot,  # type: ignore[arg-type]
    )

    assert status_message_id == 99
    assert state.data["status_message_id"] == 99
    assert events[0][0] == "status-after-prompt", events
    assert "Готовлю референсы" in str(events[0][1])
    assert events[1] == (
        "disable-old-settings",
        {"chat_id": 700, "message_id": 42, "reply_markup": None},
    )


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
    await _check_generation_status_after_prompt()
    await _check_commands()
    print("Bot UX regression passed")


if __name__ == "__main__":
    asyncio.run(amain())
