from __future__ import annotations

import logging
from contextlib import suppress
from decimal import Decimal
from typing import Any

from sqlalchemy import select

from app.db import session_scope
from app.models import Payment, User
from app.services.financial_credits import reverse_paid_payment
from app.ui import navigation_keyboard

logger = logging.getLogger(__name__)
FULL_REVERSAL_STATUSES = {"REFUNDED", "REVERSED", "CHARGEBACK"}


def install_payment_patches() -> None:
    from app.services import payments

    if getattr(payments, "_financial_patches_installed", False):
        return
    original_handler = payments.handle_tbank_notification

    async def disabled_custom_credit_payment(*args: Any, **kwargs: Any):
        raise payments.PaymentCreditAmountInvalid("custom_credit_sales_disabled")

    async def reversal_aware_handler(context: Any, payload: dict[str, Any]) -> bool:
        status = str(payload.get("Status") or "").upper()
        if status not in FULL_REVERSAL_STATUSES:
            return await original_handler(context, payload)
        return await _handle_full_reversal(context, payments, payload, status)

    # Main imports these values after app.bot installs repository/payment patches.
    # Zero values keep the legacy response shape harmless for old Mini App clients,
    # while all custom-credit creation paths remain rejected server-side.
    payments.CUSTOM_CREDIT_PRICE_RUB = Decimal("0")
    payments.CUSTOM_CREDIT_MIN_AMOUNT = 0
    payments.CUSTOM_CREDIT_MAX_AMOUNT = 0
    payments.create_custom_credit_payment = disabled_custom_credit_payment
    payments.handle_tbank_notification = reversal_aware_handler
    payments._financial_patches_installed = True


async def _handle_full_reversal(
    context: Any,
    payments_module: Any,
    payload: dict[str, Any],
    status: str,
) -> bool:
    if not context.tbank.verify_notification(payload):
        logger.warning("payment_reversal_invalid_signature")
        return False
    order_id = str(payload.get("OrderId") or "")
    if not order_id:
        return False

    notify_chat_id: int | None = None
    notify_text: str | None = None
    async with session_scope(context.session_factory) as session:
        payment = await session.scalar(
            select(Payment).where(Payment.order_id == order_id).with_for_update()
        )
        if not payment:
            return False
        payment.raw_payload = {**dict(payment.raw_payload or {}), "reversal_callback": payload}
        validation_error = payments_module._validate_notification(context, payment, payload)
        if validation_error:
            payment.raw_payload = {
                **dict(payment.raw_payload or {}),
                "_reversal_validation_error": validation_error,
            }
            return True
        if payment.status == "reversed":
            return True
        if payment.status != "paid":
            payment.status = status.lower()
            return True

        changed, debts = await reverse_paid_payment(
            session,
            payment=payment,
            reason=f"tbank:{status.lower()}",
        )
        if not changed:
            return True
        user = await session.get(User, payment.user_id)
        if user:
            notify_chat_id = user.telegram_id
            debt_total = sum(
                int(value or 0)
                for key, value in debts.items()
                if key != "affiliate_kopecks"
            )
            notify_text = (
                "Платеж отменен банком. Начисление сторнировано."
                + (
                    f"\n\nЧасть кредитов уже была использована. Долг: <b>{debt_total}</b> "
                    "кредитов; будущие начисления сначала погасят его."
                    if debt_total
                    else ""
                )
            )

    if context.bot and notify_chat_id and notify_text:
        with suppress(Exception):
            await context.bot.send_message(
                notify_chat_id,
                notify_text,
                reply_markup=navigation_keyboard(),
            )
    return True
