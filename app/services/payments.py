from __future__ import annotations

import logging
import time
from contextlib import suppress
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP
from html import escape
from typing import Any
from uuid import uuid4

from sqlalchemy import select

from app.context import AppContext
from app.db import session_scope
from app.models import CreditPackage, Payment, User
from app.repositories import (
    apply_affiliate_commission,
    apply_package_snapshot_to_user,
    apply_package_to_user,
    credit_package_snapshot,
    package_is_user_visible,
    payment_package_snapshot,
)
from app.ui import navigation_keyboard

PAID_STATUSES = {"CONFIRMED"}
FAILED_STATUSES = {"REJECTED", "CANCELED", "DEADLINE_EXPIRED", "AUTH_FAIL"}
CUSTOM_CREDIT_PRICE_RUB = Decimal("1.00")
CUSTOM_CREDIT_MIN_AMOUNT = 1
CUSTOM_CREDIT_MAX_AMOUNT = 100_000
logger = logging.getLogger(__name__)


class PaymentCreationError(RuntimeError):
    pass


class PaymentPackageUnavailable(PaymentCreationError):
    pass


class PaymentCreditAmountInvalid(PaymentCreationError):
    pass


class PaymentProviderError(PaymentCreationError):
    pass


@dataclass(slots=True)
class PackagePaymentInit:
    payment_id: int
    order_id: str
    status: str
    payment_url: str | None
    package_snapshot: dict[str, Any]
    amount_kopecks: int


async def create_package_payment(
    context: AppContext,
    *,
    user_id: int,
    package_id: int,
    customer_key: str | None = None,
    source: str = "bot",
) -> PackagePaymentInit:
    package_unavailable = False
    payment_id = 0
    order_id = ""
    package_snapshot: dict[str, Any] = {}
    amount_kopecks = 0
    telegram_id = 0
    package_title = ""

    async with session_scope(context.session_factory) as session:
        user = await session.get(User, user_id)
        package = await session.get(CreditPackage, package_id)
        if not user or not package or not package_is_user_visible(package):
            package_unavailable = True
        else:
            amount_kopecks = _amount_kopecks(package.price_rub)
            package_snapshot = credit_package_snapshot(package)
            if amount_kopecks <= 0 or not _package_grants_value(package_snapshot):
                package_unavailable = True
            else:
                order_id = f"stupidbot-{user.id}-{uuid4().hex[:12]}"
                payment = Payment(
                    user_id=user.id,
                    package_id=package.id,
                    order_id=order_id,
                    amount_kopecks=amount_kopecks,
                    status="created",
                    raw_payload={
                        "package_snapshot": package_snapshot,
                        "source": source,
                    },
                )
                session.add(payment)
                await session.flush()
                payment_id = payment.id
                telegram_id = user.telegram_id
                package_title = package.title

    if package_unavailable:
        raise PaymentPackageUnavailable("package_unavailable")

    return await _finish_payment_init(
        context,
        payment_id=payment_id,
        order_id=order_id,
        package_snapshot=package_snapshot,
        amount_kopecks=amount_kopecks,
        telegram_id=telegram_id,
        package_title=package_title,
        customer_key=customer_key,
    )


async def create_custom_credit_payment(
    context: AppContext,
    *,
    user_id: int,
    credits: int,
    customer_key: str | None = None,
    source: str = "bot",
) -> PackagePaymentInit:
    credits = _normalize_custom_credit_amount(credits)
    package_snapshot = custom_credit_package_snapshot(credits)
    amount_kopecks = _amount_kopecks(package_snapshot["price_rub"])
    order_id = ""
    payment_id = 0
    telegram_id = 0
    user_missing = False

    async with session_scope(context.session_factory) as session:
        user = await session.get(User, user_id)
        if not user:
            user_missing = True
        else:
            order_id = f"stupidbot-{user.id}-{uuid4().hex[:12]}"
            payment = Payment(
                user_id=user.id,
                package_id=None,
                order_id=order_id,
                amount_kopecks=amount_kopecks,
                status="created",
                raw_payload={
                    "package_snapshot": package_snapshot,
                    "source": source,
                },
            )
            session.add(payment)
            await session.flush()
            payment_id = payment.id
            telegram_id = user.telegram_id

    if user_missing:
        raise PaymentPackageUnavailable("user_unavailable")

    return await _finish_payment_init(
        context,
        payment_id=payment_id,
        order_id=order_id,
        package_snapshot=package_snapshot,
        amount_kopecks=amount_kopecks,
        telegram_id=telegram_id,
        package_title=str(package_snapshot["title"]),
        customer_key=customer_key,
    )


async def _finish_payment_init(
    context: AppContext,
    *,
    payment_id: int,
    order_id: str,
    package_snapshot: dict[str, Any],
    amount_kopecks: int,
    telegram_id: int,
    package_title: str,
    customer_key: str | None = None,
) -> PackagePaymentInit:
    if not context.tbank.is_configured:
        async with session_scope(context.session_factory) as session:
            payment = await session.get(Payment, payment_id, with_for_update=True)
            if payment:
                payment.status = "manual_pending"
        return PackagePaymentInit(
            payment_id=payment_id,
            order_id=order_id,
            status="manual_pending",
            payment_url=None,
            package_snapshot=package_snapshot,
            amount_kopecks=amount_kopecks,
        )

    started = time.monotonic()
    logger.info("payment_provider_request_start payment_id=%s amount_kopecks=%s", payment_id, amount_kopecks)
    try:
        result = await context.tbank.init_payment(
            order_id=order_id,
            amount_kopecks=amount_kopecks,
            description=package_title,
            notification_url=context.settings.tbank_callback_url,
            customer_key=customer_key or str(telegram_id),
        )
    except Exception as exc:
        logger.exception("payment_provider_request_failed payment_id=%s duration_ms=%d", payment_id, int((time.monotonic() - started) * 1000))
        async with session_scope(context.session_factory) as session:
            payment = await session.get(Payment, payment_id, with_for_update=True)
            if payment:
                payment.status = "failed"
                payment.raw_payload = {**dict(payment.raw_payload or {}), "error": str(exc)}
        raise PaymentProviderError(str(exc)) from exc

    logger.info("payment_provider_request_ok payment_id=%s duration_ms=%d", payment_id, int((time.monotonic() - started) * 1000))
    payment_url = result.get("PaymentURL")
    async with session_scope(context.session_factory) as session:
        payment = await session.get(Payment, payment_id, with_for_update=True)
        if payment:
            payment.provider_payment_id = str(result.get("PaymentId") or "")
            payment.payment_url = payment_url
            payment.raw_payload = {
                **dict(payment.raw_payload or {}),
                "provider_init": result,
            }

    return PackagePaymentInit(
        payment_id=payment_id,
        order_id=order_id,
        status="created",
        payment_url=str(payment_url) if payment_url else None,
        package_snapshot=package_snapshot,
        amount_kopecks=amount_kopecks,
    )


async def handle_tbank_notification(context: AppContext, payload: dict[str, Any]) -> bool:
    logger.info("payment_callback_received order_present=%s status=%s", bool(payload.get("OrderId")), payload.get("Status"))
    if not context.tbank.verify_notification(payload):
        logger.warning("payment_callback_invalid_signature")
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
        payment.raw_payload = {**dict(payment.raw_payload or {}), "callback": payload}

        validation_error = _validate_notification(context, payment, payload)
        if validation_error:
            payment.status = "invalid_callback"
            payment.raw_payload = {
                **dict(payment.raw_payload or {}),
                "_validation_error": validation_error,
            }
            return True

        provider_payment_id = str(payload.get("PaymentId") or "")
        if provider_payment_id:
            payment.provider_payment_id = provider_payment_id

        if payment.status == "paid":
            return True

        status = str(payload.get("Status") or "")
        success = _is_success(payload.get("Success"))
        if success and status in PAID_STATUSES and payment.status != "paid":
            snapshot = payment_package_snapshot(payment)
            package = (
                await session.get(CreditPackage, payment.package_id) if payment.package_id else None
            )
            user = await session.get(User, payment.user_id, with_for_update=True)
            if user and snapshot:
                await apply_package_snapshot_to_user(session, user=user, snapshot=snapshot)
                await apply_affiliate_commission(session, payment=payment, buyer=user)
                payment.status = "paid"
                notify_chat_id = user.telegram_id
                notify_text = _paid_notification_text(snapshot, payment, user)
            elif package and user:
                await apply_package_to_user(session, user=user, package=package)
                await apply_affiliate_commission(session, payment=payment, buyer=user)
                payment.status = "paid"
                notify_chat_id = user.telegram_id
                notify_text = _paid_notification_text(package, payment, user)
            else:
                payment.status = "invalid_callback"
        elif status:
            payment.status = status.lower()
            if status in FAILED_STATUSES:
                user = await session.get(User, payment.user_id)
                package = (
                    await session.get(CreditPackage, payment.package_id)
                    if payment.package_id
                    else None
                )
                if user:
                    notify_chat_id = user.telegram_id
                    notify_text = _failed_notification_text(
                        payment_package_snapshot(payment) or package, payment, status
                    )
    if context.bot and notify_chat_id and notify_text:
        with suppress(Exception):
            await context.bot.send_message(
                notify_chat_id,
                notify_text,
                reply_markup=navigation_keyboard(),
            )
    return True


def _validate_notification(
    context: AppContext, payment: Payment, payload: dict[str, Any]
) -> str | None:
    terminal_key = str(payload.get("TerminalKey") or "")
    if context.tbank.terminal_key and terminal_key != context.tbank.terminal_key:
        return "terminal_key_mismatch"

    try:
        amount = int(payload.get("Amount"))
    except (TypeError, ValueError):
        return "invalid_amount"
    if amount != payment.amount_kopecks:
        return "amount_mismatch"

    provider_payment_id = str(payload.get("PaymentId") or "")
    if payment.provider_payment_id and provider_payment_id != payment.provider_payment_id:
        return "payment_id_mismatch"

    error_code = str(payload.get("ErrorCode") or "0")
    if _is_success(payload.get("Success")) and error_code not in {"0", ""}:
        return "success_with_error_code"
    return None


def _is_success(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() == "true"
    return False


def _paid_notification_text(
    package: CreditPackage | dict[str, Any], payment: Payment, user: User
) -> str:
    if _package_is_unlimited(package) and user.unlimited_until:
        balance = (
            f"Безлимит активен до {user.unlimited_until:%Y-%m-%d %H:%M}.\n{_balance_text(user)}"
        )
    else:
        balance = _balance_text(user)
    return (
        "Оплата прошла.\n\n"
        f"Пакет: <b>{escape(_package_title(package))}</b>\n"
        f"Сумма: <b>{_format_kopecks(payment.amount_kopecks)}</b>\n"
        f"{balance}\n\n"
        "Можно запускать генерации."
    )


def _balance_text(user: User) -> str:
    return (
        "Баланс:\n"
        f"Фото: <b>{int(user.photo_credits_balance or 0)}</b>\n"
        f"Видео: <b>{int(user.video_credits_balance or 0)}</b>\n"
        f"Универсальные: <b>{int(user.credits_balance or 0)}</b>"
    )


def _failed_notification_text(
    package: CreditPackage | dict[str, Any] | None, payment: Payment, status: str
) -> str:
    package_title = escape(_package_title(package)) if package else "пакет"
    return (
        "Оплата не завершена.\n\n"
        f"Пакет: <b>{package_title}</b>\n"
        f"Сумма: <b>{_format_kopecks(payment.amount_kopecks)}</b>\n"
        f"Статус: <b>{escape(status.lower())}</b>\n\n"
        "Бананы не списывались и не начислялись."
    )


def _format_kopecks(amount_kopecks: int) -> str:
    return f"{amount_kopecks / 100:.0f} ₽"


def _amount_kopecks(price_rub: object) -> int:
    amount = (Decimal(str(price_rub)) * Decimal("100")).quantize(
        Decimal("1"),
        rounding=ROUND_HALF_UP,
    )
    return int(amount)


def custom_credit_package_snapshot(credits: int) -> dict[str, Any]:
    credits = _normalize_custom_credit_amount(credits)
    price_rub = CUSTOM_CREDIT_PRICE_RUB * Decimal(credits)
    return {
        "package_id": None,
        "code": "custom_credits",
        "title": _universal_credit_title(credits),
        "description": "Покупка произвольного количества универсальных кредитов.",
        "terms": f"Курс: 1 универсальный кредит = {_format_decimal_rub(CUSTOM_CREDIT_PRICE_RUB)} ₽.",
        "credits": credits,
        "photo_credits": 0,
        "video_credits": 0,
        "price_rub": str(price_rub),
        "is_unlimited": False,
        "duration_days": None,
        "custom_credits": True,
        "rate_rub_per_credit": str(CUSTOM_CREDIT_PRICE_RUB),
    }


def _normalize_custom_credit_amount(value: int) -> int:
    try:
        credits = int(value)
    except (TypeError, ValueError):
        raise PaymentCreditAmountInvalid("invalid_credit_amount") from None
    if credits < CUSTOM_CREDIT_MIN_AMOUNT or credits > CUSTOM_CREDIT_MAX_AMOUNT:
        raise PaymentCreditAmountInvalid("invalid_credit_amount")
    return credits


def _format_decimal_rub(value: Decimal) -> str:
    return f"{value:.0f}" if value == value.to_integral_value() else f"{value:.2f}"


def _universal_credit_title(value: int) -> str:
    value = int(value)
    tail = abs(value) % 100
    last = abs(value) % 10
    if tail in {11, 12, 13, 14}:
        word = "универсальных кредитов"
    elif last == 1:
        word = "универсальный кредит"
    elif last in {2, 3, 4}:
        word = "универсальных кредита"
    else:
        word = "универсальных кредитов"
    return f"{value} {word}"


def _package_grants_value(package: CreditPackage | dict[str, Any]) -> bool:
    if isinstance(package, dict):
        credits = sum(
            _positive_int(package.get(field))
            for field in ("credits", "photo_credits", "video_credits")
        )
        duration_days = _positive_int(package.get("duration_days"))
        return credits > 0 or (bool(package.get("is_unlimited")) and duration_days > 0)

    credits = (
        int(package.credits or 0)
        + int(package.photo_credits or 0)
        + int(package.video_credits or 0)
    )
    duration_days = int(package.duration_days or 0)
    return credits > 0 or (bool(package.is_unlimited) and duration_days > 0)


def _positive_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _package_title(package: CreditPackage | dict[str, Any] | None) -> str:
    if isinstance(package, dict):
        return str(package.get("title") or "пакет")
    if package:
        return package.title
    return "пакет"


def _package_is_unlimited(package: CreditPackage | dict[str, Any]) -> bool:
    if isinstance(package, dict):
        return bool(package.get("is_unlimited"))
    return bool(package.is_unlimited)
