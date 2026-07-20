from __future__ import annotations

from typing import Any
from urllib.parse import quote

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup, WebAppInfo
from fastapi import FastAPI, Request

from app.config import Settings

_ROUTE_MARKER = "_stupidbot_telegram_feed_config_installed"
_ORIGINAL_FEED_KEYBOARD: Any | None = None


def _positive_task_id(task_id: int) -> int:
    value = int(task_id)
    if value <= 0:
        raise ValueError("task_id must be positive")
    return value


def telegram_post_start_param(task_id: int) -> str:
    return f"post_{_positive_task_id(task_id)}"


def telegram_post_deep_link(settings: Settings, task_id: int) -> str:
    username = settings.telegram_bot_username.strip().lstrip("@") or "eva_nana_bot"
    start_param = telegram_post_start_param(task_id)
    return (
        f"https://t.me/{quote(username, safe='')}"
        f"?startapp={quote(start_param, safe='_-')}"
    )


def telegram_post_web_app_url(settings: Settings, task_id: int) -> str:
    separator = "&" if "?" in settings.mini_app_url else "?"
    return f"{settings.mini_app_url}{separator}post={_positive_task_id(task_id)}"


async def telegram_feed_config(request: Request) -> dict[str, str]:
    settings = request.app.state.context.settings
    username = settings.telegram_bot_username.strip().lstrip("@") or "eva_nana_bot"
    return {"telegram_bot_username": username}


def install_http_telegram_feed_config_route() -> None:
    """Register public Mini App link configuration on future FastAPI instances."""

    if getattr(FastAPI, _ROUTE_MARKER, False):
        return
    original_init = FastAPI.__init__

    def init_with_telegram_feed_config(self: FastAPI, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        path = "/api/tma/app/config"
        if not any(getattr(route, "path", None) == path for route in self.routes):
            self.add_api_route(
                path,
                telegram_feed_config,
                methods=["GET"],
                tags=["mini-app"],
                summary="Public Mini App configuration",
            )

    FastAPI.__init__ = init_with_telegram_feed_config  # type: ignore[method-assign]
    setattr(FastAPI, _ROUTE_MARKER, True)


def install_telegram_feed_links_patch(settings: Settings) -> None:
    """Open feed cards inside Telegram and expose Telegram-native share links."""

    from app.plugins.feed import plugin as feed_plugin

    global _ORIGINAL_FEED_KEYBOARD
    if _ORIGINAL_FEED_KEYBOARD is None:
        _ORIGINAL_FEED_KEYBOARD = feed_plugin._feed_keyboard

    original_keyboard = _ORIGINAL_FEED_KEYBOARD

    def feed_post_url(context: Any, task_id: int) -> str:
        return telegram_post_deep_link(context.settings, task_id)

    def feed_keyboard(
        task: Any,
        *,
        viewer_user_id: int,
        index: int,
        total: int,
        dislikes: int = 0,
        post_url: str | None = None,
    ) -> InlineKeyboardMarkup:
        markup = original_keyboard(
            task,
            viewer_user_id=viewer_user_id,
            index=index,
            total=total,
            dislikes=dislikes,
            post_url=None,
        )
        if not post_url:
            return markup

        rows = [list(row) for row in markup.inline_keyboard]
        rows.insert(
            min(2, len(rows)),
            [
                InlineKeyboardButton(
                    text="Открыть пост",
                    web_app=WebAppInfo(url=telegram_post_web_app_url(settings, task.id)),
                )
            ],
        )
        return InlineKeyboardMarkup(inline_keyboard=rows)

    feed_plugin._feed_post_url = feed_post_url
    feed_plugin._feed_keyboard = feed_keyboard
