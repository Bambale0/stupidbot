from __future__ import annotations

import logging
import time
from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware, Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.fsm.storage.redis import RedisStorage
from aiogram.types import BotCommand, BotCommandScopeChat, BotCommandScopeDefault, TelegramObject
from redis.asyncio import Redis

from app.config import Settings
from app.context import AppContext
from app.readiness import install_http_readiness_route
from app.services.financial_settings import validate_production_security
from app.services.referrals import install_repository_patches

install_repository_patches()
install_http_readiness_route()

from app.plugins.loader import load_plugins  # noqa: E402

logger = logging.getLogger(__name__)


class ActionLoggingMiddleware(BaseMiddleware):
    async def __call__(self, handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]], event: TelegramObject, data: dict[str, Any]) -> Any:
        started = time.monotonic()
        action = _event_action(event)
        telegram_id = _event_telegram_id(event)
        try:
            result = await handler(event, data)
        except Exception:
            logger.exception("telegram_action_failed user_tg=%s action=%s duration_ms=%d", telegram_id, action, int((time.monotonic() - started) * 1000))
            raise
        logger.info("telegram_action_ok user_tg=%s action=%s duration_ms=%d", telegram_id, action, int((time.monotonic() - started) * 1000))
        return result


def _event_telegram_id(event: TelegramObject) -> int | None:
    user = getattr(event, "from_user", None)
    if user:
        return getattr(user, "id", None)
    message = getattr(event, "message", None)
    user = getattr(message, "from_user", None)
    return getattr(user, "id", None) if user else None


def _event_action(event: TelegramObject) -> str:
    data = getattr(event, "data", None)
    if data:
        return f"callback:{str(data)[:80]}"
    text = str(getattr(event, "text", "") or "").strip()
    if text.startswith("/"):
        return f"command:{text.split()[0][:40]}"
    web_app_data = getattr(getattr(event, "web_app_data", None), "data", None)
    if web_app_data:
        return "message:web_app_data"
    if getattr(event, "photo", None):
        return "message:photo"
    if getattr(event, "video", None):
        return "message:video"
    if getattr(event, "document", None):
        return "message:document"
    return "message:text" if text else event.__class__.__name__


def create_bot(settings: Settings) -> Bot:
    validate_production_security(settings)
    return Bot(token=settings.telegram_bot_token, default=DefaultBotProperties(parse_mode=ParseMode.HTML))


def create_dispatcher(context: AppContext, redis: Redis) -> Dispatcher:
    for required_plugin in ("feed", "finance"):
        if required_plugin not in context.settings.enabled_plugins:
            context.settings.enabled_plugins.append(required_plugin)
    # UX patches depend on all feature modules being loaded first.
    context.settings.enabled_plugins = [
        name for name in context.settings.enabled_plugins if name != "ux"
    ]
    context.settings.enabled_plugins.append("ux")
    dispatcher = Dispatcher(storage=RedisStorage(redis=redis))
    dispatcher.message.middleware(ActionLoggingMiddleware())
    dispatcher.callback_query.middleware(ActionLoggingMiddleware())
    dispatcher["context"] = context
    load_plugins(dispatcher, context)
    return dispatcher


async def register_bot_commands(bot: Bot, settings: Settings) -> None:
    default_commands = [
        BotCommand(command="start", description="Запустить бота"),
        BotCommand(command="menu", description="Главное меню"),
        BotCommand(command="app", description="Открыть студию BANANA"),
        BotCommand(command="image", description="Создать фото"),
        BotCommand(command="motion", description="Создать видео"),
        BotCommand(command="feed", description="Лента работ"),
        BotCommand(command="balance", description="Баланс"),
        BotCommand(command="packages", description="Пополнить кредиты"),
        BotCommand(command="partners", description="Партнёрская программа"),
        BotCommand(command="help", description="Помощь"),
    ]
    admin_commands = [
        *default_commands,
        BotCommand(command="admin", description="Управление проектом"),
        BotCommand(command="finance", description="Финансовая аналитика"),
    ]
    await bot.set_my_commands(default_commands, scope=BotCommandScopeDefault())
    for admin_id in settings.admin_ids:
        await bot.set_my_commands(admin_commands, scope=BotCommandScopeChat(chat_id=admin_id))
