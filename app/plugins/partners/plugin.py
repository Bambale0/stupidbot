from __future__ import annotations

from contextlib import suppress
from html import escape

from aiogram import Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.context import AppContext
from app.db import session_scope
from app.models import AffiliateWithdrawal, PartnerLink, User
from app.plugins.common import ensure_user_for_callback, ensure_user_for_message, is_admin_user
from app.repositories import ensure_partner_code, get_enabled_partner_links
from app.services.referrals import build_ref_link
from app.ui import ACCOUNT_MENU_CALLBACK, add_navigation_buttons, navigation_keyboard

router = Router(name="partners")


class PartnerStates(StatesGroup):
    withdrawal_details = State()


@router.message(F.text.in_({"Партнёры", "Партнеры", "Партнёрская программа", "Партнерка"}))
@router.message(Command("partners"))
async def partners(message: Message, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    user = await ensure_user_for_message(message, context)
    await _send_partners(message, context, user.id)


@router.callback_query(F.data == "menu:partners")
async def partners_callback(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    user = await ensure_user_for_callback(callback, context)
    if callback.message:
        await _send_partners(callback.message, context, user.id)
    await callback.answer()


async def _send_partners(message: Message, context: AppContext, user_id: int) -> None:
    async with session_scope(context.session_factory) as session:
        links = await get_enabled_partner_links(session)
        user = await session.get(User, user_id)
        if user:
            await ensure_partner_code(session, user)
            partner_code = user.partner_code
            affiliate_balance = int(user.affiliate_balance_kopecks or 0)
            affiliate_earned = int(user.affiliate_earned_kopecks or 0)
            rate_bps = 3000 if user.affiliate_commission_rate_bps is None else int(
                user.affiliate_commission_rate_bps
            )
        else:
            partner_code = None
            affiliate_balance = 0
            affiliate_earned = 0
            rate_bps = 3000
    ref_link = await build_ref_link(context.bot, partner_code)
    text = (
        "<b>Партнёрская программа</b>\n\n"
        f"Ставка: <b>{rate_bps / 100:.0f}%</b>\n"
        f"Заработано: <b>{affiliate_earned / 100:.0f} ₽</b>\n"
        f"Доступно к выводу: <b>{affiliate_balance / 100:.0f} ₽</b>"
    )
    if ref_link:
        text += f"\n\nВаша ссылка:\n{ref_link}"
    if rate_bps < 5000:
        text += "\n\nСтатус амбассадора и ставка 50% — через поддержку."
    if links:
        text += "\n\nПолезные ссылки:"
    await message.answer(text, reply_markup=_partner_keyboard(links, affiliate_balance > 0))


@router.callback_query(F.data == "partner:withdraw")
async def withdraw_prompt(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    user = await ensure_user_for_callback(callback, context)
    async with session_scope(context.session_factory) as session:
        fresh_user = await session.get(User, user.id)
        balance = int(fresh_user.affiliate_balance_kopecks or 0) if fresh_user else 0
    if balance <= 0:
        await callback.answer("Нет средств для вывода", show_alert=True)
        return
    await state.set_state(PartnerStates.withdrawal_details)
    if callback.message:
        await callback.message.answer(
            "<b>Запрос на вывод</b>\n\n"
            f"Доступно: <b>{balance / 100:.0f} ₽</b>\n"
            "Отправьте реквизиты одним сообщением: банк, карта или телефон.",
            reply_markup=navigation_keyboard(back_callback="menu:partners"),
        )
    await callback.answer()


@router.message(PartnerStates.withdrawal_details, F.text)
async def withdraw_create(message: Message, context: AppContext, state: FSMContext) -> None:
    user = await ensure_user_for_message(message, context)
    details = (message.text or "").strip()
    if len(details) < 5:
        await message.answer(
            "Укажите банк и реквизиты подробнее.",
            reply_markup=navigation_keyboard(back_callback="menu:partners"),
        )
        return
    details = details[:1200]
    async with session_scope(context.session_factory) as session:
        fresh_user = await session.get(User, user.id, with_for_update=True)
        if not fresh_user:
            await message.answer("Пользователь не найден.", reply_markup=navigation_keyboard())
            await state.clear()
            return
        amount = int(fresh_user.affiliate_balance_kopecks or 0)
        if amount <= 0:
            await message.answer("Нет средств для вывода.", reply_markup=navigation_keyboard())
            await state.clear()
            return
        fresh_user.affiliate_balance_kopecks = 0
        withdrawal = AffiliateWithdrawal(
            user_id=fresh_user.id,
            amount_kopecks=amount,
            status="pending",
            details=details,
        )
        session.add(withdrawal)
        await session.flush()
        withdrawal_id = withdrawal.id
        telegram_id = fresh_user.telegram_id
        username = fresh_user.username
    await state.clear()
    await message.answer(
        "Заявка создана.\n\n"
        f"Сумма: <b>{amount / 100:.0f} ₽</b>\n"
        "Средства зарезервированы до обработки.",
        reply_markup=navigation_keyboard(back_callback="menu:partners"),
    )
    await _notify_admins_about_withdrawal(
        context,
        withdrawal_id=withdrawal_id,
        telegram_id=telegram_id,
        username=username,
        amount_kopecks=amount,
        details=details,
    )


@router.callback_query(F.data.startswith("partner:withdrawal:paid:"))
async def withdrawal_paid(callback: CallbackQuery, context: AppContext) -> None:
    if not await _require_admin(callback, context):
        return
    withdrawal_id = int(callback.data.removeprefix("partner:withdrawal:paid:"))
    async with session_scope(context.session_factory) as session:
        withdrawal = await session.get(AffiliateWithdrawal, withdrawal_id, with_for_update=True)
        if not withdrawal or withdrawal.status != "pending":
            await callback.answer("Заявка уже обработана", show_alert=True)
            return
        withdrawal.status = "paid"
        user = await session.get(User, withdrawal.user_id)
        notify_chat_id = user.telegram_id if user else None
        amount = withdrawal.amount_kopecks
    if context.bot and notify_chat_id:
        with suppress(Exception):
            await context.bot.send_message(
                notify_chat_id,
                f"Выплата выполнена.\n\nСумма: <b>{amount / 100:.0f} ₽</b>",
            )
    await callback.answer("Отмечено выплаченным", show_alert=True)


@router.callback_query(F.data.startswith("partner:withdrawal:reject:"))
async def withdrawal_reject(callback: CallbackQuery, context: AppContext) -> None:
    if not await _require_admin(callback, context):
        return
    withdrawal_id = int(callback.data.removeprefix("partner:withdrawal:reject:"))
    async with session_scope(context.session_factory) as session:
        withdrawal = await session.get(AffiliateWithdrawal, withdrawal_id, with_for_update=True)
        if not withdrawal or withdrawal.status != "pending":
            await callback.answer("Заявка уже обработана", show_alert=True)
            return
        user = await session.get(User, withdrawal.user_id, with_for_update=True)
        withdrawal.status = "rejected"
        amount = withdrawal.amount_kopecks
        notify_chat_id = None
        if user:
            user.affiliate_balance_kopecks += amount
            notify_chat_id = user.telegram_id
    if context.bot and notify_chat_id:
        with suppress(Exception):
            await context.bot.send_message(
                notify_chat_id,
                "Заявка отклонена. Средства возвращены на партнёрский баланс.",
            )
    await callback.answer("Средства возвращены", show_alert=True)


@router.callback_query(F.data.startswith("partner:open:"))
async def open_partner(callback: CallbackQuery, context: AppContext) -> None:
    await ensure_user_for_callback(callback, context)
    link_id = int(callback.data.removeprefix("partner:open:"))
    async with session_scope(context.session_factory) as session:
        link = await session.get(PartnerLink, link_id)
        if not link or not link.is_enabled:
            await callback.answer("Ссылка недоступна", show_alert=True)
            return
        link.clicks += 1
        url = link.url
    await callback.answer(url=url)


def setup(dispatcher: Dispatcher, context: AppContext) -> None:
    dispatcher.include_router(router)


def _partner_keyboard(links: list[PartnerLink], can_withdraw: bool):
    builder = InlineKeyboardBuilder()
    if can_withdraw:
        builder.button(text="Запросить вывод", callback_data="partner:withdraw")
    for link in links:
        builder.button(text=link.title, callback_data=f"partner:open:{link.id}")
    nav_count = add_navigation_buttons(builder, back_callback=ACCOUNT_MENU_CALLBACK)
    rows = []
    if can_withdraw:
        rows.append(1)
    rows.extend([1] * len(links))
    rows.append(nav_count)
    builder.adjust(*rows)
    return builder.as_markup()


def _withdrawal_admin_keyboard(withdrawal_id: int):
    builder = InlineKeyboardBuilder()
    builder.button(text="Выплачено", callback_data=f"partner:withdrawal:paid:{withdrawal_id}")
    builder.button(text="Отклонить и вернуть", callback_data=f"partner:withdrawal:reject:{withdrawal_id}")
    builder.adjust(1, 1)
    return builder.as_markup()


async def _notify_admins_about_withdrawal(
    context: AppContext,
    *,
    withdrawal_id: int,
    telegram_id: int,
    username: str | None,
    amount_kopecks: int,
    details: str,
) -> None:
    if not context.bot:
        return
    username_line = f"@{escape(username)}" if username else "—"
    text = (
        "<b>Новая заявка на вывод</b>\n\n"
        f"Заявка: <code>#{withdrawal_id}</code>\n"
        f"Пользователь: <code>{telegram_id}</code> {username_line}\n"
        f"Сумма: <b>{amount_kopecks / 100:.0f} ₽</b>\n\n"
        f"Реквизиты:\n<code>{escape(details)}</code>"
    )
    for admin_id in context.settings.admin_ids:
        with suppress(Exception):
            await context.bot.send_message(
                admin_id,
                text,
                reply_markup=_withdrawal_admin_keyboard(withdrawal_id),
            )


async def _require_admin(callback: CallbackQuery, context: AppContext) -> bool:
    user = await ensure_user_for_callback(callback, context)
    if not is_admin_user(user, context):
        await callback.answer("Нет доступа", show_alert=True)
        return False
    return True
