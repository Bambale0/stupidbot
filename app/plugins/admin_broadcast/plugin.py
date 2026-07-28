from __future__ import annotations

import asyncio
import logging
from contextlib import suppress
from datetime import datetime, timezone
from html import escape

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select

from app.context import AppContext
from app.db import session_scope
from app.models import Broadcast, User
from app.plugins.admin.plugin import AdminStates, _cancel_keyboard
from app.plugins.common import ensure_user_for_callback, ensure_user_for_message, is_admin_user

logger = logging.getLogger(__name__)
router = Router(name="admin_broadcast")

PROGRESS_TTL_SECONDS = 30 * 24 * 60 * 60
PROGRESS_UPDATE_EVERY = 20
DELIVERY_DELAY_SECONDS = 0.05


def _sent_key(broadcast_id: int) -> str:
    return f"broadcast:{broadcast_id}:sent"


def _failure_key(broadcast_id: int) -> str:
    return f"broadcast:{broadcast_id}:failures"


def _lock_key(broadcast_id: int) -> str:
    return f"broadcast:{broadcast_id}:lock"


def _admin_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Новая рассылка", callback_data="admin:broadcast:new")
    builder.button(text="Обновить", callback_data="admin:broadcast")
    builder.button(text="Назад", callback_data="admin:menu")
    builder.adjust(1, 1, 1)
    return builder.as_markup()


def _broadcast_keyboard(item: Broadcast) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if item.status in {"interrupted", "failed"}:
        builder.button(text="Продолжить", callback_data=f"admin:broadcast:resume:{item.id}")
    builder.button(text="Диагностика", callback_data=f"admin:broadcast:diagnostics:{item.id}")
    builder.button(text="К списку", callback_data="admin:broadcast")
    builder.adjust(1, 1, 1)
    return builder.as_markup()


async def _safe_answer(callback: CallbackQuery, *args, **kwargs) -> None:
    with suppress(TelegramBadRequest):
        await callback.answer(*args, **kwargs)


async def _require_admin_callback(callback: CallbackQuery, context: AppContext) -> bool:
    user = await ensure_user_for_callback(callback, context)
    if not is_admin_user(user, context):
        await _safe_answer(callback, "Нет доступа", show_alert=True)
        return False
    return True


async def _require_admin_message(message: Message, context: AppContext):
    user = await ensure_user_for_message(message, context)
    if not is_admin_user(user, context):
        await message.answer("Нет доступа.")
        return None
    return user


@router.callback_query(F.data == "admin:broadcast")
async def broadcast_list(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    if not await _require_admin_callback(callback, context):
        return
    async with session_scope(context.session_factory) as session:
        items = list(
            await session.scalars(select(Broadcast).order_by(Broadcast.created_at.desc()).limit(12))
        )
    lines = ["<b>Рассылки</b>", ""]
    if not items:
        lines.append("Рассылок пока нет.")
    for item in items:
        lines.append(
            f"#{item.id} · <b>{escape(item.status)}</b> · отправлено {int(item.sent_count or 0)} · "
            f"ошибок {int(item.fail_count or 0)}\n{escape(item.title)}"
        )
    builder = InlineKeyboardBuilder()
    builder.button(text="Новая рассылка", callback_data="admin:broadcast:new")
    for item in items:
        builder.button(text=f"#{item.id} · {item.status}", callback_data=f"admin:broadcast:item:{item.id}")
    builder.button(text="Назад", callback_data="admin:menu")
    builder.adjust(*([1] * (len(items) + 2)))
    if callback.message:
        await callback.message.answer("\n".join(lines), reply_markup=builder.as_markup())
    await _safe_answer(callback)


@router.callback_query(F.data == "admin:broadcast:new")
async def broadcast_new(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    if not await _require_admin_callback(callback, context):
        return
    await state.set_state(AdminStates.broadcast_text)
    if callback.message:
        await callback.message.answer(
            "Рассылка\n\nОтправьте текст сообщения. После предпросмотра рассылку можно запустить.",
            reply_markup=_cancel_keyboard("admin:broadcast"),
        )
    await _safe_answer(callback)


@router.message(StateFilter(AdminStates.broadcast_text), F.text)
async def broadcast_text(message: Message, context: AppContext, state: FSMContext) -> None:
    admin = await _require_admin_message(message, context)
    if not admin:
        return
    text = str(message.text or "").strip()
    if not text:
        await message.answer("Текст рассылки не может быть пустым.")
        return
    async with session_scope(context.session_factory) as session:
        recipients = await session.scalar(
            select(func.count()).select_from(User).where(User.is_blocked.is_(False))
        )
    await state.update_data(broadcast_text=text)
    builder = InlineKeyboardBuilder()
    builder.button(text="Запустить", callback_data="admin:broadcast:send")
    builder.button(text="Отмена", callback_data="admin:broadcast:discard")
    builder.adjust(1, 1)
    await message.answer(
        f"Предпросмотр рассылки\n\nПолучателей: <b>{int(recipients or 0)}</b>\n\n{escape(text)}",
        reply_markup=builder.as_markup(),
    )


@router.callback_query(F.data == "admin:broadcast:send")
async def broadcast_send(
    callback: CallbackQuery, context: AppContext, state: FSMContext, bot: Bot
) -> None:
    admin = await ensure_user_for_callback(callback, context)
    if not is_admin_user(admin, context):
        await _safe_answer(callback, "Нет доступа", show_alert=True)
        return
    data = await state.get_data()
    text = str(data.get("broadcast_text") or "").strip()
    if not text:
        await state.clear()
        await _safe_answer(callback, "Состояние потеряно. Начните заново.", show_alert=True)
        return
    async with session_scope(context.session_factory) as session:
        item = Broadcast(
            created_by_user_id=admin.id,
            title=f"Broadcast {datetime.now(timezone.utc):%Y-%m-%d %H:%M}",
            text=text,
            status="sending",
        )
        session.add(item)
        await session.flush()
        broadcast_id = item.id
    asyncio.create_task(_run_broadcast(context, bot, broadcast_id), name=f"broadcast-{broadcast_id}")
    await state.clear()
    if callback.message:
        await callback.message.answer(
            f"Рассылка #{broadcast_id} запущена. Прогресс доступен в разделе «Рассылки».",
            reply_markup=_admin_keyboard(),
        )
    await _safe_answer(callback)


@router.callback_query(F.data == "admin:broadcast:discard")
async def broadcast_discard(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    if not await _require_admin_callback(callback, context):
        return
    await state.clear()
    if callback.message:
        await callback.message.answer("Рассылка отменена.", reply_markup=_admin_keyboard())
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("admin:broadcast:item:"))
async def broadcast_item(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    if not await _require_admin_callback(callback, context):
        return
    try:
        broadcast_id = int(str(callback.data).rsplit(":", 1)[-1])
    except ValueError:
        await _safe_answer(callback, "Рассылка не найдена", show_alert=True)
        return
    async with session_scope(context.session_factory) as session:
        item = await session.get(Broadcast, broadcast_id)
    if not item:
        await _safe_answer(callback, "Рассылка не найдена", show_alert=True)
        return
    text = (
        f"<b>Рассылка #{item.id}</b>\n\n"
        f"Статус: <b>{escape(item.status)}</b>\n"
        f"Отправлено: <b>{int(item.sent_count or 0)}</b>\n"
        f"Ошибок: <b>{int(item.fail_count or 0)}</b>\n"
        f"Создана: <b>{item.created_at:%Y-%m-%d %H:%M}</b>\n\n"
        f"{escape(item.text[:1500])}"
    )
    if callback.message:
        await callback.message.answer(text, reply_markup=_broadcast_keyboard(item))
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("admin:broadcast:resume:"))
async def broadcast_resume(callback: CallbackQuery, context: AppContext, bot: Bot) -> None:
    if not await _require_admin_callback(callback, context):
        return
    try:
        broadcast_id = int(str(callback.data).rsplit(":", 1)[-1])
    except ValueError:
        await _safe_answer(callback, "Рассылка не найдена", show_alert=True)
        return
    async with session_scope(context.session_factory) as session:
        item = await session.get(Broadcast, broadcast_id, with_for_update=True)
        if not item:
            await _safe_answer(callback, "Рассылка не найдена", show_alert=True)
            return
        if item.status not in {"interrupted", "failed"}:
            await _safe_answer(callback, "Эту рассылку нельзя продолжить", show_alert=True)
            return
        item.status = "sending"
    asyncio.create_task(_run_broadcast(context, bot, broadcast_id), name=f"broadcast-{broadcast_id}")
    await _safe_answer(callback, "Продолжение запущено", show_alert=True)


@router.callback_query(F.data.startswith("admin:broadcast:diagnostics:"))
async def broadcast_diagnostics(callback: CallbackQuery, context: AppContext) -> None:
    if not await _require_admin_callback(callback, context):
        return
    try:
        broadcast_id = int(str(callback.data).rsplit(":", 1)[-1])
    except ValueError:
        await _safe_answer(callback, "Рассылка не найдена", show_alert=True)
        return
    failures = await context.redis.hgetall(_failure_key(broadcast_id))
    lines = [f"<b>Диагностика рассылки #{broadcast_id}</b>", ""]
    if not failures:
        lines.append("Ошибок доставки не зафиксировано.")
    else:
        for raw_user_id, raw_error in list(failures.items())[:30]:
            user_id = raw_user_id.decode() if isinstance(raw_user_id, bytes) else str(raw_user_id)
            error = raw_error.decode() if isinstance(raw_error, bytes) else str(raw_error)
            lines.append(f"<code>{escape(user_id)}</code> · {escape(error[:120])}")
        if len(failures) > 30:
            lines.append(f"…и ещё {len(failures) - 30}")
    if callback.message:
        await callback.message.answer("\n".join(lines), reply_markup=_admin_keyboard())
    await _safe_answer(callback)


async def _persist_progress(
    context: AppContext,
    broadcast_id: int,
    *,
    status: str,
    sent: int,
    failed: int,
    finished: bool = False,
) -> None:
    async with session_scope(context.session_factory) as session:
        item = await session.get(Broadcast, broadcast_id, with_for_update=True)
        if not item:
            return
        item.status = status
        item.sent_count = sent
        item.fail_count = failed
        if finished:
            item.sent_at = datetime.now(timezone.utc)


async def _run_broadcast(context: AppContext, bot: Bot, broadcast_id: int) -> None:
    lock_key = _lock_key(broadcast_id)
    acquired = await context.redis.set(lock_key, b"1", ex=60 * 60, nx=True)
    if not acquired:
        logger.info("broadcast_resume_ignored id=%s reason=already_running", broadcast_id)
        return
    try:
        async with session_scope(context.session_factory) as session:
            item = await session.get(Broadcast, broadcast_id)
            users = list(
                await session.scalars(
                    select(User).where(User.is_blocked.is_(False)).order_by(User.id)
                )
            )
        if not item:
            return
        sent_key = _sent_key(broadcast_id)
        failure_key = _failure_key(broadcast_id)
        sent = int(await context.redis.scard(sent_key) or 0)
        failed = int(await context.redis.hlen(failure_key) or 0)
        await _persist_progress(context, broadcast_id, status="sending", sent=sent, failed=failed)
        processed_since_update = 0
        for user in users:
            if await context.redis.sismember(sent_key, str(user.id)):
                continue
            try:
                await bot.send_message(user.telegram_id, item.text, parse_mode=None)
            except Exception as exc:  # isolate each recipient failure
                error_name = type(exc).__name__
                await context.redis.hset(failure_key, str(user.id), error_name)
                failed = int(await context.redis.hlen(failure_key) or 0)
                logger.warning(
                    "broadcast_delivery_failed broadcast_id=%s user_id=%s error=%s",
                    broadcast_id,
                    user.id,
                    error_name,
                )
            else:
                await context.redis.sadd(sent_key, str(user.id))
                await context.redis.hdel(failure_key, str(user.id))
                sent = int(await context.redis.scard(sent_key) or 0)
                failed = int(await context.redis.hlen(failure_key) or 0)
            processed_since_update += 1
            if processed_since_update >= PROGRESS_UPDATE_EVERY:
                await _persist_progress(
                    context, broadcast_id, status="sending", sent=sent, failed=failed
                )
                processed_since_update = 0
            await asyncio.sleep(DELIVERY_DELAY_SECONDS)
        await context.redis.expire(sent_key, PROGRESS_TTL_SECONDS)
        await context.redis.expire(failure_key, PROGRESS_TTL_SECONDS)
        await _persist_progress(
            context,
            broadcast_id,
            status="sent",
            sent=sent,
            failed=failed,
            finished=True,
        )
    except asyncio.CancelledError:
        sent = int(await context.redis.scard(_sent_key(broadcast_id)) or 0)
        failed = int(await context.redis.hlen(_failure_key(broadcast_id)) or 0)
        await _persist_progress(
            context, broadcast_id, status="interrupted", sent=sent, failed=failed
        )
        raise
    except Exception:
        logger.exception("broadcast_run_failed broadcast_id=%s", broadcast_id)
        sent = int(await context.redis.scard(_sent_key(broadcast_id)) or 0)
        failed = int(await context.redis.hlen(_failure_key(broadcast_id)) or 0)
        await _persist_progress(context, broadcast_id, status="failed", sent=sent, failed=failed)
    finally:
        await context.redis.delete(lock_key)


def setup(dispatcher: Dispatcher, context: AppContext) -> None:
    del context
    dispatcher.include_router(router)
