from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI

from app.config import Settings
from app.models import GenerationTask
from app.plugins.feed import plugin as feed_plugin
from app.services.telegram_feed_links import (
    install_http_telegram_feed_config_route,
    install_telegram_feed_links_patch,
    telegram_post_deep_link,
    telegram_post_start_param,
    telegram_post_web_app_url,
)


def run_telegram_feed_links_regression() -> None:
    settings = Settings(
        _env_file=None,
        telegram_bot_username="@eva_nana_bot",
        public_base_url="https://banana.example",
        mini_app_path="/miniapp",
    )

    assert telegram_post_start_param(123) == "post_123"
    assert (
        telegram_post_deep_link(settings, 123)
        == "https://t.me/eva_nana_bot?startapp=post_123"
    )
    assert telegram_post_web_app_url(settings, 123) == "https://banana.example/miniapp/?post=123"

    install_http_telegram_feed_config_route()
    app = FastAPI()
    assert "/api/tma/app/config" in {getattr(route, "path", "") for route in app.routes}

    install_telegram_feed_links_patch(settings)
    task = GenerationTask(id=123, user_id=1, model_code="nano-banana", likes_count=0)
    markup = feed_plugin._feed_keyboard(
        task,
        viewer_user_id=2,
        index=0,
        total=1,
        post_url=telegram_post_deep_link(settings, task.id),
    )
    buttons = [button for row in markup.inline_keyboard for button in row]
    open_buttons = [button for button in buttons if button.text == "Открыть пост"]
    assert len(open_buttons) == 1
    assert open_buttons[0].url is None
    assert open_buttons[0].web_app is not None
    assert open_buttons[0].web_app.url == "https://banana.example/miniapp/?post=123"
    assert feed_plugin._feed_post_url(type("Context", (), {"settings": settings})(), 123) == (
        "https://t.me/eva_nana_bot?startapp=post_123"
    )

    project_root = Path(__file__).resolve().parents[1]
    feed_links_js = (project_root / "app/static/miniapp/assets/feed-posts-safe.js").read_text(
        encoding="utf-8"
    )
    miniapp_index = (project_root / "app/static/miniapp/index.html").read_text(
        encoding="utf-8"
    )
    bot_source = (project_root / "app/bot.py").read_text(encoding="utf-8")

    for contract in (
        "initDataUnsafe?.start_param",
        "tgWebAppStartParam",
        "?startapp=post_",
        "/api/tma/app/config",
        "parsePostId",
        "Telegram-ссылка на пост скопирована",
    ):
        assert contract in feed_links_js
    assert "feed-posts-safe.js?v=20260720-telegram1" in miniapp_index
    assert "install_http_telegram_feed_config_route()" in bot_source
    assert "install_telegram_feed_links_patch(context.settings)" in bot_source

    print("telegram feed links regression passed")


if __name__ == "__main__":
    run_telegram_feed_links_regression()
