from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from html import escape
from typing import Any

from aiogram import Bot, Dispatcher, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from app.context import AppContext
from app.db import session_scope
from app.models import (
    AffiliateLedgerEntry,
    CreditLedgerEntry,
    CreditPackage,
    Payment,
    User,
)
from app.plugins.admin import plugin as admin_plugin
from app.plugins.common import (
    ensure_user_for_callback,
    ensure_user_for_message,
    is_admin_user,
)
from app.repositories import package_grants_value, package_is_technical, payment_package_snapshot
from app.services.financial_credits import reverse_paid_payment
from app.ui import add_navigation_buttons

router = Router(name="admin_finance")


class FinancialAdminStates(StatesGroup):
    payment_action_reason = State()


_BALANCE_FIELDS = {
    "credits": ("credits_balance", "common"),
    "photo_credits": ("photo_credits_balance", "photo"),
    "video_credits": ("video_credits_balance", "video"),
}


def _operation_key(message: Message, action: str, target: int | str) -> str:
    chat_id = getattr(message.chat, "id", 0)
    return f"admin:{chat_id}:{message.message_id}:{action}:{target}"


def _reasoned_text(value: str) -> tuple[str, str]:
    payload, separator, reason = value.partition("|")
    payload = payload.strip()
    reason = reason.strip()
    if not separator or len(reason) < 3:
        raise ValueError("Добавьте причину после символа |. Пример: 20 | компенсация за сбой")
    return payload, reason[:500]


def _positive_price(value: Any) -> bool:
    try:
        return Decimal(str(value or "0")) > 0
    except (InvalidOperation, ValueError, TypeError):
        return False


def _sale_values_valid(
    *,
    price_rub: Any,
    credits: int,
    photo_credits: int,
    video_credits: int,
    is_unlimited: bool,
    duration_days: int | None,
) -> bool:
    grants = max(0, credits) + max(0, photo_credits) + max(0, video_credits)
    unlimited_valid = bool(is_unlimited and int(duration_days or 0) > 0)
    return _positive_price(price_rub) and (grants > 0 or unlimited_valid)


def _package_can_be_enabled(package: CreditPackage) -> bool:
    return (
        not package_is_technical(package)
        and _sale_values_valid(
            price_rub=package.price_rub,
            credits=int(package.credits or 0),
            photo_credits=int(package.photo_credits or 0),
            video_credits=int(package.video_credits or 0),
            is_unlimited=bool(package.is_unlimited),
            duration_days=package.duration_days,
        )
    )


async def _require_admin_message(message: Message, context: AppContext) -> User | None:
    admin = await ensure_user_for_message(message, context)
    if not is_admin_user(admin, context):
        await message.answer("Нет доступа.")
        return None
    return admin


async def _require_admin_callback(callback: CallbackQuery, context: AppContext) -> User | None:
    admin = await ensure_user_for_callback(callback, context)
    if not is_admin_user(admin, context):
        await admin_plugin._safe_answer(callback, "Нет доступа", show_alert=True)
        return None
    return admin


async def _append_credit_audit(
    session,
    *,
    user: User,
    credit_type: str,
    before: int,
    after: int,
    reason: str,
    admin: User,
    operation_key: str,
    reference_type: str,
    reference_id: str,
    metadata: dict[str, Any] | None = None,
) -> None:
    keyed_operation = f"{operation_key}:{credit_type}"
    existing = await session.scalar(
        select(CreditLedgerEntry.id).where(CreditLedgerEntry.operation_key == keyed_operation)
    )
    if existing:
        return
    session.add(
        CreditLedgerEntry(
            user_id=user.id,
            credit_type=credit_type,
            balance_delta=int(after) - int(before),
            debt_delta=0,
            reason="admin_adjustment",
            reference_type=reference_type,
            reference_id=reference_id,
            operation_key=keyed_operation,
            metadata_json={
                "actor_user_id": admin.id,
                "actor_telegram_id": admin.telegram_id,
                "reason": reason,
                "before": before,
                "after": after,
                **dict(metadata or {}),
            },
        )
    )


async def _append_affiliate_audit(
    session,
    *,
    user: User,
    before_balance: int,
    after_balance: int,
    before_earned: int,
    after_earned: int,
    before_debt: int,
    after_debt: int,
    reason: str,
    admin: User,
    operation_key: str,
    reference_id: str,
) -> None:
    keyed_operation = f"{operation_key}:affiliate:{user.id}"
    existing = await session.scalar(
        select(AffiliateLedgerEntry.id).where(
            AffiliateLedgerEntry.operation_key == keyed_operation
        )
    )
    if existing:
        return
    session.add(
        AffiliateLedgerEntry(
            user_id=user.id,
            balance_delta_kopecks=after_balance - before_balance,
            earned_delta_kopecks=after_earned - before_earned,
            debt_delta_kopecks=after_debt - before_debt,
            reason="admin_payment_action",
            reference_type="payment",
            reference_id=reference_id,
            operation_key=keyed_operation,
            metadata_json={
                "actor_user_id": admin.id,
                "actor_telegram_id": admin.telegram_id,
                "reason": reason,
            },
        )
    )


def _credit_snapshot(user: User) -> dict[str, int]:
    return {
        "common": int(user.credits_balance or 0),
        "photo": int(user.photo_credits_balance or 0),
        "video": int(user.video_credits_balance or 0),
    }


def _affiliate_snapshot(user: User | None) -> tuple[int, int, int]:
    if not user:
        return 0, 0, 0
    return (
        int(user.affiliate_balance_kopecks or 0),
        int(user.affiliate_earned_kopecks or 0),
        int(user.affiliate_debt_kopecks or 0),
    )


def _append_payment_raw_audit(
    payment: Payment,
    *,
    admin: User,
    reason: str,
    action: str,
    before_status: str,
    operation_key: str,
) -> None:
    payload = dict(payment.raw_payload or {})
    events = list(payload.get("admin_audit") or [])
    if any(str(item.get("operation_key")) == operation_key for item in events if isinstance(item, dict)):
        return
    events.append(
        {
            "operation_key": operation_key,
            "action": action,
            "actor_user_id": admin.id,
            "actor_telegram_id": admin.telegram_id,
            "reason": reason,
            "before_status": before_status,
            "after_status": payment.status,
            "at": datetime.now(timezone.utc).isoformat(),
        }
    )
    payment.raw_payload = {**payload, "admin_audit": events[-50:]}


@router.callback_query(F.data.startswith("admin:user:adjust:"))
async def audited_user_adjust_prompt(
    callback: CallbackQuery, context: AppContext, state: FSMContext
) -> None:
    if not await _require_admin_callback(callback, context):
        return
    raw = str(callback.data).removeprefix("admin:user:adjust:")
    action, _, user_id_text = raw.partition(":")
    if action not in {*_BALANCE_FIELDS, "unlimited", "affiliate_rate"} or not user_id_text.isdigit():
        await admin_plugin._safe_answer(callback, "Действие недоступно", show_alert=True)
        return
    await state.set_state(admin_plugin.AdminStates.user_adjust)
    await state.update_data(user_id=int(user_id_text), user_action=action)
    labels = {
        "credits": "универсальных кредитов",
        "photo_credits": "фото-кредитов",
        "video_credits": "видео-кредитов",
        "unlimited": "дней безлимита",
        "affiliate_rate": "процентов партнёрской комиссии",
    }
    if callback.message:
        await callback.message.answer(
            f"Изменение {labels[action]}\n\n"
            "Введите значение и обязательную причину через |.\n"
            "Пример: 20 | компенсация за сбой\n"
            "Отрицательное число уменьшает кредитный баланс. Для безлимита 0 снимает доступ.",
            reply_markup=admin_plugin._cancel_keyboard(f"admin:user:{user_id_text}"),
        )
    await admin_plugin._safe_answer(callback)


@router.message(admin_plugin.AdminStates.user_adjust, F.text)
async def audited_user_adjust_apply(
    message: Message, context: AppContext, state: FSMContext
) -> None:
    admin = await _require_admin_message(message, context)
    if not admin:
        return
    data = await state.get_data()
    user_id = int(data.get("user_id") or 0)
    action = str(data.get("user_action") or "")
    try:
        amount_text, reason = _reasoned_text(message.text or "")
        amount = int(amount_text)
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=admin_plugin._cancel_keyboard(f"admin:user:{user_id}"))
        return

    operation_key = _operation_key(message, action, user_id)
    async with session_scope(context.session_factory) as session:
        user = await session.get(User, user_id, with_for_update=True)
        if not user:
            await message.answer("Пользователь не найден.", reply_markup=admin_plugin._cancel_keyboard("admin:users"))
            return

        if action in _BALANCE_FIELDS:
            field, credit_type = _BALANCE_FIELDS[action]
            before = int(getattr(user, field) or 0)
            after = max(0, before + amount)
            setattr(user, field, after)
            await _append_credit_audit(
                session,
                user=user,
                credit_type=credit_type,
                before=before,
                after=after,
                reason=reason,
                admin=admin,
                operation_key=operation_key,
                reference_type="admin_user",
                reference_id=str(user.id),
            )
            result = f"Баланс обновлён: {before} → {after}. Причина сохранена."
        elif action == "unlimited":
            before = user.unlimited_until
            if amount <= 0:
                user.unlimited_until = None
            else:
                now = datetime.now(timezone.utc)
                base = before if before and before > now else now
                user.unlimited_until = base + timedelta(days=amount)
            await _append_credit_audit(
                session,
                user=user,
                credit_type="unlimited",
                before=0,
                after=0,
                reason=reason,
                admin=admin,
                operation_key=operation_key,
                reference_type="admin_user",
                reference_id=str(user.id),
                metadata={
                    "before_until": before.isoformat() if before else None,
                    "after_until": user.unlimited_until.isoformat() if user.unlimited_until else None,
                },
            )
            result = "Безлимит обновлён. Причина сохранена."
        else:
            if amount < 0 or amount > 100:
                await message.answer("Введите процент от 0 до 100.", reply_markup=admin_plugin._cancel_keyboard(f"admin:user:{user_id}"))
                return
            before = int(user.affiliate_commission_rate_bps or 0)
            after = amount * 100
            user.affiliate_commission_rate_bps = after
            await _append_credit_audit(
                session,
                user=user,
                credit_type="affiliate_rate",
                before=before,
                after=after,
                reason=reason,
                admin=admin,
                operation_key=operation_key,
                reference_type="admin_user",
                reference_id=str(user.id),
                metadata={"unit": "basis_points"},
            )
            result = f"Ставка партнёрки обновлена: {before / 100:.0f}% → {amount}%."

    await state.clear()
    await message.answer(result, reply_markup=admin_plugin._admin_keyboard())
    await admin_plugin._send_user_detail(message, context, user_id)


@router.callback_query(F.data.in_({"admin:users:grant", "adm:add_credits"}))
async def audited_grant_prompt(
    callback: CallbackQuery, context: AppContext, state: FSMContext
) -> None:
    if not await _require_admin_callback(callback, context):
        return
    await state.set_state(admin_plugin.AdminStates.grant_credits)
    if callback.message:
        await callback.message.answer(
            "Начислить универсальные кредиты\n\n"
            "Введите Telegram ID, количество и обязательную причину через |.\n"
            "Пример: 339795159 20 | компенсация за сбой",
            reply_markup=admin_plugin._cancel_keyboard("admin:users"),
        )
    await admin_plugin._safe_answer(callback)


@router.message(admin_plugin.AdminStates.grant_credits, F.text)
async def audited_grant_apply(
    message: Message, context: AppContext, state: FSMContext
) -> None:
    admin = await _require_admin_message(message, context)
    if not admin:
        return
    try:
        payload, reason = _reasoned_text(message.text or "")
        telegram_id_text, amount_text = payload.split(maxsplit=1)
        telegram_id = int(telegram_id_text)
        amount = int(amount_text)
    except ValueError as exc:
        text = str(exc) if "причин" in str(exc).lower() else "Формат: Telegram_ID количество | причина"
        await message.answer(text, reply_markup=admin_plugin._cancel_keyboard("admin:users"))
        return

    operation_key = _operation_key(message, "grant", telegram_id)
    async with session_scope(context.session_factory) as session:
        user = await session.scalar(
            select(User).where(User.telegram_id == telegram_id).with_for_update()
        )
        if not user:
            await message.answer("Пользователь не найден.", reply_markup=admin_plugin._cancel_keyboard("admin:users"))
            return
        before = int(user.credits_balance or 0)
        after = max(0, before + amount)
        user.credits_balance = after
        await _append_credit_audit(
            session,
            user=user,
            credit_type="common",
            before=before,
            after=after,
            reason=reason,
            admin=admin,
            operation_key=operation_key,
            reference_type="admin_user",
            reference_id=str(user.id),
        )

    await state.clear()
    await message.answer(
        f"Готово. Универсальный баланс {telegram_id}: {before} → {after}. Причина сохранена.",
        reply_markup=admin_plugin._admin_keyboard(),
    )


@router.callback_query(F.data.startswith("admin:package:toggle:"))
async def guarded_package_toggle(callback: CallbackQuery, context: AppContext) -> None:
    if not await _require_admin_callback(callback, context):
        return
    package_id_text = str(callback.data).removeprefix("admin:package:toggle:")
    if not package_id_text.isdigit():
        await admin_plugin._safe_answer(callback, "Пакет не найден", show_alert=True)
        return
    package_id = int(package_id_text)
    async with session_scope(context.session_factory) as session:
        package = await session.get(CreditPackage, package_id, with_for_update=True)
        if not package:
            await admin_plugin._safe_answer(callback, "Пакет не найден", show_alert=True)
            return
        if not package.is_enabled and not _package_can_be_enabled(package):
            await admin_plugin._safe_answer(
                callback,
                "Нельзя включить пакет: нужна цена больше 0 и хотя бы один кредит либо безлимит со сроком.",
                show_alert=True,
            )
            return
        package.is_enabled = not package.is_enabled
        enabled = package.is_enabled
        payments_count = await session.scalar(
            select(Payment.id).where(Payment.package_id == package.id).limit(1)
        )
        text = admin_plugin._package_detail_text(package, 1 if payments_count else 0)
        keyboard = admin_plugin._package_keyboard(package.id, package.is_enabled)
    if callback.message:
        await callback.message.answer(text, reply_markup=keyboard)
    await admin_plugin._safe_answer(callback, "Включен" if enabled else "Выключен", show_alert=True)


@router.message(admin_plugin.AdminStates.package_field, F.text)
async def guarded_package_field_apply(
    message: Message, context: AppContext, state: FSMContext
) -> None:
    if not await _require_admin_message(message, context):
        return
    data = await state.get_data()
    package_id = int(data.get("package_id") or 0)
    field = str(data.get("package_field") or "")
    value = (message.text or "").strip()
    async with session_scope(context.session_factory) as session:
        package = await session.get(CreditPackage, package_id, with_for_update=True)
        if not package:
            await message.answer("Пакет не найден.", reply_markup=admin_plugin._cancel_keyboard("admin:packages"))
            return
        candidate = {
            "price_rub": package.price_rub,
            "credits": int(package.credits or 0),
            "photo_credits": int(package.photo_credits or 0),
            "video_credits": int(package.video_credits or 0),
            "is_unlimited": bool(package.is_unlimited),
            "duration_days": package.duration_days,
        }
        try:
            if field == "title":
                if not value:
                    raise ValueError("Название не может быть пустым.")
                package.title = value
            elif field == "description":
                package.description = value or None
            elif field == "terms":
                package.terms = None if value == "-" else (value or None)
            elif field in {"credits", "photo_credits", "video_credits"}:
                amount = int(value)
                if amount < 0:
                    raise ValueError("Кредиты не могут быть отрицательными.")
                candidate[field] = amount
            elif field == "price":
                price = Decimal(value.replace(",", "."))
                if price < 0:
                    raise ValueError("Цена не может быть отрицательной.")
                candidate["price_rub"] = price
            elif field == "duration":
                duration = int(value)
                if duration < 0:
                    raise ValueError("Срок не может быть отрицательным.")
                candidate["duration_days"] = duration or None
            elif field == "unlimited":
                candidate["is_unlimited"] = admin_plugin._parse_bool(value)
            elif field == "position":
                package.position = int(value)
            else:
                raise ValueError("Поле недоступно.")

            if package.is_enabled and not _sale_values_valid(**candidate):
                raise ValueError(
                    "Включённый пакет должен иметь цену больше 0 и давать кредиты либо безлимит со сроком. Сначала выключите пакет."
                )

            if field in {"credits", "photo_credits", "video_credits"}:
                setattr(package, field, candidate[field])
            elif field == "price":
                package.price_rub = candidate["price_rub"]
            elif field == "duration":
                package.duration_days = candidate["duration_days"]
            elif field == "unlimited":
                package.is_unlimited = candidate["is_unlimited"]
        except (ValueError, InvalidOperation, ArithmeticError) as exc:
            await message.answer(str(exc), reply_markup=admin_plugin._cancel_keyboard(f"admin:package:{package_id}"))
            return

    await state.clear()
    await message.answer("Пакет обновлён.", reply_markup=admin_plugin._admin_keyboard())


def _audited_payment_keyboard(payment_id: int, status: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    rows = 0
    if status == "manual_pending":
        builder.button(text="Подтвердить оплату", callback_data=f"admin:payment:paid:{payment_id}")
        rows += 1
    if status == "paid":
        builder.button(text="Сторнировать платёж", callback_data=f"admin:payment:reverse:{payment_id}")
        rows += 1
    elif status not in {"cancelled", "reversed"}:
        builder.button(text="Отменить", callback_data=f"admin:payment:cancel:{payment_id}")
        rows += 1
    nav_count = add_navigation_buttons(builder, back_callback="admin:payments")
    builder.adjust(*([1] * rows), nav_count)
    return builder.as_markup()


def _audited_payment_detail_text(payment: Payment, package: CreditPackage | None, user: User | None) -> str:
    base = _ORIGINAL_PAYMENT_DETAIL_TEXT(payment, package, user)
    snapshot = payment_package_snapshot(payment) or {}
    snapshot_text = (
        f"\n\nПродано по снимку:\n"
        f"Фото: <b>{int(snapshot.get('photo_credits') or 0)}</b> · "
        f"Видео: <b>{int(snapshot.get('video_credits') or 0)}</b> · "
        f"Универсальные: <b>{int(snapshot.get('credits') or 0)}</b>"
    ) if snapshot else ""
    reversal = (
        f"\nСторно: <b>{payment.reversed_at:%Y-%m-%d %H:%M}</b>\n"
        f"Причина: {escape(payment.reversal_reason or '-')}"
        if payment.reversed_at
        else ""
    )
    audit_count = len(list(dict(payment.raw_payload or {}).get("admin_audit") or []))
    return f"{base}{snapshot_text}{reversal}\nАдмин-аудит: <b>{audit_count}</b>"


@router.callback_query(F.data == "admin:payments:mark_paid")
async def audited_payment_mark_prompt(
    callback: CallbackQuery, context: AppContext, state: FSMContext
) -> None:
    if not await _require_admin_callback(callback, context):
        return
    await state.set_state(admin_plugin.AdminStates.payment_mark_paid)
    if callback.message:
        await callback.message.answer(
            "Подтвердить платёж вручную\n\nВведите order_id и обязательную причину через |.\n"
            "Пример: stupidbot-1-abc123 | перевод проверен в банке",
            reply_markup=admin_plugin._cancel_keyboard("admin:payments"),
        )
    await admin_plugin._safe_answer(callback)


async def _execute_payment_action(
    *,
    session,
    payment: Payment,
    action: str,
    reason: str,
    admin: User,
    operation_key: str,
) -> tuple[bool, str, int | None, str | None]:
    before_status = str(payment.status)
    buyer = await session.get(User, payment.user_id, with_for_update=True)
    before_credits = _credit_snapshot(buyer) if buyer else {"common": 0, "photo": 0, "video": 0}
    referrer = (
        await session.get(User, payment.affiliate_commission_user_id, with_for_update=True)
        if payment.affiliate_commission_user_id
        else None
    )
    before_affiliate = _affiliate_snapshot(referrer)

    notify_chat_id: int | None = None
    notify_text: str | None = None
    if action == "paid":
        result = await admin_plugin._mark_payment_paid(session, payment)
        if not result.ok:
            return False, result.admin_text, None, None
        notify_chat_id = result.notify_chat_id
        notify_text = result.notify_text
        message = result.admin_text
    elif action == "cancel":
        if payment.status in {"paid", "reversed"}:
            return False, "Оплаченный или сторнированный платёж нельзя просто отменить.", None, None
        if payment.status == "cancelled":
            return False, "Платёж уже отменён.", None, None
        payment.status = "cancelled"
        message = "Платёж отменён."
    elif action == "reverse":
        ok, debts = await reverse_paid_payment(session, payment=payment, reason=reason)
        if not ok:
            return False, "Сторно недоступно или уже выполнено.", None, None
        debt_text = ", ".join(f"{key}: {value}" for key, value in debts.items() if value) or "без долгов"
        message = f"Платёж сторнирован ({debt_text})."
        if buyer:
            notify_chat_id = buyer.telegram_id
            notify_text = (
                "Платёж сторнирован администратором.\n\n"
                f"Причина: <b>{escape(reason)}</b>\n"
                "Начисления по этому платежу отозваны. Если часть кредитов уже потрачена, остаток учтён как долг."
            )
    else:
        return False, "Неизвестное действие.", None, None

    if buyer:
        after_credits = _credit_snapshot(buyer)
        for credit_type in ("common", "photo", "video"):
            if before_credits[credit_type] != after_credits[credit_type]:
                await _append_credit_audit(
                    session,
                    user=buyer,
                    credit_type=credit_type,
                    before=before_credits[credit_type],
                    after=after_credits[credit_type],
                    reason=reason,
                    admin=admin,
                    operation_key=operation_key,
                    reference_type="payment",
                    reference_id=str(payment.id),
                    metadata={"payment_action": action},
                )

    referrer = (
        await session.get(User, payment.affiliate_commission_user_id, with_for_update=True)
        if payment.affiliate_commission_user_id
        else None
    )
    after_affiliate = _affiliate_snapshot(referrer)
    if referrer and before_affiliate != after_affiliate:
        await _append_affiliate_audit(
            session,
            user=referrer,
            before_balance=before_affiliate[0],
            after_balance=after_affiliate[0],
            before_earned=before_affiliate[1],
            after_earned=after_affiliate[1],
            before_debt=before_affiliate[2],
            after_debt=after_affiliate[2],
            reason=reason,
            admin=admin,
            operation_key=operation_key,
            reference_id=str(payment.id),
        )

    _append_payment_raw_audit(
        payment,
        admin=admin,
        reason=reason,
        action=action,
        before_status=before_status,
        operation_key=operation_key,
    )
    return True, message, notify_chat_id, notify_text


@router.message(admin_plugin.AdminStates.payment_mark_paid, F.text)
async def audited_payment_mark_apply(
    message: Message, context: AppContext, state: FSMContext, bot: Bot
) -> None:
    admin = await _require_admin_message(message, context)
    if not admin:
        return
    try:
        order_id, reason = _reasoned_text(message.text or "")
    except ValueError as exc:
        await message.answer(str(exc), reply_markup=admin_plugin._cancel_keyboard("admin:payments"))
        return
    operation_key = _operation_key(message, "payment_paid", order_id)
    async with session_scope(context.session_factory) as session:
        payment = await session.scalar(
            select(Payment).where(Payment.order_id == order_id).with_for_update()
        )
        if not payment:
            await message.answer("Платёж не найден.", reply_markup=admin_plugin._cancel_keyboard("admin:payments"))
            return
        ok, result, notify_chat_id, notify_text = await _execute_payment_action(
            session=session,
            payment=payment,
            action="paid",
            reason=reason,
            admin=admin,
            operation_key=operation_key,
        )
    await state.clear()
    if ok and notify_chat_id and notify_text:
        await bot.send_message(notify_chat_id, notify_text)
    await message.answer(result, reply_markup=admin_plugin._admin_keyboard())


@router.callback_query(
    F.data.func(
        lambda data: bool(
            data
            and any(
                str(data).startswith(prefix)
                for prefix in (
                    "admin:payment:paid:",
                    "admin:payment:cancel:",
                    "admin:payment:reverse:",
                )
            )
        )
    )
)
async def payment_action_reason_prompt(
    callback: CallbackQuery, context: AppContext, state: FSMContext
) -> None:
    if not await _require_admin_callback(callback, context):
        return
    raw = str(callback.data).removeprefix("admin:payment:")
    action, _, payment_id_text = raw.partition(":")
    if action not in {"paid", "cancel", "reverse"} or not payment_id_text.isdigit():
        await admin_plugin._safe_answer(callback, "Платёж не найден", show_alert=True)
        return
    await state.set_state(FinancialAdminStates.payment_action_reason)
    await state.update_data(payment_id=int(payment_id_text), payment_action=action)
    labels = {"paid": "подтверждения", "cancel": "отмены", "reverse": "сторно"}
    if callback.message:
        await callback.message.answer(
            f"Причина {labels[action]} платежа\n\nВведите короткую причину. Она сохранится в аудите.",
            reply_markup=admin_plugin._cancel_keyboard(f"admin:payment:{payment_id_text}"),
        )
    await admin_plugin._safe_answer(callback)


@router.message(FinancialAdminStates.payment_action_reason, F.text)
async def payment_action_reason_apply(
    message: Message, context: AppContext, state: FSMContext, bot: Bot
) -> None:
    admin = await _require_admin_message(message, context)
    if not admin:
        return
    reason = (message.text or "").strip()
    if len(reason) < 3:
        await message.answer("Причина должна содержать минимум 3 символа.")
        return
    data = await state.get_data()
    payment_id = int(data.get("payment_id") or 0)
    action = str(data.get("payment_action") or "")
    operation_key = _operation_key(message, f"payment_{action}", payment_id)
    async with session_scope(context.session_factory) as session:
        payment = await session.get(Payment, payment_id, with_for_update=True)
        if not payment:
            await message.answer("Платёж не найден.")
            return
        ok, result, notify_chat_id, notify_text = await _execute_payment_action(
            session=session,
            payment=payment,
            action=action,
            reason=reason,
            admin=admin,
            operation_key=operation_key,
        )
    await state.clear()
    if ok and notify_chat_id and notify_text:
        await bot.send_message(notify_chat_id, notify_text)
    await message.answer(result, reply_markup=admin_plugin._admin_keyboard())


_ORIGINAL_PAYMENT_DETAIL_TEXT = admin_plugin._payment_detail_text


def setup(dispatcher: Dispatcher, context: AppContext) -> None:
    del context
    admin_plugin._payment_keyboard = _audited_payment_keyboard
    admin_plugin._payment_detail_text = _audited_payment_detail_text
    dispatcher.include_router(router)
