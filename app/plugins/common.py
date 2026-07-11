from __future__ import annotations

from typing import Any

from aiogram.dispatcher.event.bases import CancelHandler
from aiogram.types import CallbackQuery, Message

from app.context import AppContext
from app.db import session_scope
from app.models import User
from app.repositories import get_or_create_user


async def ensure_user_for_message(message: Message, context: AppContext) -> User:
    if not message.from_user:
        raise RuntimeError("Telegram message has no from_user")
    async with session_scope(context.session_factory) as session:
        user = await get_or_create_user(session, message.from_user, context.settings.admin_ids)
        await session.flush()
        await session.refresh(user)
        if user_is_blocked(user, context):
            await message.answer("Доступ ограничен.")
            raise CancelHandler()
        return user


async def ensure_user_for_callback(callback: CallbackQuery, context: AppContext) -> User:
    if not callback.from_user:
        raise RuntimeError("Telegram callback has no from_user")
    async with session_scope(context.session_factory) as session:
        user = await get_or_create_user(session, callback.from_user, context.settings.admin_ids)
        await session.flush()
        await session.refresh(user)
        if user_is_blocked(user, context):
            await callback.answer("Доступ ограничен", show_alert=True)
            raise CancelHandler()
        return user


def is_admin_user(user: User, context: AppContext) -> bool:
    return user.is_admin or user.telegram_id in context.settings.admin_ids


def user_is_blocked(user: User, context: AppContext) -> bool:
    return bool(user.is_blocked and not is_admin_user(user, context))


async def callback_answer(callback: CallbackQuery, text: str | None = None) -> None:
    try:
        await callback.answer(text)
    except Exception:
        pass


def mention_user(user: Any) -> str:
    if getattr(user, "username", None):
        return f"@{user.username}"
    name = " ".join(
        part for part in [getattr(user, "first_name", None), getattr(user, "last_name", None)] if part
    )
    return name or str(getattr(user, "telegram_id", "user"))
