from __future__ import annotations

from html import escape

from aiogram import Dispatcher, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from app.context import AppContext
from app.db import session_scope
from app.models import Broadcast
from app.plugins.admin import plugin as admin_plugin
from app.plugins.common import ensure_user_for_callback, is_admin_user
from app.services.admin_hardening import (
    broadcast_runtime_state,
    broadcast_task_is_active,
    restart_broadcast,
)
from app.ui import add_navigation_buttons

router = Router(name="admin_ops")


async def _require_admin(callback: CallbackQuery, context: AppContext) -> bool:
    user = await ensure_user_for_callback(callback, context)
    if not is_admin_user(user, context):
        await admin_plugin._safe_answer(callback, "Нет доступа", show_alert=True)
        return False
    return True


def _status_marker(status: str) -> str:
    return {
        "draft": "📝",
        "queued": "⏳",
        "sending": "📤",
        "interrupted": "⏸",
        "failed": "❌",
        "sent": "✅",
    }.get(status, "•")


@router.callback_query(F.data.in_({"admin:broadcast", "adm:broadcast"}))
async def broadcast_dashboard(
    callback: CallbackQuery,
    context: AppContext,
    state: FSMContext,
) -> None:
    await state.clear()
    if not await _require_admin(callback, context):
        return
    async with session_scope(context.session_factory) as session:
        broadcasts = list(
            await session.scalars(
                select(Broadcast).order_by(Broadcast.created_at.desc()).limit(12)
            )
        )

    lines = [
        "📣 <b>Рассылки</b>",
        "",
        "Рассылка выполняется в фоне. Прерванную отправку можно продолжить без повторной доставки уже обработанным пользователям.",
        "",
    ]
    builder = InlineKeyboardBuilder()
    builder.button(text="➕ Новая рассылка", callback_data="admin:broadcast:new")
    for item in broadcasts:
        marker = _status_marker(str(item.status or "draft"))
        lines.append(
            f"{marker} <b>#{item.id}</b> · {escape(item.title or 'Рассылка')}\n"
            f"   статус: <b>{escape(item.status or 'draft')}</b> · "
            f"отправлено: <b>{int(item.sent_count or 0)}</b> · "
            f"ошибок: <b>{int(item.fail_count or 0)}</b>"
        )
        builder.button(
            text=f"{marker} #{item.id} · {item.status}",
            callback_data=f"admin:broadcast:detail:{item.id}",
        )
    nav_count = add_navigation_buttons(builder, back_callback="admin:menu")
    builder.adjust(1, *([1] * len(broadcasts)), nav_count)
    if callback.message:
        await callback.message.answer("\n".join(lines), reply_markup=builder.as_markup())
    await admin_plugin._safe_answer(callback)


@router.callback_query(F.data == "admin:broadcast:new")
async def new_broadcast(
    callback: CallbackQuery,
    context: AppContext,
    state: FSMContext,
) -> None:
    if not await _require_admin(callback, context):
        return
    await state.set_state(admin_plugin.AdminStates.broadcast_text)
    if callback.message:
        await callback.message.answer(
            "Новая рассылка\n\n"
            "Отправьте текст сообщения. Перед запуском будет показан предпросмотр и число получателей.",
            reply_markup=admin_plugin._cancel_keyboard("admin:broadcast"),
        )
    await admin_plugin._safe_answer(callback)


@router.callback_query(F.data.startswith("admin:broadcast:detail:"))
async def broadcast_detail(callback: CallbackQuery, context: AppContext) -> None:
    if not await _require_admin(callback, context):
        return
    broadcast_id_text = str(callback.data).removeprefix("admin:broadcast:detail:")
    if not broadcast_id_text.isdigit():
        await admin_plugin._safe_answer(callback, "Рассылка не найдена", show_alert=True)
        return
    broadcast_id = int(broadcast_id_text)
    async with session_scope(context.session_factory) as session:
        broadcast = await session.get(Broadcast, broadcast_id)
    if not broadcast:
        await admin_plugin._safe_answer(callback, "Рассылка не найдена", show_alert=True)
        return
    runtime = await broadcast_runtime_state(context, broadcast_id)
    failures = runtime.get("failures") or []
    failure_lines = [
        f"• user {int(item.get('user_id') or 0)} · {escape(str(item.get('error') or 'error'))}"
        for item in failures[-5:]
        if isinstance(item, dict)
    ]
    active = broadcast_task_is_active(broadcast_id)
    text = (
        f"{_status_marker(str(broadcast.status))} <b>Рассылка #{broadcast.id}</b>\n\n"
        f"{escape(broadcast.title or 'Рассылка')}\n"
        f"Статус: <b>{escape(broadcast.status or 'draft')}</b>\n"
        f"Активная задача: <b>{'да' if active else 'нет'}</b>\n"
        f"Отправлено: <b>{int(broadcast.sent_count or 0)}</b>\n"
        f"Ошибок: <b>{int(broadcast.fail_count or 0)}</b>\n"
        f"Последний user_id: <b>{int(runtime.get('last_user_id') or 0)}</b>\n"
        f"Попыток запуска: <b>{int(runtime.get('attempts') or 0)}</b>\n\n"
        f"Текст:\n<blockquote>{escape((broadcast.text or '')[:1200])}</blockquote>"
    )
    if failure_lines:
        text += "\n\nПоследние ошибки:\n" + "\n".join(failure_lines)

    builder = InlineKeyboardBuilder()
    has_resume = broadcast.status in {"interrupted", "failed", "queued", "sending"} and not active
    if has_resume:
        builder.button(
            text="▶️ Продолжить",
            callback_data=f"admin:broadcast:resume:{broadcast.id}",
        )
    nav_count = add_navigation_buttons(builder, back_callback="admin:broadcast")
    builder.adjust(*([1] if has_resume else []), nav_count)
    if callback.message:
        await callback.message.answer(text, reply_markup=builder.as_markup())
    await admin_plugin._safe_answer(callback)


@router.callback_query(F.data.startswith("admin:broadcast:resume:"))
async def broadcast_resume(callback: CallbackQuery, context: AppContext) -> None:
    if not await _require_admin(callback, context):
        return
    broadcast_id_text = str(callback.data).removeprefix("admin:broadcast:resume:")
    if not broadcast_id_text.isdigit() or context.bot is None:
        await admin_plugin._safe_answer(callback, "Рассылка недоступна", show_alert=True)
        return
    ok, message = await restart_broadcast(context, context.bot, int(broadcast_id_text))
    await admin_plugin._safe_answer(callback, message, show_alert=True)
    if ok and callback.message:
        await callback.message.answer(
            message,
            reply_markup=admin_plugin._back_admin_keyboard("admin:broadcast"),
        )


def setup(dispatcher: Dispatcher, context: AppContext) -> None:
    del context
    dispatcher.include_router(router)
