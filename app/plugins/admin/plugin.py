from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from html import escape
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import func, select

from app.context import AppContext
from app.db import session_scope
from app.models import (
    AffiliateWithdrawal,
    BotSetting,
    Broadcast,
    CreditPackage,
    GalleryItem,
    GenerationModel,
    GenerationTask,
    Payment,
    PartnerLink,
    User,
)
from app.plugins.common import (
    ensure_user_for_callback,
    ensure_user_for_message,
    is_admin_user,
    mention_user,
)
from app.repositories import (
    ALLOWED_MODEL_CODES,
    apply_affiliate_commission,
    apply_package_snapshot_to_user,
    apply_package_to_user,
    normalize_ref_code,
    package_is_technical,
    payment_package_snapshot,
    stats_snapshot,
)
from app.ui import (
    add_navigation_buttons,
    main_menu,
    model_price_text,
    navigation_keyboard,
    package_credits_text,
)

router = Router(name="admin")


@dataclass(slots=True)
class PaymentMarkResult:
    ok: bool
    admin_text: str
    notify_chat_id: int | None = None
    notify_text: str | None = None


async def _safe_answer(callback: CallbackQuery, *args, **kwargs) -> None:
    with suppress(TelegramBadRequest):
        await callback.answer(*args, **kwargs)


class AdminStates(StatesGroup):
    user_lookup = State()
    user_adjust = State()
    grant_credits = State()
    block_user = State()
    model_field = State()
    model_price = State()
    package_field = State()
    package_add = State()
    payment_mark_paid = State()
    gallery_field = State()
    gallery_add = State()
    gallery_toggle = State()
    partner_field = State()
    partner_add = State()
    partner_toggle = State()
    setting_set = State()
    broadcast_text = State()


@router.message(Command("admin"))
@router.message(F.text == "Админка")
async def admin_entry(message: Message, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    user = await ensure_user_for_message(message, context)
    if not is_admin_user(user, context):
        await message.answer("Нет доступа.")
        return
    await message.answer(_admin_home_text(), reply_markup=_admin_keyboard())


async def _edit_or_answer_admin(
    callback: CallbackQuery,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    """APIX-style callback rendering: edit current card, fallback to a new message."""
    if not callback.message:
        return
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        message = str(exc).lower()
        if (
            "there is no text in the message to edit" not in message
            and "message is not modified" not in message
        ):
            raise
        if "message is not modified" in message:
            return
        await callback.message.answer(text, reply_markup=reply_markup)


@router.callback_query(F.data.in_({"admin:menu", "menu:admin", "adm:back"}))
async def admin_menu(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    if not await _require_admin_callback(callback, context):
        return
    await _edit_or_answer_admin(callback, _admin_home_text(), reply_markup=_admin_keyboard())
    await _safe_answer(callback)


@router.callback_query(F.data == "admin:cancel")
async def admin_cancel(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    if not await _require_admin_callback(callback, context):
        return
    await state.clear()
    await _edit_or_answer_admin(callback, "Действие отменено.", reply_markup=_admin_keyboard())
    await _safe_answer(callback)




@router.message(StateFilter(AdminStates), F.text.casefold().in_({"отмена", "cancel", "/cancel"}))
async def admin_state_cancel_message(message: Message, context: AppContext, state: FSMContext) -> None:
    if not await _require_admin_message(message, context):
        return
    await state.clear()
    await message.answer("Действие отменено.", reply_markup=_admin_keyboard())


@router.message(StateFilter(AdminStates), F.text.casefold().in_({"назад", "back", "домой", "главное меню", "/menu"}))
async def admin_state_back_message(message: Message, context: AppContext, state: FSMContext) -> None:
    if not await _require_admin_message(message, context):
        return
    await state.clear()
    await message.answer(_admin_home_text(), reply_markup=_admin_keyboard())


@router.message(StateFilter(AdminStates), ~F.text)
async def admin_state_wrong_message_type(message: Message, context: AppContext) -> None:
    if not await _require_admin_message(message, context):
        return
    await message.answer(
        "Нужен текстовый ответ. Отправьте текст, нажмите «Отмена» или напишите /cancel.",
        reply_markup=_cancel_keyboard("admin:menu"),
    )


@router.callback_query(F.data.in_({"admin:stats", "adm:stats"}))
async def admin_stats(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    if not await _require_admin_callback(callback, context):
        return
    async with session_scope(context.session_factory) as session:
        stats = await stats_snapshot(session)
    text = (
        "Статистика проекта\n\n"
        f"Пользователи: {stats['users']}\n"
        f"Все задачи: {stats['tasks']}\n"
        f"Успешные генерации: {stats['tasks_success']}\n"
        f"Оплаченные платежи: {stats['payments_paid']}"
    )
    if callback.message:
        await callback.message.answer(text, reply_markup=_back_admin_keyboard())
    await _safe_answer(callback)




@router.callback_query(F.data.in_({"admin:analytics", "adm:analytics"}))
async def admin_analytics(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    if not await _require_admin_callback(callback, context):
        return
    since_24h = datetime.now(timezone.utc) - timedelta(hours=24)
    async with session_scope(context.session_factory) as session:
        users_total = await session.scalar(select(func.count()).select_from(User))
        users_blocked = await session.scalar(select(func.count()).select_from(User).where(User.is_blocked.is_(True)))
        tasks_total = await session.scalar(select(func.count()).select_from(GenerationTask))
        tasks_24h = await session.scalar(select(func.count()).select_from(GenerationTask).where(GenerationTask.created_at >= since_24h))
        tasks_success = await session.scalar(select(func.count()).select_from(GenerationTask).where(GenerationTask.status == "success"))
        tasks_fail = await session.scalar(select(func.count()).select_from(GenerationTask).where(GenerationTask.status == "fail"))
        tasks_active = await session.scalar(select(func.count()).select_from(GenerationTask).where(GenerationTask.status.in_(["submitted", "waiting", "queuing", "generating", "submitting"])))
        paid_count = await session.scalar(select(func.count()).select_from(Payment).where(Payment.status == "paid"))
        paid_sum = await session.scalar(select(func.coalesce(func.sum(Payment.amount_kopecks), 0)).where(Payment.status == "paid"))
        pending_payments = await session.scalar(select(func.count()).select_from(Payment).where(Payment.status.in_(["created", "manual_pending"])))
    conversion = (int(tasks_success or 0) / int(tasks_total or 1)) * 100 if int(tasks_total or 0) else 0
    text = (
        "📈 Аналитика\n\n"
        f"Пользователи: <b>{int(users_total or 0)}</b> · заблокировано: <b>{int(users_blocked or 0)}</b>\n"
        f"Генерации всего: <b>{int(tasks_total or 0)}</b>\n"
        f"За 24 часа: <b>{int(tasks_24h or 0)}</b>\n"
        f"Успешно: <b>{int(tasks_success or 0)}</b> · ошибок: <b>{int(tasks_fail or 0)}</b> · активных: <b>{int(tasks_active or 0)}</b>\n"
        f"Success rate: <b>{conversion:.1f}%</b>\n\n"
        f"Оплаченных платежей: <b>{int(paid_count or 0)}</b>\n"
        f"Выручка: <b>{int(paid_sum or 0) / 100:.0f} ₽</b>\n"
        f"Ожидают оплаты/ручной обработки: <b>{int(pending_payments or 0)}</b>"
    )
    if callback.message:
        await callback.message.answer(text, reply_markup=_back_admin_keyboard())
    await _safe_answer(callback)


@router.callback_query(F.data.in_({"admin:orders", "admin:tasks", "admin:operations", "adm:orders", "adm:operations"}))
async def admin_orders(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    if not await _require_admin_callback(callback, context):
        return
    async with session_scope(context.session_factory) as session:
        tasks = list(await session.scalars(select(GenerationTask).order_by(GenerationTask.created_at.desc()).limit(12)))
    lines = ["🧾 Заказы / операции", ""]
    if not tasks:
        lines.append("Операций пока нет.")
    for task in tasks:
        lines.append(_task_summary_line(task))
    builder = InlineKeyboardBuilder()
    for task in tasks[:10]:
        builder.button(text=f"#{task.id} · {task.status}", callback_data=f"admin:task:{task.id}")
    nav_count = add_navigation_buttons(builder, back_callback="admin:menu")
    builder.adjust(*([1] * min(len(tasks), 10)), nav_count)
    if callback.message:
        await callback.message.answer("\n".join(lines), reply_markup=builder.as_markup())
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("admin:task:"))
async def admin_task_detail(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    if not await _require_admin_callback(callback, context):
        return
    try:
        task_id = int(callback.data.removeprefix("admin:task:"))
    except (TypeError, ValueError):
        await _safe_answer(callback, "Операция не найдена", show_alert=True)
        return
    async with session_scope(context.session_factory) as session:
        task = await session.get(GenerationTask, task_id)
        user = await session.get(User, task.user_id) if task else None
    if not task:
        await _safe_answer(callback, "Операция не найдена", show_alert=True)
        return
    if callback.message:
        await callback.message.answer(_task_detail_text(task, user), reply_markup=_task_detail_keyboard(task))
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("admin:task:retry:"))
async def admin_task_retry(callback: CallbackQuery, context: AppContext) -> None:
    if not await _require_admin_callback(callback, context):
        return
    try:
        task_id = int(callback.data.removeprefix("admin:task:retry:"))
    except (TypeError, ValueError):
        await _safe_answer(callback, "Операция не найдена", show_alert=True)
        return
    async with session_scope(context.session_factory) as session:
        task = await session.get(GenerationTask, task_id, with_for_update=True)
        if not task:
            await _safe_answer(callback, "Операция не найдена", show_alert=True)
            return
        if not task.provider_task_id:
            await _safe_answer(callback, "Нечего повторно проверять: нет provider_task_id", show_alert=True)
            return
        if task.status == "success":
            await _safe_answer(callback, "Операция уже завершена успешно", show_alert=True)
            return
        task.status = "waiting"
        task.error_message = None
        task.result_payload = {**dict(task.result_payload or {}), "admin_retry_requested_at": datetime.now(timezone.utc).isoformat()}
    await _safe_answer(callback, "Операция поставлена на повторную проверку", show_alert=True)


@router.callback_query(F.data.in_({"admin:support", "adm:support"}))
async def admin_support(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    if not await _require_admin_callback(callback, context):
        return
    text = (
        "🆘 Обращения\n\n"
        "В StupidBot пока нет отдельной таблицы тикетов.\n"
        "Сейчас обращения обрабатываются через личные сообщения/поддержку и карточки пользователей.\n\n"
        "Что доступно сейчас:\n"
        "• найти пользователя;\n"
        "• посмотреть его баланс/платежи/операции;\n"
        "• заблокировать/разблокировать;\n"
        "• начислить кредиты;\n"
        "• отправить уведомление через рассылку.\n\n"
        "Для полноценной очереди обращений нужен отдельный SupportTicket model + команда/кнопка создания тикета."
    )
    if callback.message:
        await callback.message.answer(text, reply_markup=_support_admin_keyboard())
    await _safe_answer(callback)


@router.callback_query(F.data.in_({"admin:logs", "adm:logs"}))
async def admin_error_logs(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    if not await _require_admin_callback(callback, context):
        return
    text = await _recent_error_logs_text()
    if callback.message:
        await callback.message.answer(text, reply_markup=_back_admin_keyboard())
    await _safe_answer(callback)


@router.callback_query(F.data == "admin:users")
async def admin_users(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    if not await _require_admin_callback(callback, context):
        return
    async with session_scope(context.session_factory) as session:
        users = list(await session.scalars(select(User).order_by(User.created_at.desc()).limit(10)))
    builder = InlineKeyboardBuilder()
    lines = ["Пользователи\n\nПоследние 10:"]
    for user in users:
        status = "заблокирован" if user.is_blocked else "активен"
        role = "admin" if user.is_admin else "user"
        lines.append(
            f"{user.telegram_id} · {escape(mention_user(user))} · "
            f"фото {int(user.photo_credits_balance or 0)} · "
            f"видео {int(user.video_credits_balance or 0)} · "
            f"унив. {int(user.credits_balance or 0)} · {role} · {status}"
        )
        builder.button(
            text=f"{mention_user(user)} · {user.telegram_id}", callback_data=f"admin:user:{user.id}"
        )
    builder.button(text="Найти пользователя", callback_data="admin:users:find")
    builder.button(text="Начислить универсальные по Telegram ID", callback_data="admin:users:grant")
    builder.button(text="Блокировка по Telegram ID", callback_data="admin:users:block")
    nav_count = add_navigation_buttons(builder, back_callback="admin:menu")
    builder.adjust(*([1] * len(users)), 1, 1, 1, nav_count)
    if callback.message:
        await callback.message.answer("\n".join(lines), reply_markup=builder.as_markup())
    await _safe_answer(callback)


@router.callback_query(F.data == "admin:users:find")
async def admin_user_lookup_prompt(
    callback: CallbackQuery, context: AppContext, state: FSMContext
) -> None:
    if not await _require_admin_callback(callback, context):
        return
    await state.set_state(AdminStates.user_lookup)
    if callback.message:
        await callback.message.answer(
            "Поиск пользователя\n\n"
            "Отправьте Telegram ID, внутренний ID, @username или партнерский код.",
            reply_markup=_cancel_keyboard("admin:users"),
        )
    await _safe_answer(callback)


@router.message(AdminStates.user_lookup, F.text)
async def admin_user_lookup_apply(message: Message, context: AppContext, state: FSMContext) -> None:
    if not await _require_admin_message(message, context):
        return
    query = message.text.strip()
    async with session_scope(context.session_factory) as session:
        user = await _find_user(session, query)
        if not user:
            await message.answer(
                "Пользователь не найден.", reply_markup=_cancel_keyboard("admin:users")
            )
            return
        user_id = user.id
    await state.clear()
    await _send_user_detail(message, context, user_id)


@router.callback_query(
    F.data.func(
        lambda data: bool(
            data and data.startswith("admin:user:") and data.removeprefix("admin:user:").isdigit()
        )
    )
)
async def admin_user_detail(
    callback: CallbackQuery, context: AppContext, state: FSMContext
) -> None:
    await state.clear()
    if not await _require_admin_callback(callback, context):
        return
    user_id = int(callback.data.removeprefix("admin:user:"))
    if callback.message:
        await _send_user_detail(callback.message, context, user_id)
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("admin:user:toggle_block:"))
async def admin_user_toggle_block(callback: CallbackQuery, context: AppContext) -> None:
    if not await _require_admin_callback(callback, context):
        return
    user_id = int(callback.data.removeprefix("admin:user:toggle_block:"))
    async with session_scope(context.session_factory) as session:
        user = await session.get(User, user_id)
        if not user:
            await _safe_answer(callback, "Пользователь не найден", show_alert=True)
            return
        user.is_blocked = not user.is_blocked
        status = "заблокирован" if user.is_blocked else "разблокирован"
    await _safe_answer(callback, status, show_alert=True)
    if callback.message:
        await _send_user_detail(callback.message, context, user_id)


@router.callback_query(F.data.startswith("admin:user:toggle_admin:"))
async def admin_user_toggle_admin(callback: CallbackQuery, context: AppContext) -> None:
    admin = await ensure_user_for_callback(callback, context)
    if not is_admin_user(admin, context):
        await _safe_answer(callback, "Нет доступа", show_alert=True)
        return
    user_id = int(callback.data.removeprefix("admin:user:toggle_admin:"))
    async with session_scope(context.session_factory) as session:
        user = await session.get(User, user_id)
        if not user:
            await _safe_answer(callback, "Пользователь не найден", show_alert=True)
            return
        if user.telegram_id in context.settings.admin_ids and user.is_admin:
            await _safe_answer(callback, "Админ из .env останется админом", show_alert=True)
            return
        user.is_admin = not user.is_admin
        status = "админ" if user.is_admin else "обычный пользователь"
    await _safe_answer(callback, status, show_alert=True)
    if callback.message:
        await _send_user_detail(callback.message, context, user_id)


@router.callback_query(F.data.startswith("admin:user:adjust:"))
async def admin_user_adjust_prompt(
    callback: CallbackQuery, context: AppContext, state: FSMContext
) -> None:
    if not await _require_admin_callback(callback, context):
        return
    raw = callback.data.removeprefix("admin:user:adjust:")
    action, _, user_id_text = raw.partition(":")
    if (
        action not in {"credits", "photo_credits", "video_credits", "unlimited", "affiliate_rate"}
        or not user_id_text.isdigit()
    ):
        await _safe_answer(callback, "Действие недоступно", show_alert=True)
        return
    user_id = int(user_id_text)
    await state.set_state(AdminStates.user_adjust)
    await state.update_data(user_id=user_id, user_action=action)
    if callback.message:
        if action == "credits":
            text = (
                "Изменить универсальные кредиты\n\n"
                "Отправьте число кредитов. Можно положительное или отрицательное.\n"
                "Пример: 20 или -5"
            )
        elif action == "photo_credits":
            text = (
                "Изменить фото-кредиты\n\n"
                "Отправьте число кредитов. Можно положительное или отрицательное.\n"
                "Пример: 20 или -5"
            )
        elif action == "video_credits":
            text = (
                "Изменить видео-кредиты\n\n"
                "Отправьте число кредитов. Можно положительное или отрицательное.\n"
                "Пример: 20 или -5"
            )
        elif action == "unlimited":
            text = "Выдать безлимит\n\nОтправьте количество дней. 0 снимет безлимит.\nПример: 30"
        else:
            text = (
                "Ставка партнерки\n\n"
                "Отправьте процент комиссии: 30 для обычной рефералки или 50 для амбасадора.\n"
                "Допустимый диапазон: 0-100."
            )
        await callback.message.answer(text, reply_markup=_cancel_keyboard(f"admin:user:{user_id}"))
    await _safe_answer(callback)


@router.message(AdminStates.user_adjust, F.text)
async def admin_user_adjust_apply(message: Message, context: AppContext, state: FSMContext) -> None:
    if not await _require_admin_message(message, context):
        return
    data = await state.get_data()
    user_id = int(data["user_id"])
    action = str(data["user_action"])
    try:
        amount = int(message.text.strip())
    except ValueError:
        await message.answer(
            "Введите целое число.", reply_markup=_cancel_keyboard(f"admin:user:{user_id}")
        )
        return
    async with session_scope(context.session_factory) as session:
        user = await session.get(User, user_id)
        if not user:
            await message.answer(
                "Пользователь не найден.", reply_markup=_cancel_keyboard("admin:users")
            )
            return
        if action == "credits":
            user.credits_balance = max(0, int(user.credits_balance or 0) + amount)
            result = f"Универсальные кредиты обновлены: {int(user.credits_balance or 0)}."
        elif action == "photo_credits":
            user.photo_credits_balance = max(0, int(user.photo_credits_balance or 0) + amount)
            result = f"Фото-кредиты обновлены: {int(user.photo_credits_balance or 0)}."
        elif action == "video_credits":
            user.video_credits_balance = max(0, int(user.video_credits_balance or 0) + amount)
            result = f"Видео-кредиты обновлены: {int(user.video_credits_balance or 0)}."
        elif action == "unlimited":
            if amount <= 0:
                user.unlimited_until = None
                result = "Безлимит снят."
            else:
                now = datetime.now(timezone.utc)
                base = (
                    user.unlimited_until
                    if user.unlimited_until and user.unlimited_until > now
                    else now
                )
                user.unlimited_until = base + timedelta(days=amount)
                result = f"Безлимит до {user.unlimited_until:%Y-%m-%d %H:%M}."
        else:
            if amount < 0 or amount > 100:
                await message.answer(
                    "Введите процент от 0 до 100.",
                    reply_markup=_cancel_keyboard(f"admin:user:{user_id}"),
                )
                return
            user.affiliate_commission_rate_bps = amount * 100
            result = f"Ставка партнерки обновлена: {amount}%."
    await state.clear()
    await message.answer(result, reply_markup=_admin_keyboard())
    await _send_user_detail(message, context, user_id)


@router.callback_query(F.data.in_({"admin:users:grant", "adm:add_credits"}))
async def admin_grant_prompt(
    callback: CallbackQuery, context: AppContext, state: FSMContext
) -> None:
    if not await _require_admin_callback(callback, context):
        return
    await state.set_state(AdminStates.grant_credits)
    if callback.message:
        await callback.message.answer(
            "Начислить универсальные кредиты\n\n"
            "Отправьте Telegram ID пользователя и количество кредитов через пробел.\n"
            "Пример: 339795159 20",
            reply_markup=_cancel_keyboard("admin:users"),
        )
    await _safe_answer(callback)


@router.message(AdminStates.grant_credits, F.text)
async def admin_grant_apply(message: Message, context: AppContext, state: FSMContext) -> None:
    if not await _require_admin_message(message, context):
        return
    try:
        telegram_id_text, amount_text = message.text.split(maxsplit=1)
        telegram_id = int(telegram_id_text)
        amount = int(amount_text)
    except ValueError:
        await message.answer(
            "Не понял. Пример правильного ввода: 339795159 20",
            reply_markup=_cancel_keyboard("admin:users"),
        )
        return
    async with session_scope(context.session_factory) as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if not user:
            await message.answer(
                "Пользователь не найден.", reply_markup=_cancel_keyboard("admin:users")
            )
            return
        user.credits_balance = max(0, int(user.credits_balance or 0) + amount)
        balance = user.credits_balance
    await state.clear()
    await message.answer(
        f"Готово. Новый универсальный баланс {telegram_id}: {balance} кредитов.",
        reply_markup=_admin_keyboard(),
    )


@router.callback_query(F.data.in_({"admin:users:block", "adm:ban"}))
async def admin_block_prompt(
    callback: CallbackQuery, context: AppContext, state: FSMContext
) -> None:
    if not await _require_admin_callback(callback, context):
        return
    await state.set_state(AdminStates.block_user)
    if callback.message:
        await callback.message.answer(
            "Блокировка пользователя\n\n"
            "Отправьте Telegram ID и действие:\n"
            "on - заблокировать\n"
            "off - разблокировать\n\n"
            "Пример: 339795159 on",
            reply_markup=_cancel_keyboard("admin:users"),
        )
    await _safe_answer(callback)


@router.message(AdminStates.block_user, F.text)
async def admin_block_apply(message: Message, context: AppContext, state: FSMContext) -> None:
    if not await _require_admin_message(message, context):
        return
    try:
        telegram_id_text, status = message.text.split(maxsplit=1)
        telegram_id = int(telegram_id_text)
    except ValueError:
        await message.answer(
            "Не понял. Пример правильного ввода: 339795159 on",
            reply_markup=_cancel_keyboard("admin:users"),
        )
        return
    async with session_scope(context.session_factory) as session:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if not user:
            await message.answer(
                "Пользователь не найден.", reply_markup=_cancel_keyboard("admin:users")
            )
            return
        user.is_blocked = status.strip().lower() in {"on", "1", "true", "yes"}
    await state.clear()
    await message.answer("Статус обновлен.", reply_markup=_admin_keyboard())


@router.callback_query(F.data.in_({"admin:models", "adm:models"}))
async def admin_models(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    if not await _require_admin_callback(callback, context):
        return
    async with session_scope(context.session_factory) as session:
        models = list(
            await session.scalars(
                select(GenerationModel)
                .where(GenerationModel.code.in_(ALLOWED_MODEL_CODES))
                .order_by(GenerationModel.position)
            )
        )
    builder = InlineKeyboardBuilder()
    lines = ["Модели генерации\n\nВыберите модель, чтобы изменить цену или включить/выключить:"]
    for model in models:
        flag = "включена" if model.is_enabled else "выключена"
        lines.append(f"{model.id}. {escape(model.title)} · {model_price_text(model)} · {flag}")
        builder.button(text=f"{model.id}. {model.title}", callback_data=f"admin:model:{model.id}")
    nav_count = add_navigation_buttons(builder, back_callback="admin:menu")
    builder.adjust(*([1] * len(models)), nav_count)
    if callback.message:
        await callback.message.answer("\n".join(lines), reply_markup=builder.as_markup())
    await _safe_answer(callback)


@router.callback_query(
    F.data.func(
        lambda data: bool(
            data and data.startswith("admin:model:") and data.removeprefix("admin:model:").isdigit()
        )
    )
)
async def admin_model_detail(
    callback: CallbackQuery, context: AppContext, state: FSMContext
) -> None:
    await state.clear()
    if not await _require_admin_callback(callback, context):
        return
    model_id = int(callback.data.removeprefix("admin:model:"))
    async with session_scope(context.session_factory) as session:
        model = await session.get(GenerationModel, model_id)
        if not model:
            await _safe_answer(callback, "Модель не найдена", show_alert=True)
            return
        text = (
            f"Модель\n\n{escape(model.title)}\n"
            f"Код: {escape(model.code)}\n"
            f"Категория: {escape(model.category)}\n"
            f"Цена: {model_price_text(model)}\n"
            f"Позиция: {model.position}\n"
            f"Описание: {escape(model.description or 'не задано')}\n"
            f"Статус: {'включена' if model.is_enabled else 'выключена'}"
        )
    await state.update_data(model_id=model_id)
    if callback.message:
        await callback.message.answer(text, reply_markup=_model_keyboard(model_id))
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("admin:model:toggle:"))
async def admin_model_toggle(callback: CallbackQuery, context: AppContext) -> None:
    if not await _require_admin_callback(callback, context):
        return
    model_id = int(callback.data.removeprefix("admin:model:toggle:"))
    async with session_scope(context.session_factory) as session:
        model = await session.get(GenerationModel, model_id)
        if not model:
            await _safe_answer(callback, "Модель не найдена", show_alert=True)
            return
        model.is_enabled = not model.is_enabled
        enabled = model.is_enabled
    await _safe_answer(callback, "Включена" if enabled else "Выключена", show_alert=True)


@router.callback_query(F.data.startswith("admin:model:price:"))
async def admin_model_price_prompt(
    callback: CallbackQuery, context: AppContext, state: FSMContext
) -> None:
    if not await _require_admin_callback(callback, context):
        return
    model_id = int(callback.data.removeprefix("admin:model:price:"))
    await state.update_data(model_id=model_id)
    await state.set_state(AdminStates.model_price)
    if callback.message:
        await callback.message.answer(
            "Изменить цену модели\n\nВведите новую цену в кредитах одним числом.",
            reply_markup=_cancel_keyboard(f"admin:model:{model_id}"),
        )
    await _safe_answer(callback)


@router.message(AdminStates.model_price, F.text)
async def admin_model_price_apply(message: Message, context: AppContext, state: FSMContext) -> None:
    if not await _require_admin_message(message, context):
        return
    data = await state.get_data()
    try:
        price = int(message.text)
    except ValueError:
        model_id = data.get("model_id")
        back_to = f"admin:model:{model_id}" if model_id else "admin:models"
        await message.answer("Введите целое число.", reply_markup=_cancel_keyboard(back_to))
        return
    async with session_scope(context.session_factory) as session:
        model = await session.get(GenerationModel, int(data["model_id"]))
        if not model:
            await message.answer(
                "Модель не найдена.", reply_markup=_cancel_keyboard("admin:models")
            )
            return
        model.price_credits = price
    await state.clear()
    await message.answer("Цена обновлена.", reply_markup=_admin_keyboard())


@router.callback_query(F.data.startswith("admin:model:edit:"))
async def admin_model_field_prompt(
    callback: CallbackQuery, context: AppContext, state: FSMContext
) -> None:
    if not await _require_admin_callback(callback, context):
        return
    raw = callback.data.removeprefix("admin:model:edit:")
    field, _, model_id_text = raw.partition(":")
    if field not in {"title", "description", "position"} or not model_id_text.isdigit():
        await _safe_answer(callback, "Поле недоступно", show_alert=True)
        return
    model_id = int(model_id_text)
    await state.set_state(AdminStates.model_field)
    await state.update_data(model_id=model_id, model_field=field)
    prompts = {
        "title": "Введите новое название модели.",
        "description": "Введите новое описание модели.",
        "position": "Введите новую позицию сортировки числом.",
    }
    if callback.message:
        await callback.message.answer(
            prompts[field],
            reply_markup=_cancel_keyboard(f"admin:model:{model_id}"),
        )
    await _safe_answer(callback)


@router.message(AdminStates.model_field, F.text)
async def admin_model_field_apply(message: Message, context: AppContext, state: FSMContext) -> None:
    if not await _require_admin_message(message, context):
        return
    data = await state.get_data()
    model_id = int(data["model_id"])
    field = str(data["model_field"])
    value = message.text.strip()
    async with session_scope(context.session_factory) as session:
        model = await session.get(GenerationModel, model_id)
        if not model:
            await message.answer(
                "Модель не найдена.", reply_markup=_cancel_keyboard("admin:models")
            )
            return
        if field == "position":
            try:
                model.position = int(value)
            except ValueError:
                await message.answer(
                    "Позиция должна быть целым числом.",
                    reply_markup=_cancel_keyboard(f"admin:model:{model_id}"),
                )
                return
        elif field == "title":
            if not value:
                await message.answer(
                    "Название не может быть пустым.",
                    reply_markup=_cancel_keyboard(f"admin:model:{model_id}"),
                )
                return
            model.title = value
        else:
            model.description = value or None
    await state.clear()
    await message.answer("Модель обновлена.", reply_markup=_admin_keyboard())


@router.callback_query(F.data.in_({"admin:packages", "adm:price"}))
async def admin_packages(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    if not await _require_admin_callback(callback, context):
        return
    async with session_scope(context.session_factory) as session:
        packages = list(
            await session.scalars(select(CreditPackage).order_by(CreditPackage.position))
        )
    text, keyboard = _admin_packages_view(packages)
    if callback.message:
        await callback.message.answer(text, reply_markup=keyboard)
    await _safe_answer(callback)


@router.callback_query(
    F.data.func(
        lambda data: bool(
            data
            and data.startswith("admin:package:")
            and data.removeprefix("admin:package:").isdigit()
        )
    )
)
async def admin_package_detail(
    callback: CallbackQuery, context: AppContext, state: FSMContext
) -> None:
    await state.clear()
    if not await _require_admin_callback(callback, context):
        return
    package_id = int(callback.data.removeprefix("admin:package:"))
    async with session_scope(context.session_factory) as session:
        package = await session.get(CreditPackage, package_id)
        if not package:
            await _safe_answer(callback, "Пакет не найден", show_alert=True)
            return
        payments_count = await session.scalar(
            select(func.count()).select_from(Payment).where(Payment.package_id == package.id)
        )
        text = _package_detail_text(package, int(payments_count or 0))
        keyboard = _package_keyboard(package_id, package.is_enabled)
    if callback.message:
        await callback.message.answer(text, reply_markup=keyboard)
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("admin:package:toggle:"))
async def admin_package_toggle(callback: CallbackQuery, context: AppContext) -> None:
    if not await _require_admin_callback(callback, context):
        return
    package_id = int(callback.data.removeprefix("admin:package:toggle:"))
    async with session_scope(context.session_factory) as session:
        package = await session.get(CreditPackage, package_id)
        if not package:
            await _safe_answer(callback, "Пакет не найден", show_alert=True)
            return
        package.is_enabled = not package.is_enabled
        enabled = package.is_enabled
        payments_count = await session.scalar(
            select(func.count()).select_from(Payment).where(Payment.package_id == package.id)
        )
        text = _package_detail_text(package, int(payments_count or 0))
        keyboard = _package_keyboard(package_id, package.is_enabled)
    if callback.message:
        await callback.message.answer(text, reply_markup=keyboard)
    await _safe_answer(callback, "Включен" if enabled else "Выключен", show_alert=True)


@router.callback_query(F.data.startswith("admin:package:edit:"))
async def admin_package_field_prompt(
    callback: CallbackQuery, context: AppContext, state: FSMContext
) -> None:
    if not await _require_admin_callback(callback, context):
        return
    raw = callback.data.removeprefix("admin:package:edit:")
    field, _, package_id_text = raw.partition(":")
    allowed = {
        "title",
        "description",
        "terms",
        "credits",
        "photo_credits",
        "video_credits",
        "price",
        "duration",
        "unlimited",
        "position",
    }
    if field not in allowed or not package_id_text.isdigit():
        await _safe_answer(callback, "Поле недоступно", show_alert=True)
        return
    package_id = int(package_id_text)
    await state.set_state(AdminStates.package_field)
    await state.update_data(package_id=package_id, package_field=field)
    prompts = {
        "title": "Введите новое название пакета.",
        "description": "Введите новое описание пакета.",
        "terms": "Введите условия пакета. Чтобы очистить условия, отправьте -.",
        "credits": "Введите количество универсальных кредитов числом.",
        "photo_credits": "Введите лимит фото-кредитов числом.",
        "video_credits": "Введите лимит видео-кредитов числом.",
        "price": "Введите цену в рублях, например 1490 или 1490.50.",
        "duration": "Введите срок безлимита в днях. 0 очистит срок.",
        "unlimited": "Это безлимитный пакет? Ответьте да или нет.",
        "position": "Введите позицию сортировки числом.",
    }
    if callback.message:
        await callback.message.answer(
            prompts[field],
            reply_markup=_cancel_keyboard(f"admin:package:{package_id}"),
        )
    await _safe_answer(callback)


@router.message(AdminStates.package_field, F.text)
async def admin_package_field_apply(
    message: Message, context: AppContext, state: FSMContext
) -> None:
    if not await _require_admin_message(message, context):
        return
    data = await state.get_data()
    package_id = int(data["package_id"])
    field = str(data["package_field"])
    value = message.text.strip()
    async with session_scope(context.session_factory) as session:
        package = await session.get(CreditPackage, package_id)
        if not package:
            await message.answer(
                "Пакет не найден.", reply_markup=_cancel_keyboard("admin:packages")
            )
            return
        try:
            if field == "title":
                if not value:
                    raise ValueError("Название не может быть пустым.")
                package.title = value
            elif field == "description":
                package.description = value or None
            elif field == "terms":
                package.terms = None if value == "-" else (value or None)
            elif field == "credits":
                credits = int(value)
                if credits < 0:
                    raise ValueError("Кредиты не могут быть отрицательными.")
                package.credits = credits
            elif field == "photo_credits":
                credits = int(value)
                if credits < 0:
                    raise ValueError("Фото-кредиты не могут быть отрицательными.")
                package.photo_credits = credits
            elif field == "video_credits":
                credits = int(value)
                if credits < 0:
                    raise ValueError("Видео-кредиты не могут быть отрицательными.")
                package.video_credits = credits
            elif field == "price":
                price = Decimal(value.replace(",", "."))
                if price < 0:
                    raise ValueError("Цена не может быть отрицательной.")
                package.price_rub = price
            elif field == "duration":
                duration = int(value)
                if duration < 0:
                    raise ValueError("Срок не может быть отрицательным.")
                package.duration_days = duration or None
            elif field == "unlimited":
                package.is_unlimited = _parse_bool(value)
            elif field == "position":
                package.position = int(value)
        except (ValueError, ArithmeticError) as exc:
            await message.answer(
                str(exc), reply_markup=_cancel_keyboard(f"admin:package:{package_id}")
            )
            return
    await state.clear()
    await message.answer("Пакет обновлен.", reply_markup=_admin_keyboard())


@router.callback_query(F.data.startswith("admin:package:delete:"))
async def admin_package_delete(callback: CallbackQuery, context: AppContext) -> None:
    if not await _require_admin_callback(callback, context):
        return
    package_id = int(callback.data.removeprefix("admin:package:delete:"))
    async with session_scope(context.session_factory) as session:
        package = await session.get(CreditPackage, package_id)
        if not package:
            await _safe_answer(callback, "Пакет не найден", show_alert=True)
            return
        payments_count = await session.scalar(
            select(func.count()).select_from(Payment).where(Payment.package_id == package.id)
        )
        if payments_count:
            package.is_enabled = False
            await _safe_answer(callback, "У пакета есть платежи, он выключен", show_alert=True)
            return
        await session.delete(package)
    await _safe_answer(callback, "Пакет удален", show_alert=True)


@router.callback_query(F.data == "admin:package:add")
async def admin_package_add_prompt(
    callback: CallbackQuery, context: AppContext, state: FSMContext
) -> None:
    if not await _require_admin_callback(callback, context):
        return
    await state.set_state(AdminStates.package_add)
    await state.update_data(package_step="code")
    if callback.message:
        await callback.message.answer(
            "Новый пакет\n\n"
            "Шаг 1 из 9. Введите короткий код пакета латиницей.\n"
            "Пример: creator_100",
            reply_markup=_cancel_keyboard("admin:packages"),
        )
    await _safe_answer(callback)


@router.message(AdminStates.package_add, F.text)
async def admin_package_add_apply(message: Message, context: AppContext, state: FSMContext) -> None:
    if not await _require_admin_message(message, context):
        return
    data = await state.get_data()
    step = data.get("package_step", "code")
    value = message.text.strip()
    if step == "code":
        if not value or not value.replace("_", "").replace("-", "").isalnum():
            await message.answer(
                "Код должен содержать латиницу/цифры, дефис или подчеркивание.",
                reply_markup=_cancel_keyboard("admin:packages"),
            )
            return
        async with session_scope(context.session_factory) as session:
            existing = await session.scalar(
                select(CreditPackage).where(CreditPackage.code == value)
            )
        if existing:
            await message.answer(
                "Пакет с таким кодом уже есть.",
                reply_markup=_cancel_keyboard("admin:packages"),
            )
            return
        await state.update_data(package_code=value, package_step="title")
        await message.answer(
            "Шаг 2 из 9. Введите название пакета.\nПример: Пакет автора",
            reply_markup=_cancel_keyboard("admin:packages"),
        )
        return
    if step == "title":
        if not value:
            await message.answer(
                "Название не может быть пустым.", reply_markup=_cancel_keyboard("admin:packages")
            )
            return
        await state.update_data(package_title=value, package_step="photo_credits")
        await message.answer(
            "Шаг 3 из 9. Сколько фото-кредитов входит в пакет? Можно 0.",
            reply_markup=_cancel_keyboard("admin:packages"),
        )
        return
    if step == "photo_credits":
        if not value.isdigit():
            await message.answer(
                "Введите целое число, например 50, или 0.",
                reply_markup=_cancel_keyboard("admin:packages"),
            )
            return
        await state.update_data(package_photo_credits=int(value), package_step="video_credits")
        await message.answer(
            "Шаг 4 из 9. Сколько видео-кредитов входит в пакет? Можно 0.",
            reply_markup=_cancel_keyboard("admin:packages"),
        )
        return
    if step == "video_credits":
        if not value.isdigit():
            await message.answer(
                "Введите целое число, например 10, или 0.",
                reply_markup=_cancel_keyboard("admin:packages"),
            )
            return
        await state.update_data(package_video_credits=int(value), package_step="credits")
        await message.answer(
            "Шаг 5 из 9. Сколько универсальных кредитов добавить? Можно 0.\n"
            "Универсальные кредиты подходят и для фото, и для видео.",
            reply_markup=_cancel_keyboard("admin:packages"),
        )
        return
    if step == "credits":
        if not value.isdigit():
            await message.answer(
                "Введите целое число, например 100, или 0.",
                reply_markup=_cancel_keyboard("admin:packages"),
            )
            return
        await state.update_data(package_credits=int(value), package_step="price")
        await message.answer(
            "Шаг 6 из 9. Введите цену в рублях.\nПример: 1490",
            reply_markup=_cancel_keyboard("admin:packages"),
        )
        return
    if step == "price":
        try:
            price = Decimal(value.replace(",", "."))
            if price <= 0:
                raise ValueError
        except (ValueError, ArithmeticError):
            await message.answer(
                "Введите положительную цену числом, например 1490 или 1490.50.",
                reply_markup=_cancel_keyboard("admin:packages"),
            )
            return
        await state.update_data(package_price=str(price), package_step="duration")
        await message.answer(
            "Шаг 7 из 9. Срок безлимита в днях. Для обычного пакета введите 0.",
            reply_markup=_cancel_keyboard("admin:packages"),
        )
        return
    if step == "duration":
        if not value.isdigit():
            await message.answer(
                "Введите целое число дней, например 30, или 0.",
                reply_markup=_cancel_keyboard("admin:packages"),
            )
            return
        duration = int(value)
        await state.update_data(package_duration=duration, package_step="unlimited")
        await message.answer(
            "Шаг 8 из 9. Это безлимитный пакет? Ответьте: да или нет.",
            reply_markup=_cancel_keyboard("admin:packages"),
        )
        return
    if step == "unlimited":
        try:
            is_unlimited = _parse_bool(value)
        except ValueError as exc:
            await message.answer(str(exc), reply_markup=_cancel_keyboard("admin:packages"))
            return
        data = await state.get_data()
        credits = int(data["package_credits"])
        photo_credits = int(data["package_photo_credits"])
        video_credits = int(data["package_video_credits"])
        duration_days = int(data["package_duration"]) or None
        if is_unlimited and not duration_days:
            await message.answer(
                "Для безлимитного пакета срок должен быть больше 0.",
                reply_markup=_cancel_keyboard("admin:packages"),
            )
            return
        if not is_unlimited and credits <= 0 and photo_credits <= 0 and video_credits <= 0:
            await message.answer(
                "Для обычного пакета нужен хотя бы один тип кредитов больше 0.",
                reply_markup=_cancel_keyboard("admin:packages"),
            )
            return
        await state.update_data(package_is_unlimited=is_unlimited, package_step="terms")
        await message.answer(
            "Шаг 9 из 9. Введите условия пакета, которые увидит пользователь.\n"
            "Например: кредиты зачисляются сразу после оплаты. Чтобы пропустить, отправьте -.",
            reply_markup=_cancel_keyboard("admin:packages"),
        )
        return
    if step != "terms":
        await message.answer(
            "Неожиданный шаг создания пакета. Начните добавление заново.",
            reply_markup=_cancel_keyboard("admin:packages"),
        )
        return
    terms = None if value == "-" else (value or None)
    data = await state.get_data()
    credits = int(data["package_credits"])
    photo_credits = int(data["package_photo_credits"])
    video_credits = int(data["package_video_credits"])
    duration_days = int(data["package_duration"]) or None
    is_unlimited = bool(data["package_is_unlimited"])
    async with session_scope(context.session_factory) as session:
        session.add(
            CreditPackage(
                code=data["package_code"],
                title=data["package_title"],
                credits=credits,
                photo_credits=photo_credits,
                video_credits=video_credits,
                price_rub=Decimal(str(data["package_price"])),
                duration_days=duration_days,
                is_unlimited=is_unlimited,
                terms=terms,
                is_enabled=True,
            )
        )
    await state.clear()
    await message.answer("Пакет добавлен.", reply_markup=_admin_keyboard())


@router.callback_query(F.data.in_({"admin:payments", "adm:payments"}))
async def admin_payments(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    if not await _require_admin_callback(callback, context):
        return
    async with session_scope(context.session_factory) as session:
        payments = list(
            await session.scalars(select(Payment).order_by(Payment.created_at.desc()).limit(10))
        )
    builder = InlineKeyboardBuilder()
    lines = ["Платежи\n\nПоследние 10:"]
    for payment in payments:
        lines.append(
            f"{payment.order_id} · user {payment.user_id} · {payment.amount_kopecks / 100:.0f} ₽ · {payment.status}"
        )
        builder.button(
            text=f"{payment.status} · {payment.order_id[-12:]}",
            callback_data=f"admin:payment:{payment.id}",
        )
    builder.button(text="Отметить по order_id", callback_data="admin:payments:mark_paid")
    nav_count = add_navigation_buttons(builder, back_callback="admin:menu")
    builder.adjust(*([1] * len(payments)), 1, nav_count)
    if callback.message:
        await callback.message.answer("\n".join(lines), reply_markup=builder.as_markup())
    await _safe_answer(callback)


@router.callback_query(
    F.data.func(
        lambda data: bool(
            data
            and data.startswith("admin:payment:")
            and data.removeprefix("admin:payment:").isdigit()
        )
    )
)
async def admin_payment_detail(
    callback: CallbackQuery, context: AppContext, state: FSMContext
) -> None:
    await state.clear()
    if not await _require_admin_callback(callback, context):
        return
    payment_id = int(callback.data.removeprefix("admin:payment:"))
    async with session_scope(context.session_factory) as session:
        payment = await session.get(Payment, payment_id)
        if not payment:
            await _safe_answer(callback, "Платеж не найден", show_alert=True)
            return
        package = (
            await session.get(CreditPackage, payment.package_id) if payment.package_id else None
        )
        user = await session.get(User, payment.user_id)
        text = _payment_detail_text(payment, package, user)
    if callback.message:
        await callback.message.answer(
            text, reply_markup=_payment_keyboard(payment_id, payment.status)
        )
    await _safe_answer(callback)


@router.callback_query(F.data == "admin:payments:mark_paid")
async def admin_payment_mark_prompt(
    callback: CallbackQuery, context: AppContext, state: FSMContext
) -> None:
    if not await _require_admin_callback(callback, context):
        return
    await state.set_state(AdminStates.payment_mark_paid)
    if callback.message:
        await callback.message.answer(
            "Отметить платеж оплаченным\n\nОтправьте номер заказа `order_id` из списка платежей.",
            reply_markup=_cancel_keyboard("admin:payments"),
        )
    await _safe_answer(callback)


@router.message(AdminStates.payment_mark_paid, F.text)
async def admin_payment_mark_apply(
    message: Message, context: AppContext, state: FSMContext
) -> None:
    if not await _require_admin_message(message, context):
        return
    order_id = message.text.strip()
    async with session_scope(context.session_factory) as session:
        payment = await session.scalar(
            select(Payment).where(Payment.order_id == order_id).with_for_update()
        )
        if not payment:
            await message.answer(
                "Платеж не найден.", reply_markup=_cancel_keyboard("admin:payments")
            )
            return
        result = await _mark_payment_paid(session, payment)
        if not result.ok:
            await message.answer(result.admin_text, reply_markup=_admin_keyboard())
            await state.clear()
            return
    await state.clear()
    if context.bot and result.notify_chat_id and result.notify_text:
        with suppress(Exception):
            await context.bot.send_message(
                result.notify_chat_id,
                result.notify_text,
                reply_markup=navigation_keyboard(),
            )
    await message.answer(result.admin_text, reply_markup=_admin_keyboard())


@router.callback_query(F.data.startswith("admin:payment:paid:"))
async def admin_payment_paid_callback(callback: CallbackQuery, context: AppContext) -> None:
    if not await _require_admin_callback(callback, context):
        return
    payment_id = int(callback.data.removeprefix("admin:payment:paid:"))
    async with session_scope(context.session_factory) as session:
        payment = await session.get(Payment, payment_id, with_for_update=True)
        if not payment:
            await _safe_answer(callback, "Платеж не найден", show_alert=True)
            return
        result = await _mark_payment_paid(session, payment)
    if context.bot and result.notify_chat_id and result.notify_text:
        with suppress(Exception):
            await context.bot.send_message(
                result.notify_chat_id,
                result.notify_text,
                reply_markup=navigation_keyboard(),
            )
    await _safe_answer(callback, result.admin_text, show_alert=True)


@router.callback_query(F.data.startswith("admin:payment:cancel:"))
async def admin_payment_cancel_callback(callback: CallbackQuery, context: AppContext) -> None:
    if not await _require_admin_callback(callback, context):
        return
    payment_id = int(callback.data.removeprefix("admin:payment:cancel:"))
    async with session_scope(context.session_factory) as session:
        payment = await session.get(Payment, payment_id)
        if not payment:
            await _safe_answer(callback, "Платеж не найден", show_alert=True)
            return
        if payment.status == "paid":
            await _safe_answer(callback, "Оплаченный платеж нельзя отменить", show_alert=True)
            return
        payment.status = "cancelled"
    await _safe_answer(callback, "Платеж отменен", show_alert=True)


@router.callback_query(F.data.in_({"admin:gallery", "adm:gallery"}))
async def admin_gallery(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    if not await _require_admin_callback(callback, context):
        return
    async with session_scope(context.session_factory) as session:
        items = list(
            await session.scalars(
                select(GalleryItem).order_by(GalleryItem.created_at.desc()).limit(10)
            )
        )
    builder = InlineKeyboardBuilder()
    lines = ["Галерея\n\nПубличные элементы видны пользователям в разделе «Галерея»."]
    for item in items:
        flag = "public" if item.is_public else "hidden"
        featured = " · featured" if item.is_featured else ""
        lines.append(
            f"{item.id}. {escape(item.title or item.media_type)} · {flag} · "
            f"{escape(item.media_url[:60])}"
        )
        builder.button(
            text=f"{item.id}. {item.title or item.media_type}{featured}",
            callback_data=f"admin:gallery:item:{item.id}",
        )
    builder.button(text="Добавить работу", callback_data="admin:gallery:add")
    builder.button(text="Скрыть / показать по ID", callback_data="admin:gallery:toggle")
    nav_count = add_navigation_buttons(builder, back_callback="admin:menu")
    builder.adjust(*([1] * len(items)), 1, 1, nav_count)
    if callback.message:
        await callback.message.answer("\n".join(lines), reply_markup=builder.as_markup())
    await _safe_answer(callback)


@router.callback_query(F.data.startswith("admin:gallery:item:"))
async def admin_gallery_detail(
    callback: CallbackQuery, context: AppContext, state: FSMContext
) -> None:
    await state.clear()
    if not await _require_admin_callback(callback, context):
        return
    item_id = int(callback.data.removeprefix("admin:gallery:item:"))
    async with session_scope(context.session_factory) as session:
        item = await session.get(GalleryItem, item_id)
        if not item:
            await _safe_answer(callback, "Элемент не найден", show_alert=True)
            return
        text = _gallery_detail_text(item)
    if callback.message:
        await callback.message.answer(text, reply_markup=_gallery_item_keyboard(item_id))
    await _safe_answer(callback)


@router.callback_query(F.data == "admin:gallery:add")
async def admin_gallery_add_prompt(
    callback: CallbackQuery, context: AppContext, state: FSMContext
) -> None:
    if not await _require_admin_callback(callback, context):
        return
    await state.set_state(AdminStates.gallery_add)
    await state.update_data(gallery_step="url")
    if callback.message:
        await callback.message.answer(
            "Добавить работу в галерею\n\nШаг 1 из 4. Отправьте ссылку на изображение или видео.",
            reply_markup=_cancel_keyboard("admin:gallery"),
        )
    await _safe_answer(callback)


@router.message(AdminStates.gallery_add, F.text)
async def admin_gallery_add_apply(message: Message, context: AppContext, state: FSMContext) -> None:
    if not await _require_admin_message(message, context):
        return
    data = await state.get_data()
    step = data.get("gallery_step", "url")
    value = message.text.strip()
    if step == "url":
        await state.update_data(gallery_url=value)
        await message.answer(
            "Шаг 2 из 4. Выберите тип медиа:", reply_markup=_gallery_type_keyboard()
        )
        return
    if step == "title":
        await state.update_data(gallery_title=value, gallery_step="prompt")
        await message.answer(
            "Шаг 4 из 4. Отправьте промпт, который нужно показать в галерее.",
            reply_markup=_cancel_keyboard("admin:gallery"),
        )
        return
    if step == "prompt":
        async with session_scope(context.session_factory) as session:
            session.add(
                GalleryItem(
                    media_url=data["gallery_url"],
                    media_type=data["gallery_type"],
                    title=data["gallery_title"],
                    prompt=value,
                    is_public=True,
                )
            )
        await state.clear()
        await message.answer("Элемент добавлен в галерею.", reply_markup=_admin_keyboard())
        return
    await message.answer(
        "Пожалуйста, используйте кнопки или начните добавление заново.",
        reply_markup=_cancel_keyboard("admin:gallery"),
    )


@router.callback_query(AdminStates.gallery_add, F.data.startswith("admin:gallery:type:"))
async def admin_gallery_type_apply(
    callback: CallbackQuery, context: AppContext, state: FSMContext
) -> None:
    if not await _require_admin_callback(callback, context):
        return
    media_type = callback.data.removeprefix("admin:gallery:type:")
    await state.update_data(gallery_type=media_type, gallery_step="title")
    if callback.message:
        await callback.message.answer(
            "Шаг 3 из 4. Введите название работы.",
            reply_markup=_cancel_keyboard("admin:gallery"),
        )
    await _safe_answer(callback)


@router.callback_query(F.data == "admin:gallery:toggle")
async def admin_gallery_toggle_prompt(
    callback: CallbackQuery, context: AppContext, state: FSMContext
) -> None:
    if not await _require_admin_callback(callback, context):
        return
    await state.set_state(AdminStates.gallery_toggle)
    if callback.message:
        await callback.message.answer(
            "Скрыть или показать элемент галереи\n\nОтправьте ID элемента из списка выше.",
            reply_markup=_cancel_keyboard("admin:gallery"),
        )
    await _safe_answer(callback)


@router.message(AdminStates.gallery_toggle, F.text)
async def admin_gallery_toggle_apply(
    message: Message, context: AppContext, state: FSMContext
) -> None:
    if not await _require_admin_message(message, context):
        return
    try:
        item_id = int(message.text.strip())
    except ValueError:
        await message.answer(
            "Введите ID элемента числом.", reply_markup=_cancel_keyboard("admin:gallery")
        )
        return
    async with session_scope(context.session_factory) as session:
        item = await session.get(GalleryItem, item_id)
        if not item:
            await message.answer(
                "Элемент не найден.", reply_markup=_cancel_keyboard("admin:gallery")
            )
            return
        item.is_public = not item.is_public
    await state.clear()
    await message.answer("Галерея обновлена.", reply_markup=_admin_keyboard())


@router.callback_query(F.data.startswith("admin:gallery:toggle_public:"))
async def admin_gallery_toggle_public(callback: CallbackQuery, context: AppContext) -> None:
    if not await _require_admin_callback(callback, context):
        return
    item_id = int(callback.data.removeprefix("admin:gallery:toggle_public:"))
    async with session_scope(context.session_factory) as session:
        item = await session.get(GalleryItem, item_id)
        if not item:
            await _safe_answer(callback, "Элемент не найден", show_alert=True)
            return
        item.is_public = not item.is_public
        status = "public" if item.is_public else "hidden"
    await _safe_answer(callback, status, show_alert=True)


@router.callback_query(F.data.startswith("admin:gallery:toggle_featured:"))
async def admin_gallery_toggle_featured(callback: CallbackQuery, context: AppContext) -> None:
    if not await _require_admin_callback(callback, context):
        return
    item_id = int(callback.data.removeprefix("admin:gallery:toggle_featured:"))
    async with session_scope(context.session_factory) as session:
        item = await session.get(GalleryItem, item_id)
        if not item:
            await _safe_answer(callback, "Элемент не найден", show_alert=True)
            return
        item.is_featured = not item.is_featured
        status = "featured" if item.is_featured else "обычный"
    await _safe_answer(callback, status, show_alert=True)


@router.callback_query(F.data.startswith("admin:gallery:delete:"))
async def admin_gallery_delete(callback: CallbackQuery, context: AppContext) -> None:
    if not await _require_admin_callback(callback, context):
        return
    item_id = int(callback.data.removeprefix("admin:gallery:delete:"))
    async with session_scope(context.session_factory) as session:
        item = await session.get(GalleryItem, item_id)
        if not item:
            await _safe_answer(callback, "Элемент не найден", show_alert=True)
            return
        await session.delete(item)
    await _safe_answer(callback, "Элемент удален", show_alert=True)


@router.callback_query(F.data.startswith("admin:gallery:edit:"))
async def admin_gallery_field_prompt(
    callback: CallbackQuery, context: AppContext, state: FSMContext
) -> None:
    if not await _require_admin_callback(callback, context):
        return
    raw = callback.data.removeprefix("admin:gallery:edit:")
    field, _, item_id_text = raw.partition(":")
    if field not in {"url", "type", "title", "prompt"} or not item_id_text.isdigit():
        await _safe_answer(callback, "Поле недоступно", show_alert=True)
        return
    item_id = int(item_id_text)
    await state.set_state(AdminStates.gallery_field)
    await state.update_data(gallery_item_id=item_id, gallery_field=field)
    prompts = {
        "url": "Отправьте новый URL или file_id медиа.",
        "type": "Введите тип: image или video.",
        "title": "Введите новое название работы.",
        "prompt": "Введите новый промпт.",
    }
    if callback.message:
        await callback.message.answer(
            prompts[field],
            reply_markup=_cancel_keyboard(f"admin:gallery:item:{item_id}"),
        )
    await _safe_answer(callback)


@router.message(AdminStates.gallery_field, F.text)
async def admin_gallery_field_apply(
    message: Message, context: AppContext, state: FSMContext
) -> None:
    if not await _require_admin_message(message, context):
        return
    data = await state.get_data()
    item_id = int(data["gallery_item_id"])
    field = str(data["gallery_field"])
    value = message.text.strip()
    async with session_scope(context.session_factory) as session:
        item = await session.get(GalleryItem, item_id)
        if not item:
            await message.answer(
                "Элемент не найден.", reply_markup=_cancel_keyboard("admin:gallery")
            )
            return
        if field == "url":
            if not value:
                await message.answer(
                    "URL не может быть пустым.",
                    reply_markup=_cancel_keyboard(f"admin:gallery:item:{item_id}"),
                )
                return
            item.media_url = value
        elif field == "type":
            if value not in {"image", "video"}:
                await message.answer(
                    "Тип должен быть image или video.",
                    reply_markup=_cancel_keyboard(f"admin:gallery:item:{item_id}"),
                )
                return
            item.media_type = value
        elif field == "title":
            item.title = value or None
        elif field == "prompt":
            item.prompt = value or None
    await state.clear()
    await message.answer("Элемент галереи обновлен.", reply_markup=_admin_keyboard())




@router.callback_query(F.data.in_({"admin:referrals", "adm:referrals"}))
async def admin_referrals(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    if not await _require_admin_callback(callback, context):
        return
    async with session_scope(context.session_factory) as session:
        users = list(await session.scalars(select(User).order_by(User.created_at.desc()).limit(5000)))
    child_counts: dict[int, int] = {}
    for user in users:
        if user.referred_by_user_id:
            child_counts[user.referred_by_user_id] = child_counts.get(user.referred_by_user_id, 0) + 1
    leaders = sorted(
        [user for user in users if child_counts.get(user.id, 0) > 0],
        key=lambda item: (child_counts.get(item.id, 0), item.affiliate_earned_kopecks or 0),
        reverse=True,
    )[:15]
    total_referred = sum(child_counts.values())
    total_earned = sum(int(user.affiliate_earned_kopecks or 0) for user in users)
    total_balance = sum(int(user.affiliate_balance_kopecks or 0) for user in users)

    lines = [
        "👥 <b>Рефералы</b>",
        "",
        f"Приглашённых всего: <b>{total_referred}</b>",
        f"Начислено партнёрам: <b>{total_earned / 100:.0f} ₽</b>",
        f"На партнёрских балансах: <b>{total_balance / 100:.0f} ₽</b>",
        "",
    ]
    if not leaders:
        lines.append("Пока нет пользователей с рефералами.")
    else:
        lines.append("Топ партнёров:")
        for index, user in enumerate(leaders, 1):
            username = f"@{escape(user.username)}" if user.username else "без username"
            lines.append(
                f"{index}. {username} · <code>{user.telegram_id}</code>\n"
                f"   приглашено: <b>{child_counts.get(user.id, 0)}</b> · "
                f"заработано: <b>{(user.affiliate_earned_kopecks or 0) / 100:.0f} ₽</b> · "
                f"баланс: <b>{(user.affiliate_balance_kopecks or 0) / 100:.0f} ₽</b>"
            )

    builder = InlineKeyboardBuilder()
    builder.button(text="💸 Заявки на вывод", callback_data="admin:withdrawals")
    builder.button(text="🤝 Партнерские ссылки", callback_data="admin:partners")
    nav_count = add_navigation_buttons(builder, back_callback="admin:menu")
    builder.adjust(1, 1, nav_count)
    if callback.message:
        await callback.message.answer("\n".join(lines), reply_markup=builder.as_markup())
    await _safe_answer(callback)


@router.callback_query(F.data.in_({"admin:withdrawals", "adm:withdrawals"}))
async def admin_withdrawals(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    if not await _require_admin_callback(callback, context):
        return
    async with session_scope(context.session_factory) as session:
        rows = list(
            await session.execute(
                select(AffiliateWithdrawal, User)
                .join(User, User.id == AffiliateWithdrawal.user_id)
                .order_by(AffiliateWithdrawal.created_at.desc())
                .limit(15)
            )
        )
    lines = ["💸 <b>Заявки на вывод</b>", ""]
    builder = InlineKeyboardBuilder()
    if not rows:
        lines.append("Заявок на вывод пока нет.")
    else:
        for withdrawal, user in rows:
            status = str(withdrawal.status or "pending")
            marker = "⏳" if status == "pending" else "✅" if status == "paid" else "❌"
            username = f"@{escape(user.username)}" if user.username else str(user.telegram_id)
            lines.append(
                f"{marker} <b>#{withdrawal.id}</b> · {withdrawal.amount_kopecks / 100:.0f} ₽ · {status}\n"
                f"   {username} · <code>{user.telegram_id}</code>\n"
                f"   <code>{escape((withdrawal.details or '')[:120])}</code>"
            )
            if status == "pending":
                builder.button(text=f"✅ Выплачено #{withdrawal.id}", callback_data=f"partner:withdrawal:paid:{withdrawal.id}")
                builder.button(text=f"❌ Отклонить #{withdrawal.id}", callback_data=f"partner:withdrawal:reject:{withdrawal.id}")
    nav_count = add_navigation_buttons(builder, back_callback="admin:menu")
    if rows:
        builder.adjust(*([2] * sum(1 for withdrawal, _ in rows if withdrawal.status == "pending")), nav_count)
    else:
        builder.adjust(nav_count)
    if callback.message:
        await callback.message.answer("\n".join(lines), reply_markup=builder.as_markup())
    await _safe_answer(callback)

@router.callback_query(F.data == "admin:partners")
async def admin_partners(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    if not await _require_admin_callback(callback, context):
        return
    async with session_scope(context.session_factory) as session:
        links = list(await session.scalars(select(PartnerLink).order_by(PartnerLink.position)))
    builder = InlineKeyboardBuilder()
    lines = ["Партнеры\n\nСсылки видны пользователям в разделе «Партнеры»."]
    for link in links:
        flag = "включен" if link.is_enabled else "выключен"
        lines.append(
            f"{link.id}. {escape(link.title)} · {flag} · кликов {link.clicks}\n{escape(link.url)}"
        )
        builder.button(text=f"{link.id}. {link.title}", callback_data=f"admin:partner:{link.id}")
    builder.button(text="Добавить партнера", callback_data="admin:partners:add")
    builder.button(text="Включить / выключить по ID", callback_data="admin:partners:toggle")
    nav_count = add_navigation_buttons(builder, back_callback="admin:menu")
    builder.adjust(*([1] * len(links)), 1, 1, nav_count)
    if callback.message:
        await callback.message.answer("\n".join(lines), reply_markup=builder.as_markup())
    await _safe_answer(callback)


@router.callback_query(
    F.data.func(
        lambda data: bool(
            data
            and data.startswith("admin:partner:")
            and data.removeprefix("admin:partner:").isdigit()
        )
    )
)
async def admin_partner_detail(
    callback: CallbackQuery, context: AppContext, state: FSMContext
) -> None:
    await state.clear()
    if not await _require_admin_callback(callback, context):
        return
    link_id = int(callback.data.removeprefix("admin:partner:"))
    async with session_scope(context.session_factory) as session:
        link = await session.get(PartnerLink, link_id)
        if not link:
            await _safe_answer(callback, "Партнер не найден", show_alert=True)
            return
        text = _partner_detail_text(link)
    if callback.message:
        await callback.message.answer(text, reply_markup=_partner_item_keyboard(link_id))
    await _safe_answer(callback)


@router.callback_query(F.data == "admin:partners:add")
async def admin_partner_add_prompt(
    callback: CallbackQuery, context: AppContext, state: FSMContext
) -> None:
    if not await _require_admin_callback(callback, context):
        return
    await state.set_state(AdminStates.partner_add)
    await state.update_data(partner_step="code")
    if callback.message:
        await callback.message.answer(
            "Добавить партнера\n\n"
            "Шаг 1 из 4. Введите короткий код партнера латиницей.\n"
            "Пример: tg_channel",
            reply_markup=_cancel_keyboard("admin:partners"),
        )
    await _safe_answer(callback)


@router.message(AdminStates.partner_add, F.text)
async def admin_partner_add_apply(message: Message, context: AppContext, state: FSMContext) -> None:
    if not await _require_admin_message(message, context):
        return
    data = await state.get_data()
    step = data.get("partner_step", "code")
    value = message.text.strip()
    if step == "code":
        if not value or not value.replace("_", "").replace("-", "").isalnum():
            await message.answer(
                "Код должен содержать латиницу/цифры, дефис или подчеркивание.",
                reply_markup=_cancel_keyboard("admin:partners"),
            )
            return
        async with session_scope(context.session_factory) as session:
            existing = await session.scalar(select(PartnerLink).where(PartnerLink.code == value))
        if existing:
            await message.answer(
                "Партнер с таким кодом уже есть.",
                reply_markup=_cancel_keyboard("admin:partners"),
            )
            return
        await state.update_data(partner_code=value, partner_step="title")
        await message.answer(
            "Шаг 2 из 4. Введите название партнера.",
            reply_markup=_cancel_keyboard("admin:partners"),
        )
        return
    if step == "title":
        if not value:
            await message.answer(
                "Название не может быть пустым.", reply_markup=_cancel_keyboard("admin:partners")
            )
            return
        await state.update_data(partner_title=value, partner_step="url")
        await message.answer(
            "Шаг 3 из 4. Отправьте ссылку партнера.",
            reply_markup=_cancel_keyboard("admin:partners"),
        )
        return
    if step == "url":
        if not _looks_like_url(value):
            await message.answer(
                "Ссылка должна начинаться с http:// или https://.",
                reply_markup=_cancel_keyboard("admin:partners"),
            )
            return
        await state.update_data(partner_url=value, partner_step="description")
        await message.answer(
            "Шаг 4 из 4. Добавьте короткое описание.",
            reply_markup=_cancel_keyboard("admin:partners"),
        )
        return
    async with session_scope(context.session_factory) as session:
        session.add(
            PartnerLink(
                code=data["partner_code"],
                title=data["partner_title"],
                url=data["partner_url"],
                description=value,
                is_enabled=True,
            )
        )
    await state.clear()
    await message.answer("Партнер добавлен.", reply_markup=_admin_keyboard())


@router.callback_query(F.data == "admin:partners:toggle")
async def admin_partner_toggle_prompt(
    callback: CallbackQuery, context: AppContext, state: FSMContext
) -> None:
    if not await _require_admin_callback(callback, context):
        return
    await state.set_state(AdminStates.partner_toggle)
    if callback.message:
        await callback.message.answer(
            "Включить или выключить партнера\n\nОтправьте ID партнера из списка выше.",
            reply_markup=_cancel_keyboard("admin:partners"),
        )
    await _safe_answer(callback)


@router.message(AdminStates.partner_toggle, F.text)
async def admin_partner_toggle_apply(
    message: Message, context: AppContext, state: FSMContext
) -> None:
    if not await _require_admin_message(message, context):
        return
    try:
        link_id = int(message.text.strip())
    except ValueError:
        await message.answer(
            "Введите ID партнера числом.", reply_markup=_cancel_keyboard("admin:partners")
        )
        return
    async with session_scope(context.session_factory) as session:
        link = await session.get(PartnerLink, link_id)
        if not link:
            await message.answer(
                "Партнер не найден.", reply_markup=_cancel_keyboard("admin:partners")
            )
            return
        link.is_enabled = not link.is_enabled
    await state.clear()
    await message.answer("Партнер обновлен.", reply_markup=_admin_keyboard())


@router.callback_query(F.data.startswith("admin:partner:toggle:"))
async def admin_partner_toggle_callback(callback: CallbackQuery, context: AppContext) -> None:
    if not await _require_admin_callback(callback, context):
        return
    link_id = int(callback.data.removeprefix("admin:partner:toggle:"))
    async with session_scope(context.session_factory) as session:
        link = await session.get(PartnerLink, link_id)
        if not link:
            await _safe_answer(callback, "Партнер не найден", show_alert=True)
            return
        link.is_enabled = not link.is_enabled
        status = "включен" if link.is_enabled else "выключен"
    await _safe_answer(callback, status, show_alert=True)


@router.callback_query(F.data.startswith("admin:partner:edit:"))
async def admin_partner_field_prompt(
    callback: CallbackQuery, context: AppContext, state: FSMContext
) -> None:
    if not await _require_admin_callback(callback, context):
        return
    raw = callback.data.removeprefix("admin:partner:edit:")
    field, _, link_id_text = raw.partition(":")
    if field not in {"title", "url", "description", "position"} or not link_id_text.isdigit():
        await _safe_answer(callback, "Поле недоступно", show_alert=True)
        return
    link_id = int(link_id_text)
    await state.set_state(AdminStates.partner_field)
    await state.update_data(partner_id=link_id, partner_field=field)
    prompts = {
        "title": "Введите новое название партнера.",
        "url": "Отправьте новую ссылку партнера.",
        "description": "Введите новое описание.",
        "position": "Введите позицию сортировки числом.",
    }
    if callback.message:
        await callback.message.answer(
            prompts[field],
            reply_markup=_cancel_keyboard(f"admin:partner:{link_id}"),
        )
    await _safe_answer(callback)


@router.message(AdminStates.partner_field, F.text)
async def admin_partner_field_apply(
    message: Message, context: AppContext, state: FSMContext
) -> None:
    if not await _require_admin_message(message, context):
        return
    data = await state.get_data()
    link_id = int(data["partner_id"])
    field = str(data["partner_field"])
    value = message.text.strip()
    async with session_scope(context.session_factory) as session:
        link = await session.get(PartnerLink, link_id)
        if not link:
            await message.answer(
                "Партнер не найден.", reply_markup=_cancel_keyboard("admin:partners")
            )
            return
        try:
            if field == "title":
                if not value:
                    raise ValueError("Название не может быть пустым.")
                link.title = value
            elif field == "url":
                if not _looks_like_url(value):
                    raise ValueError("Ссылка должна начинаться с http:// или https://.")
                link.url = value
            elif field == "description":
                link.description = value or None
            elif field == "position":
                link.position = int(value)
        except ValueError as exc:
            await message.answer(
                str(exc), reply_markup=_cancel_keyboard(f"admin:partner:{link_id}")
            )
            return
    await state.clear()
    await message.answer("Партнер обновлен.", reply_markup=_admin_keyboard())


@router.callback_query(F.data.startswith("admin:partner:delete:"))
async def admin_partner_delete(callback: CallbackQuery, context: AppContext) -> None:
    if not await _require_admin_callback(callback, context):
        return
    link_id = int(callback.data.removeprefix("admin:partner:delete:"))
    async with session_scope(context.session_factory) as session:
        link = await session.get(PartnerLink, link_id)
        if not link:
            await _safe_answer(callback, "Партнер не найден", show_alert=True)
            return
        await session.delete(link)
    await _safe_answer(callback, "Партнер удален", show_alert=True)


@router.callback_query(F.data.in_({"admin:settings", "adm:settings"}))
async def admin_settings(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    if not await _require_admin_callback(callback, context):
        return
    async with session_scope(context.session_factory) as session:
        settings = list(await session.scalars(select(BotSetting).order_by(BotSetting.key)))
    lines = ["Настройки бота\n\nВыберите действие:"]
    for setting in settings:
        lines.append(f"{escape(setting.key)}: {escape(str(setting.value))}")
    if callback.message:
        await callback.message.answer("\n".join(lines), reply_markup=_settings_keyboard())
    await _safe_answer(callback)


@router.callback_query(F.data == "admin:settings:set")
async def admin_setting_set_prompt(
    callback: CallbackQuery, context: AppContext, state: FSMContext
) -> None:
    if not await _require_admin_callback(callback, context):
        return
    await state.set_state(AdminStates.setting_set)
    await state.update_data(setting_key="welcome_text")
    if callback.message:
        await callback.message.answer(
            "Изменить приветствие\n\n"
            "Отправьте новый текст, который пользователь увидит после /start.",
            reply_markup=_cancel_keyboard("admin:settings"),
        )
    await _safe_answer(callback)


@router.message(AdminStates.setting_set, F.text)
async def admin_setting_set_apply(message: Message, context: AppContext, state: FSMContext) -> None:
    if not await _require_admin_message(message, context):
        return
    data = await state.get_data()
    key = data.get("setting_key", "welcome_text")
    value = message.text.strip()
    async with session_scope(context.session_factory) as session:
        setting = await session.get(BotSetting, key)
        payload = {"text": value} if key == "welcome_text" else {"value": value}
        if setting:
            setting.value = payload
        else:
            session.add(BotSetting(key=key, value=payload))
    await state.clear()
    await message.answer("Настройка сохранена.", reply_markup=_admin_keyboard())


@router.callback_query(F.data.in_({"admin:broadcast", "adm:broadcast"}))
async def admin_broadcast_prompt(
    callback: CallbackQuery, context: AppContext, state: FSMContext
) -> None:
    if not await _require_admin_callback(callback, context):
        return
    await state.set_state(AdminStates.broadcast_text)
    if callback.message:
        await callback.message.answer(
            "Рассылка\n\n"
            "Отправьте текст сообщения. Он будет отправлен всем незаблокированным пользователям.",
            reply_markup=_cancel_keyboard("admin:menu"),
        )
    await _safe_answer(callback)


@router.message(AdminStates.broadcast_text, F.text)
async def admin_broadcast_apply(
    message: Message,
    context: AppContext,
    state: FSMContext,
) -> None:
    admin = await ensure_user_for_message(message, context)
    if not is_admin_user(admin, context):
        await message.answer("Нет доступа.")
        return
    text = message.text.strip()
    if not text:
        await message.answer(
            "Текст рассылки не может быть пустым.", reply_markup=_cancel_keyboard("admin:menu")
        )
        return
    async with session_scope(context.session_factory) as session:
        recipients = await session.scalar(
            select(func.count()).select_from(User).where(User.is_blocked.is_(False))
        )
    await state.update_data(broadcast_text=text)
    await message.answer(
        f"Предпросмотр рассылки\n\nПолучателей: <b>{int(recipients or 0)}</b>\n\n{escape(text)}",
        reply_markup=_broadcast_confirm_keyboard(),
    )


@router.callback_query(AdminStates.broadcast_text, F.data == "admin:broadcast:send")
async def admin_broadcast_confirm(
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
        await _safe_answer(callback, "Состояние потеряно. Начните операцию заново.", show_alert=True)
        if callback.message:
            await callback.message.answer(_admin_home_text(), reply_markup=_admin_keyboard())
        return
    async with session_scope(context.session_factory) as session:
        broadcast = Broadcast(
            created_by_user_id=admin.id,
            title=f"Broadcast {datetime.now(timezone.utc):%Y-%m-%d %H:%M}",
            text=text,
            status="sending",
        )
        session.add(broadcast)
        await session.flush()
        broadcast_id = broadcast.id
    asyncio.create_task(
        _send_broadcast(context, bot, broadcast_id), name=f"broadcast-{broadcast_id}"
    )
    await state.clear()
    if callback.message:
        await callback.message.answer("Рассылка запущена.", reply_markup=_admin_keyboard())
    await _safe_answer(callback)


@router.callback_query(AdminStates.broadcast_text, F.data == "admin:broadcast:discard")
async def admin_broadcast_discard(
    callback: CallbackQuery, context: AppContext, state: FSMContext
) -> None:
    if not await _require_admin_callback(callback, context):
        return
    await state.clear()
    if callback.message:
        await callback.message.answer("Рассылка отменена.", reply_markup=_admin_keyboard())
    await _safe_answer(callback)


async def _send_broadcast(context: AppContext, bot: Bot, broadcast_id: int) -> None:
    async with session_scope(context.session_factory) as session:
        broadcast = await session.get(Broadcast, broadcast_id)
        if not broadcast:
            return
        users = list(
            await session.scalars(select(User).where(User.is_blocked.is_(False)).order_by(User.id))
        )
    sent = 0
    failed = 0
    for user in users:
        try:
            await bot.send_message(user.telegram_id, broadcast.text, parse_mode=None)
            sent += 1
        except Exception:
            failed += 1
        await asyncio.sleep(0.05)
    async with session_scope(context.session_factory) as session:
        broadcast = await session.get(Broadcast, broadcast_id)
        if broadcast:
            broadcast.status = "sent"
            broadcast.sent_count = sent
            broadcast.fail_count = failed
            broadcast.sent_at = datetime.now(timezone.utc)


async def _find_user(session, query: str) -> User | None:
    value = query.strip()
    if not value:
        return None
    if value.startswith("@"):
        value = value[1:]
    if value.isdigit():
        numeric = int(value)
        user = await session.scalar(select(User).where(User.telegram_id == numeric))
        if user:
            return user
        return await session.get(User, numeric)
    normalized_ref = normalize_ref_code(value)
    user = await session.scalar(select(User).where(func.lower(User.partner_code) == normalized_ref))
    if user:
        return user
    return await session.scalar(select(User).where(func.lower(User.username) == value.lower()))


async def _send_user_detail(message: Message, context: AppContext, user_id: int) -> None:
    async with session_scope(context.session_factory) as session:
        user = await session.get(User, user_id)
        if not user:
            await message.answer(
                "Пользователь не найден.", reply_markup=_cancel_keyboard("admin:users")
            )
            return
        tasks_count = await session.scalar(
            select(func.count()).select_from(GalleryItem).where(GalleryItem.user_id == user.id)
        )
        payments_count = await session.scalar(
            select(func.count()).select_from(Payment).where(Payment.user_id == user.id)
        )
        payments_paid = await session.scalar(
            select(func.count())
            .select_from(Payment)
            .where(Payment.user_id == user.id, Payment.status == "paid")
        )
        env_admin = user.telegram_id in context.settings.admin_ids
        text = _user_detail_text(
            user,
            env_admin=env_admin,
            gallery_count=int(tasks_count or 0),
            payments_count=int(payments_count or 0),
            payments_paid=int(payments_paid or 0),
        )
        keyboard = _user_detail_keyboard(user.id, user.is_blocked, user.is_admin, env_admin)
    await message.answer(text, reply_markup=keyboard)


def _user_detail_text(
    user: User,
    *,
    env_admin: bool,
    gallery_count: int,
    payments_count: int,
    payments_paid: int,
) -> str:
    flags = []
    if user.is_admin:
        flags.append("admin")
    if env_admin:
        flags.append("env-admin")
    if user.is_blocked:
        flags.append("blocked")
    if not flags:
        flags.append("user")
    unlimited = (
        f"\nБезлимит до: <b>{user.unlimited_until:%Y-%m-%d %H:%M}</b>"
        if user.unlimited_until
        else ""
    )
    affiliate_rate_bps = (
        3000 if user.affiliate_commission_rate_bps is None else user.affiliate_commission_rate_bps
    )
    return (
        "Пользователь\n\n"
        f"ID: <code>{user.id}</code>\n"
        f"Telegram ID: <code>{user.telegram_id}</code>\n"
        f"Имя: <b>{escape(mention_user(user))}</b>\n"
        f"Роль/статус: <b>{escape(', '.join(flags))}</b>\n"
        "Баланс:\n"
        f"Фото: <b>{int(user.photo_credits_balance or 0)}</b>\n"
        f"Видео: <b>{int(user.video_credits_balance or 0)}</b>\n"
        f"Универсальные: <b>{int(user.credits_balance or 0)}</b>{unlimited}\n"
        f"Партнерский код: <code>{escape(str(user.partner_code or '-'))}</code>\n"
        f"Ставка партнерки: <b>{affiliate_rate_bps / 100:.0f}%</b>\n"
        f"Партнерский баланс: <b>{(user.affiliate_balance_kopecks or 0) / 100:.0f} ₽</b>\n"
        f"Галерея: <b>{gallery_count}</b>\n"
        f"Платежи: <b>{payments_count}</b>, оплачено <b>{payments_paid}</b>"
    )


def _user_detail_keyboard(
    user_id: int, is_blocked: bool, is_admin: bool, env_admin: bool
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Универсальные", callback_data=f"admin:user:adjust:credits:{user_id}")
    builder.button(text="Фото-кредиты", callback_data=f"admin:user:adjust:photo_credits:{user_id}")
    builder.button(text="Видео-кредиты", callback_data=f"admin:user:adjust:video_credits:{user_id}")
    builder.button(text="Безлимит", callback_data=f"admin:user:adjust:unlimited:{user_id}")
    builder.button(
        text="Ставка партнерки", callback_data=f"admin:user:adjust:affiliate_rate:{user_id}"
    )
    builder.button(
        text="Разблокировать" if is_blocked else "Заблокировать",
        callback_data=f"admin:user:toggle_block:{user_id}",
    )
    admin_label = "Env admin" if env_admin else ("Снять админа" if is_admin else "Сделать админом")
    builder.button(text=admin_label, callback_data=f"admin:user:toggle_admin:{user_id}")
    nav_count = add_navigation_buttons(builder, back_callback="admin:users")
    builder.adjust(2, 2, 2, 1, nav_count)
    return builder.as_markup()


def _admin_packages_view(packages: list[CreditPackage]) -> tuple[str, InlineKeyboardMarkup]:
    lines = ["Пакеты и безлимиты\n\nЗдесь управляются тарифы, цены и условия."]
    builder = InlineKeyboardBuilder()
    visible_packages = [item for item in packages if not package_is_technical(item)]
    hidden_count = len(packages) - len(visible_packages)
    for item in visible_packages:
        flag = "включен" if item.is_enabled else "выключен"
        kind = package_credits_text(item)
        lines.append(
            f"{item.id}. {escape(item.title)} · {escape(kind)} · "
            f"{float(item.price_rub):.0f} ₽ · {flag}"
        )
        builder.button(text=f"{item.id}. {item.title}", callback_data=f"admin:package:{item.id}")
        builder.button(
            text="Выключить" if item.is_enabled else "Включить",
            callback_data=f"admin:package:toggle:{item.id}",
        )
    if not visible_packages:
        lines.append("Пакеты пока не настроены.")
    if hidden_count:
        lines.append(f"Служебные тестовые записи скрыты: {hidden_count}.")
    builder.button(text="Добавить пакет", callback_data="admin:package:add")
    nav_count = add_navigation_buttons(builder, back_callback="admin:menu")
    builder.adjust(*([2] * len(visible_packages)), 1, nav_count)
    return "\n".join(lines), builder.as_markup()


def _package_detail_text(package: CreditPackage, payments_count: int) -> str:
    return (
        "Пакет\n\n"
        f"ID: <code>{package.id}</code>\n"
        f"Код: <code>{escape(package.code)}</code>\n"
        f"Название: <b>{escape(package.title)}</b>\n"
        f"Описание: {escape(package.description or '-')}\n"
        f"Условия: {escape(package.terms or '-')}\n"
        f"Тип: <b>{_package_kind(package)}</b>\n"
        f"Фото-кредиты: <b>{int(package.photo_credits or 0)}</b>\n"
        f"Видео-кредиты: <b>{int(package.video_credits or 0)}</b>\n"
        f"Универсальные кредиты: <b>{int(package.credits or 0)}</b>\n"
        f"Цена: <b>{float(package.price_rub):.2f} ₽</b>\n"
        f"Позиция: <b>{package.position}</b>\n"
        f"Статус: <b>{'включен' if package.is_enabled else 'выключен'}</b>\n"
        f"Служебный: <b>{'да' if package_is_technical(package) else 'нет'}</b>\n"
        f"Платежей: <b>{payments_count}</b>"
    )


def _package_kind(package: CreditPackage) -> str:
    return package_credits_text(package)


def _package_keyboard(package_id: int, is_enabled: bool | None = None) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    toggle_text = (
        "Включить / выключить"
        if is_enabled is None
        else ("Выключить" if is_enabled else "Включить")
    )
    builder.button(text=toggle_text, callback_data=f"admin:package:toggle:{package_id}")
    builder.button(text="Название", callback_data=f"admin:package:edit:title:{package_id}")
    builder.button(text="Описание", callback_data=f"admin:package:edit:description:{package_id}")
    builder.button(text="Условия", callback_data=f"admin:package:edit:terms:{package_id}")
    builder.button(
        text="Фото-кредиты", callback_data=f"admin:package:edit:photo_credits:{package_id}"
    )
    builder.button(
        text="Видео-кредиты", callback_data=f"admin:package:edit:video_credits:{package_id}"
    )
    builder.button(text="Универсальные", callback_data=f"admin:package:edit:credits:{package_id}")
    builder.button(text="Цена", callback_data=f"admin:package:edit:price:{package_id}")
    builder.button(text="Срок", callback_data=f"admin:package:edit:duration:{package_id}")
    builder.button(text="Безлимит?", callback_data=f"admin:package:edit:unlimited:{package_id}")
    builder.button(text="Позиция", callback_data=f"admin:package:edit:position:{package_id}")
    builder.button(text="Удалить", callback_data=f"admin:package:delete:{package_id}")
    nav_count = add_navigation_buttons(builder, back_callback="admin:packages")
    builder.adjust(1, 2, 2, 2, 2, 2, 1, nav_count)
    return builder.as_markup()


def _payment_detail_text(payment: Payment, package: CreditPackage | None, user: User | None) -> str:
    snapshot = payment_package_snapshot(payment)
    package_title = _payment_package_title(snapshot or package)
    user_text = mention_user(user) if user else f"user {payment.user_id}"
    provider_id = payment.provider_payment_id or "-"
    url = payment.payment_url or "-"
    return (
        "Платеж\n\n"
        f"ID: <code>{payment.id}</code>\n"
        f"Order ID: <code>{escape(payment.order_id)}</code>\n"
        f"Пользователь: <b>{escape(user_text)}</b> / <code>{payment.user_id}</code>\n"
        f"Пакет: <b>{escape(package_title)}</b>\n"
        f"Сумма: <b>{payment.amount_kopecks / 100:.2f} ₽</b>\n"
        f"Статус: <b>{escape(payment.status)}</b>\n"
        f"Провайдер: <b>{escape(payment.provider)}</b>\n"
        f"Provider payment ID: <code>{escape(provider_id)}</code>\n"
        f"Ссылка: {escape(url)}"
    )


def _payment_keyboard(payment_id: int, status: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    action_rows = 0
    if status == "manual_pending":
        builder.button(text="Подтвердить оплату", callback_data=f"admin:payment:paid:{payment_id}")
        action_rows += 1
    if status != "paid":
        builder.button(text="Отменить", callback_data=f"admin:payment:cancel:{payment_id}")
        action_rows += 1
    nav_count = add_navigation_buttons(builder, back_callback="admin:payments")
    builder.adjust(*([1] * action_rows), nav_count)
    return builder.as_markup()


async def _mark_payment_paid(session, payment: Payment) -> PaymentMarkResult:
    if payment.status == "paid":
        return PaymentMarkResult(ok=False, admin_text="Платеж уже отмечен оплаченным.")
    if payment.status != "manual_pending":
        return PaymentMarkResult(
            ok=False,
            admin_text="Вручную можно подтверждать только заявки manual_pending.",
        )
    snapshot = payment_package_snapshot(payment)
    package = await session.get(CreditPackage, payment.package_id) if payment.package_id else None
    user = await session.get(User, payment.user_id, with_for_update=True)
    if not user or not (snapshot or package):
        return PaymentMarkResult(ok=False, admin_text="Не найден пользователь или пакет.")
    if snapshot:
        await apply_package_snapshot_to_user(session, user=user, snapshot=snapshot)
        package_for_text: CreditPackage | dict[str, Any] = snapshot
    else:
        await apply_package_to_user(session, user=user, package=package)
        package_for_text = package
    await apply_affiliate_commission(session, payment=payment, buyer=user)
    payment.status = "paid"
    if _payment_package_is_unlimited(package_for_text) and user.unlimited_until:
        balance = f"Безлимит активен до {user.unlimited_until:%Y-%m-%d %H:%M}.\n{_user_credit_balance_text(user)}"
    else:
        balance = _user_credit_balance_text(user)
    notify_text = (
        "Оплата подтверждена.\n\n"
        f"Пакет: <b>{escape(_payment_package_title(package_for_text))}</b>\n"
        f"{balance}\n\n"
        "Можно запускать генерации."
    )
    return PaymentMarkResult(
        ok=True,
        admin_text="Платеж отмечен оплаченным.",
        notify_chat_id=user.telegram_id,
        notify_text=notify_text,
    )


def _user_credit_balance_text(user: User) -> str:
    return (
        "Баланс:\n"
        f"Фото: <b>{int(user.photo_credits_balance or 0)}</b>\n"
        f"Видео: <b>{int(user.video_credits_balance or 0)}</b>\n"
        f"Универсальные: <b>{int(user.credits_balance or 0)}</b>"
    )


def _payment_package_title(package: CreditPackage | dict[str, Any] | None) -> str:
    if isinstance(package, dict):
        return str(package.get("title") or "-")
    if package:
        return package.title
    return "-"


def _payment_package_is_unlimited(package: CreditPackage | dict[str, Any]) -> bool:
    if isinstance(package, dict):
        return bool(package.get("is_unlimited"))
    return bool(package.is_unlimited)


def _gallery_detail_text(item: GalleryItem) -> str:
    return (
        "Элемент галереи\n\n"
        f"ID: <code>{item.id}</code>\n"
        f"Название: <b>{escape(item.title or '-')}</b>\n"
        f"Тип: <b>{escape(item.media_type)}</b>\n"
        f"Статус: <b>{'public' if item.is_public else 'hidden'}</b>\n"
        f"Featured: <b>{'да' if item.is_featured else 'нет'}</b>\n"
        f"Модель: <code>{escape(item.model_code or '-')}</code>\n"
        f"URL: {escape(item.media_url)}\n\n"
        f"Промпт:\n{escape(item.prompt or '-')}"
    )


def _gallery_item_keyboard(item_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Public / hidden", callback_data=f"admin:gallery:toggle_public:{item_id}")
    builder.button(text="Featured", callback_data=f"admin:gallery:toggle_featured:{item_id}")
    builder.button(text="URL", callback_data=f"admin:gallery:edit:url:{item_id}")
    builder.button(text="Тип", callback_data=f"admin:gallery:edit:type:{item_id}")
    builder.button(text="Название", callback_data=f"admin:gallery:edit:title:{item_id}")
    builder.button(text="Промпт", callback_data=f"admin:gallery:edit:prompt:{item_id}")
    builder.button(text="Удалить", callback_data=f"admin:gallery:delete:{item_id}")
    nav_count = add_navigation_buttons(builder, back_callback="admin:gallery")
    builder.adjust(2, 2, 2, 1, nav_count)
    return builder.as_markup()


def _partner_detail_text(link: PartnerLink) -> str:
    return (
        "Партнерская ссылка\n\n"
        f"ID: <code>{link.id}</code>\n"
        f"Код: <code>{escape(link.code)}</code>\n"
        f"Название: <b>{escape(link.title)}</b>\n"
        f"Статус: <b>{'включен' if link.is_enabled else 'выключен'}</b>\n"
        f"Позиция: <b>{link.position}</b>\n"
        f"Кликов: <b>{link.clicks}</b>\n"
        f"URL: {escape(link.url)}\n"
        f"Описание: {escape(link.description or '-')}"
    )


def _partner_item_keyboard(link_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Включить / выключить", callback_data=f"admin:partner:toggle:{link_id}")
    builder.button(text="Название", callback_data=f"admin:partner:edit:title:{link_id}")
    builder.button(text="URL", callback_data=f"admin:partner:edit:url:{link_id}")
    builder.button(text="Описание", callback_data=f"admin:partner:edit:description:{link_id}")
    builder.button(text="Позиция", callback_data=f"admin:partner:edit:position:{link_id}")
    builder.button(text="Удалить", callback_data=f"admin:partner:delete:{link_id}")
    nav_count = add_navigation_buttons(builder, back_callback="admin:partners")
    builder.adjust(1, 2, 2, 1, nav_count)
    return builder.as_markup()


def _broadcast_confirm_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Отправить", callback_data="admin:broadcast:send")
    builder.button(text="Отменить", callback_data="admin:broadcast:discard")
    nav_count = add_navigation_buttons(builder, back_callback="admin:menu")
    builder.adjust(2, nav_count)
    return builder.as_markup()


def _parse_bool(value: str) -> bool:
    normalized = value.strip().lower()
    if normalized in {"да", "yes", "y", "true", "1", "on"}:
        return True
    if normalized in {"нет", "no", "n", "false", "0", "off"}:
        return False
    raise ValueError("Ответьте да или нет.")


def _looks_like_url(value: str) -> bool:
    return value.startswith(("http://", "https://"))


async def _require_admin_message(message: Message, context: AppContext) -> bool:
    user = await ensure_user_for_message(message, context)
    if not is_admin_user(user, context):
        await message.answer("Нет доступа.", reply_markup=main_menu(False))
        return False
    return True


async def _require_admin_callback(callback: CallbackQuery, context: AppContext) -> bool:
    user = await ensure_user_for_callback(callback, context)
    if not is_admin_user(user, context):
        await _safe_answer(callback, "Нет доступа", show_alert=True)
        return False
    return True



def _task_summary_line(task: GenerationTask) -> str:
    prompt = _short_text(task.prompt or "без промпта", 48)
    return (
        f"#{task.id} · <b>{escape(task.status)}</b> · {escape(task.model_code)} · "
        f"user {task.user_id} · {escape(prompt)}"
    )


def _task_detail_text(task: GenerationTask, user: User | None) -> str:
    prompt = escape(_short_text(task.prompt or "", 700))
    error = escape(_short_text(task.error_message or "", 500))
    provider_task_id = escape(_short_text(task.provider_task_id or "—", 120))
    username = mention_user(user) if user else f"user {task.user_id}"
    return (
        f"🧾 Операция #{task.id}\n\n"
        f"Пользователь: <b>{escape(username)}</b>\n"
        f"Модель: <b>{escape(task.model_code)}</b>\n"
        f"Статус: <b>{escape(task.status)}</b>\n"
        f"Кредиты: <b>{int(task.cost_credits or 0)}</b>\n"
        f"Provider task: <code>{provider_task_id}</code>\n"
        f"Создана: {task.created_at:%Y-%m-%d %H:%M}\n"
        f"Обновлена: {task.updated_at:%Y-%m-%d %H:%M}\n\n"
        f"Промпт:\n{prompt or '—'}"
        + (f"\n\nОшибка:\n<code>{error}</code>" if error else "")
    )


def _task_detail_keyboard(task: GenerationTask) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    if task.provider_task_id and task.status != "success":
        builder.button(text="🔁 Повторно проверить статус", callback_data=f"admin:task:retry:{task.id}")
    builder.button(text="🧾 К списку операций", callback_data="admin:orders")
    nav_count = add_navigation_buttons(builder, back_callback="admin:menu")
    builder.adjust(1, nav_count)
    return builder.as_markup()


def _support_admin_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="👤 Найти пользователя", callback_data="admin:users:find")
    builder.button(text="💰 Начислить", callback_data="admin:users:grant")
    builder.button(text="🚫 Бан / Разбан", callback_data="admin:users:block")
    builder.button(text="📢 Рассылка", callback_data="admin:broadcast")
    nav_count = add_navigation_buttons(builder, back_callback="admin:menu")
    builder.adjust(1, 2, 1, nav_count)
    return builder.as_markup()


async def _recent_error_logs_text() -> str:
    cmd = [
        "journalctl",
        "-u",
        "stupidbot",
        "--since",
        "24 hours ago",
        "--no-pager",
        "-n",
        "200",
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=5)
    except Exception:
        return "🪵 Логи ошибок\n\nНе удалось получить логи. Подробности записаны в systemd/journal."
    raw = (stdout or stderr or b"").decode("utf-8", errors="replace")
    lines = [line for line in raw.splitlines() if re.search(r"traceback|error|exception|failed|critical|nameerror|attributeerror|typeerror", line, re.I)]
    lines = [_sanitize_log_line(line) for line in lines[-20:]]
    if not lines:
        return "🪵 Логи ошибок\n\nЗа последние 24 часа явных ошибок в journal не найдено."
    body = escape("\n".join(lines))
    return f"🪵 Логи ошибок\n\n<code>{body}</code>"


def _sanitize_log_line(line: str) -> str:
    sanitized = re.sub(r"(?i)(token|password|api[_-]?key|secret)=([^\s]+)", r"\1=***", line)
    sanitized = re.sub(r"Bearer\s+[A-Za-z0-9._\-]+", "Bearer ***", sanitized)
    return _short_text(sanitized, 600)


def _short_text(value: str, limit: int) -> str:
    value = " ".join(str(value).split())
    if len(value) <= limit:
        return value
    return value[: max(0, limit - 1)] + "…"

def _admin_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    # APIX-style admin entry points, backed by StupidBot-native models/handlers.
    buttons = [
        ("📊 Статистика", "admin:stats"),
        ("📈 Аналитика", "admin:analytics"),
        ("👤 Пользователи", "admin:users"),
        ("💰 Начислить", "admin:users:grant"),
        ("🚫 Бан / Разбан", "admin:users:block"),
        ("💳 Платежи", "admin:payments"),
        ("🧾 Заказы / операции", "admin:orders"),
        ("📦 Пакеты и безлимит", "admin:packages"),
        ("⚙️ Модели и цены", "admin:models"),
        ("👥 Рефералы", "admin:referrals"),
        ("💸 Заявки на вывод", "admin:withdrawals"),
        ("🖼 Публичная галерея", "admin:gallery"),
        ("🤝 Партнеры", "admin:partners"),
        ("🧾 Тексты и настройки", "admin:settings"),
        ("🆘 Обращения", "admin:support"),
        ("🪵 Логи ошибок", "admin:logs"),
        ("📢 Рассылка", "admin:broadcast"),
    ]
    for text, callback_data in buttons:
        builder.button(text=text, callback_data=callback_data)
    builder.button(text="🏠 Главное меню", callback_data="menu:main")
    builder.adjust(2)
    return builder.as_markup()


def _admin_home_text() -> str:
    return (
        "Админка\n\n"
        "Выберите раздел управления. Все изменения применяются сразу и сохраняются в базе."
    )


def _back_admin_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    nav_count = add_navigation_buttons(builder, back_callback="admin:menu")
    builder.adjust(nav_count)
    return builder.as_markup()


def _cancel_keyboard(back_to: str = "admin:menu") -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Отмена", callback_data="admin:cancel")
    nav_count = add_navigation_buttons(builder, back_callback=back_to)
    builder.adjust(1, nav_count)
    return builder.as_markup()


def _users_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Начислить универсальные кредиты", callback_data="admin:users:grant")
    builder.button(text="Заблокировать / разблокировать", callback_data="admin:users:block")
    nav_count = add_navigation_buttons(builder, back_callback="admin:menu")
    builder.adjust(1, 1, nav_count)
    return builder.as_markup()


def _model_keyboard(model_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Включить / выключить", callback_data=f"admin:model:toggle:{model_id}")
    builder.button(text="Изменить цену", callback_data=f"admin:model:price:{model_id}")
    builder.button(text="Название", callback_data=f"admin:model:edit:title:{model_id}")
    builder.button(text="Описание", callback_data=f"admin:model:edit:description:{model_id}")
    builder.button(text="Позиция", callback_data=f"admin:model:edit:position:{model_id}")
    nav_count = add_navigation_buttons(builder, back_callback="admin:models")
    builder.adjust(1, 1, 2, 1, nav_count)
    return builder.as_markup()


def _payments_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Отметить платеж оплаченным", callback_data="admin:payments:mark_paid")
    nav_count = add_navigation_buttons(builder, back_callback="admin:menu")
    builder.adjust(1, nav_count)
    return builder.as_markup()


def _gallery_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Добавить работу", callback_data="admin:gallery:add")
    builder.button(text="Скрыть / показать", callback_data="admin:gallery:toggle")
    nav_count = add_navigation_buttons(builder, back_callback="admin:menu")
    builder.adjust(1, 1, nav_count)
    return builder.as_markup()


def _gallery_type_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Изображение", callback_data="admin:gallery:type:image")
    builder.button(text="Видео", callback_data="admin:gallery:type:video")
    builder.button(text="Отмена", callback_data="admin:cancel")
    nav_count = add_navigation_buttons(builder, back_callback="admin:gallery")
    builder.adjust(2, 1, nav_count)
    return builder.as_markup()


def _partners_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Добавить партнера", callback_data="admin:partners:add")
    builder.button(text="Включить / выключить", callback_data="admin:partners:toggle")
    nav_count = add_navigation_buttons(builder, back_callback="admin:menu")
    builder.adjust(1, 1, nav_count)
    return builder.as_markup()


def _settings_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Изменить приветствие", callback_data="admin:settings:set")
    nav_count = add_navigation_buttons(builder, back_callback="admin:menu")
    builder.adjust(1, nav_count)
    return builder.as_markup()


def setup(dispatcher: Dispatcher, context: AppContext) -> None:
    dispatcher.include_router(router)
