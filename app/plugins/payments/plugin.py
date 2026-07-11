from __future__ import annotations

from html import escape
import logging

from aiogram import Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from app.context import AppContext
from app.db import session_scope
from app.models import CreditPackage, Payment
from app.plugins.common import ensure_user_for_callback, ensure_user_for_message
from app.repositories import list_packages
from app.services.payments import (
    CUSTOM_CREDIT_MAX_AMOUNT,
    CUSTOM_CREDIT_MIN_AMOUNT,
    CUSTOM_CREDIT_PRICE_RUB,
    PackagePaymentInit,
    PaymentCreditAmountInvalid,
    PaymentPackageUnavailable,
    PaymentProviderError,
    create_custom_credit_payment,
    create_package_payment,
)
from app.ui import add_navigation_buttons, navigation_keyboard, package_credits_text, packages_keyboard

router = Router(name="payments")
logger = logging.getLogger(__name__)


class PaymentStates(StatesGroup):
    custom_credits = State()


@router.message(F.text == "Пакеты")
@router.message(Command("packages"))
async def packages(message: Message, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    await ensure_user_for_message(message, context)
    await _send_packages(message, context)


@router.callback_query(F.data == "menu:packages")
async def packages_callback(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    await ensure_user_for_callback(callback, context)
    if callback.message:
        await _send_packages(callback.message, context)
    await callback.answer()


async def _send_packages(message: Message, context: AppContext) -> None:
    async with session_scope(context.session_factory) as session:
        items = await list_packages(session, only_enabled=True)
    await message.answer(_packages_text(items), reply_markup=packages_keyboard(items))


@router.callback_query(F.data.startswith("pay:package:"))
async def package_selected(callback: CallbackQuery, context: AppContext) -> None:
    user = await ensure_user_for_callback(callback, context)
    try:
        package_id = int(callback.data.removeprefix("pay:package:"))
    except ValueError:
        await callback.answer("Пакет недоступен", show_alert=True)
        return
    await callback.answer("Создаю ссылку на оплату...")
    try:
        result = await create_package_payment(
            context,
            user_id=user.id,
            package_id=package_id,
            customer_key=str(user.telegram_id),
            source="bot",
        )
    except PaymentPackageUnavailable:
        if callback.message:
            await callback.message.answer(
                "Пакет недоступен. Откройте список пакетов и выберите актуальный вариант.",
                reply_markup=navigation_keyboard(back_callback="menu:packages"),
            )
        return
    except PaymentProviderError:
        logger.exception("Payment creation failed")
        if callback.message:
            await callback.message.answer(
                "Не получилось создать ссылку на оплату. Попробуйте позже.",
                reply_markup=navigation_keyboard(back_callback="menu:packages"),
            )
        return

    if not callback.message:
        return
    await _send_payment_result(callback.message, result)


@router.callback_query(F.data == "pay:custom")
async def custom_credits_prompt(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await ensure_user_for_callback(callback, context)
    await state.set_state(PaymentStates.custom_credits)
    if callback.message:
        await callback.message.answer(
            "Введите количество универсальных кредитов для покупки.\n\n"
            f"Курс: 1 кредит = {_format_price(CUSTOM_CREDIT_PRICE_RUB)}.\n"
            f"Можно купить от {CUSTOM_CREDIT_MIN_AMOUNT} до {CUSTOM_CREDIT_MAX_AMOUNT} кредитов.",
            reply_markup=navigation_keyboard(back_callback="menu:packages"),
        )
    await callback.answer()


@router.message(PaymentStates.custom_credits, F.text)
async def custom_credits_apply(message: Message, context: AppContext, state: FSMContext) -> None:
    user = await ensure_user_for_message(message, context)
    raw_value = message.text.strip().replace(" ", "")
    try:
        credits = int(raw_value)
    except ValueError:
        await message.answer(
            "Введите целое количество кредитов, например 25.",
            reply_markup=navigation_keyboard(back_callback="menu:packages"),
        )
        return

    try:
        result = await create_custom_credit_payment(
            context,
            user_id=user.id,
            credits=credits,
            customer_key=str(user.telegram_id),
            source="bot",
        )
    except PaymentCreditAmountInvalid:
        await message.answer(
            f"Количество должно быть от {CUSTOM_CREDIT_MIN_AMOUNT} до {CUSTOM_CREDIT_MAX_AMOUNT}.",
            reply_markup=navigation_keyboard(back_callback="menu:packages"),
        )
        return
    except PaymentProviderError:
        logger.exception("Custom credit payment creation failed")
        await message.answer(
            "Не получилось создать ссылку на оплату. Попробуйте позже.",
            reply_markup=navigation_keyboard(back_callback="menu:packages"),
        )
        await state.clear()
        return
    except PaymentPackageUnavailable:
        await message.answer(
            "Не получилось создать заявку. Откройте раздел «Пакеты» и попробуйте еще раз.",
            reply_markup=navigation_keyboard(back_callback="menu:packages"),
        )
        await state.clear()
        return

    await state.clear()
    await _send_payment_result(message, result)


@router.message(F.text == "Оплаты")
async def payments_alias(message: Message, context: AppContext, state: FSMContext) -> None:
    await packages(message, context, state)


async def find_payment_by_order(context: AppContext, order_id: str) -> Payment | None:
    async with session_scope(context.session_factory) as session:
        return await session.scalar(select(Payment).where(Payment.order_id == order_id))


def setup(dispatcher: Dispatcher, context: AppContext) -> None:
    dispatcher.include_router(router)


def _payment_keyboard(payment_url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Открыть оплату", url=payment_url)
    nav_count = add_navigation_buttons(builder, back_callback="menu:packages")
    builder.adjust(1, nav_count)
    return builder.as_markup()


def _packages_text(packages: list[CreditPackage]) -> str:
    lines = [
        "Пополнение кредитов:",
        "",
        f"Свое количество: 1 универсальный кредит = {_format_price(CUSTOM_CREDIT_PRICE_RUB)}.",
    ]
    if packages:
        lines.append("")
        lines.append("Готовые пакеты:")
    else:
        lines.append("Готовые пакеты пока не настроены.")
    for package in packages:
        lines.append("")
        lines.append(_package_summary_text(package))
    return "\n".join(lines)


async def _send_payment_result(message: Message, result: PackagePaymentInit) -> None:
    if result.status == "manual_pending":
        await message.answer(
            "Онлайн-оплата пока не настроена. Заявка создана, администратор сможет отметить ее "
            f"оплаченной в админке.\n\n{_payment_result_text(result)}",
            reply_markup=navigation_keyboard(back_callback="menu:packages"),
        )
        return
    if result.payment_url:
        await message.answer(
            f"{_payment_result_text(result)}\n\n"
            "После оплаты я пришлю сюда уведомление и обновленный баланс.",
            reply_markup=_payment_keyboard(result.payment_url),
        )
        return
    await message.answer(
        "Платеж создан, но платежная ссылка не вернулась.",
        reply_markup=navigation_keyboard(back_callback="menu:packages"),
    )


def _package_summary_text(package: CreditPackage) -> str:
    description = str(package.description or "").strip()
    description_text = f"\nОписание: {escape(description)}" if description else ""
    return (
        f"<b>{escape(package.title)}</b>\n"
        f"Что входит: <b>{escape(_package_amount_text(package))}</b>\n"
        f"Цена: <b>{_format_price(package.price_rub)}</b>"
        f"{description_text}\n"
        f"Условия: {escape(_package_terms_text(package))}"
    )


def _payment_result_text(result: PackagePaymentInit) -> str:
    snapshot = result.package_snapshot
    return (
        f"Пакет: <b>{escape(str(snapshot.get('title') or 'пакет'))}</b>\n"
        f"Начисление: <b>{escape(_snapshot_amount_text(snapshot))}</b>\n"
        f"Сумма: <b>{_format_price_from_kopecks(result.amount_kopecks)}</b>\n"
        f"Заявка: <b>№{result.payment_id}</b>"
        f"\nУсловия: {escape(_snapshot_terms_text(snapshot))}"
    )


def _package_amount_text(package: CreditPackage) -> str:
    return package_credits_text(package)


def _package_terms_text(package: CreditPackage) -> str:
    terms = str(package.terms or "").strip()
    if terms:
        return terms
    if package.is_unlimited:
        days = int(package.duration_days or 0)
        if days > 0:
            return f"Безлимит действует {days} д. с момента подтверждения оплаты."
        return "Безлимит активируется после подтверждения оплаты."
    return "Кредиты зачисляются на баланс сразу после подтверждения оплаты."


def _format_price(price_rub: object) -> str:
    return f"{float(price_rub):.0f} ₽"


def _format_price_from_kopecks(amount_kopecks: int) -> str:
    return f"{amount_kopecks / 100:.0f} ₽"


def _snapshot_amount_text(snapshot: dict[str, object]) -> str:
    parts: list[str] = []
    if bool(snapshot.get("is_unlimited")):
        days = _snapshot_int(snapshot.get("duration_days"))
        parts.append(f"безлимит на {days} д." if days > 0 else "безлимит")
    photo_credits = _snapshot_int(snapshot.get("photo_credits"))
    video_credits = _snapshot_int(snapshot.get("video_credits"))
    common_credits = _snapshot_int(snapshot.get("credits"))
    if photo_credits > 0:
        parts.append(f"{photo_credits} фото")
    if video_credits > 0:
        parts.append(f"{video_credits} видео")
    if common_credits > 0:
        parts.append(f"{common_credits} универсальных кредитов")
    return " + ".join(parts) if parts else "0 кредитов"


def _snapshot_terms_text(snapshot: dict[str, object]) -> str:
    terms = str(snapshot.get("terms") or "").strip()
    if terms:
        return terms
    if bool(snapshot.get("is_unlimited")):
        days = _snapshot_int(snapshot.get("duration_days"))
        if days > 0:
            return f"Безлимит действует {days} д. с момента подтверждения оплаты."
        return "Безлимит активируется после подтверждения оплаты."
    return "Кредиты зачисляются на баланс сразу после подтверждения оплаты."


def _snapshot_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
