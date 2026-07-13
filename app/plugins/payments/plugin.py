from __future__ import annotations

from html import escape
import logging

from aiogram import Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder
from sqlalchemy import select

from app.context import AppContext
from app.db import session_scope
from app.models import CreditPackage, Payment
from app.plugins.common import ensure_user_for_callback, ensure_user_for_message
from app.repositories import list_packages
from app.services.payments import (
    PackagePaymentInit,
    PaymentPackageUnavailable,
    PaymentProviderError,
    create_package_payment,
)
from app.ui import add_navigation_buttons, navigation_keyboard, package_credits_text, packages_keyboard

router = Router(name="payments")
logger = logging.getLogger(__name__)


@router.message(F.text.in_({"Пакеты", "Пополнить"}))
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
        await _send_packages(callback.message, context, edit=True)
    await callback.answer()


async def _send_packages(message: Message, context: AppContext, *, edit: bool = False) -> None:
    async with session_scope(context.session_factory) as session:
        items = [
            package
            for package in await list_packages(session, only_enabled=True)
            if not package.is_unlimited
        ]
    text = (
        "<b>Пополнение</b>\n\nВыберите пакет. Перед оплатой покажу состав и условия."
        if items
        else "<b>Пополнение</b>\n\nПакеты временно недоступны."
    )
    markup = packages_keyboard(items)
    if edit:
        try:
            await message.edit_text(text, reply_markup=markup)
            return
        except TelegramBadRequest as exc:
            error = str(exc).lower()
            if "message is not modified" in error:
                return
            if "there is no text in the message to edit" not in error:
                raise
    await message.answer(text, reply_markup=markup)


@router.callback_query(F.data.startswith("pay:preview:"))
@router.callback_query(F.data.startswith("pay:package:"))
async def package_preview(callback: CallbackQuery, context: AppContext) -> None:
    await ensure_user_for_callback(callback, context)
    package_id = _package_id_from_callback(callback.data)
    if not package_id:
        await callback.answer("Пакет недоступен", show_alert=True)
        return
    async with session_scope(context.session_factory) as session:
        package = await session.get(CreditPackage, package_id)
    if not package or not package.is_enabled or package.is_unlimited:
        await callback.answer("Пакет недоступен", show_alert=True)
        return
    if callback.message:
        text = _package_preview_text(package)
        markup = _package_preview_keyboard(package.id)
        try:
            await callback.message.edit_text(text, reply_markup=markup)
        except TelegramBadRequest as exc:
            error = str(exc).lower()
            if "message is not modified" not in error:
                await callback.message.answer(text, reply_markup=markup)
    await callback.answer()


@router.callback_query(F.data.startswith("pay:create:"))
async def package_payment_create(callback: CallbackQuery, context: AppContext) -> None:
    user = await ensure_user_for_callback(callback, context)
    package_id = _callback_int(callback.data, "pay:create:")
    if not package_id:
        await callback.answer("Пакет недоступен", show_alert=True)
        return
    await callback.answer("Создаю оплату...")
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
                "Пакет больше недоступен. Выберите другой вариант.",
                reply_markup=navigation_keyboard(back_callback="menu:packages"),
            )
        return
    except PaymentProviderError:
        logger.exception("Payment creation failed")
        if callback.message:
            await callback.message.answer(
                "Не получилось создать оплату. Попробуйте позже.",
                reply_markup=navigation_keyboard(back_callback="menu:packages"),
            )
        return

    if callback.message:
        await _send_payment_result(callback.message, result)


@router.callback_query(F.data == "pay:custom")
async def custom_credits_disabled(callback: CallbackQuery) -> None:
    await callback.answer("Выберите один из готовых пакетов.", show_alert=True)


@router.message(F.text == "Оплаты")
async def payments_alias(message: Message, context: AppContext, state: FSMContext) -> None:
    await packages(message, context, state)


async def find_payment_by_order(context: AppContext, order_id: str) -> Payment | None:
    async with session_scope(context.session_factory) as session:
        return await session.scalar(select(Payment).where(Payment.order_id == order_id))


def setup(dispatcher: Dispatcher, context: AppContext) -> None:
    dispatcher.include_router(router)


def _package_preview_keyboard(package_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Перейти к оплате", callback_data=f"pay:create:{package_id}")
    nav_count = add_navigation_buttons(builder, back_callback="menu:packages")
    builder.adjust(1, nav_count)
    return builder.as_markup()


def _payment_keyboard(payment_url: str) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Открыть оплату", url=payment_url)
    nav_count = add_navigation_buttons(builder, back_callback="menu:packages")
    builder.adjust(1, nav_count)
    return builder.as_markup()


def _package_preview_text(package: CreditPackage) -> str:
    description = str(package.description or "").strip()
    description_text = f"\n\n{escape(description)}" if description else ""
    return (
        f"<b>{escape(package.title)}</b>\n\n"
        f"В пакете: <b>{escape(package_credits_text(package))}</b>\n"
        f"Цена: <b>{_format_price(package.price_rub)}</b>"
        f"{description_text}\n\n"
        f"{escape(_package_terms_text(package))}"
    )


async def _send_payment_result(message: Message, result: PackagePaymentInit) -> None:
    if result.status == "manual_pending":
        await message.answer(
            "Заявка создана. После подтверждения администратором кредиты появятся на балансе.\n\n"
            f"{_payment_result_text(result)}",
            reply_markup=navigation_keyboard(back_callback="menu:packages"),
        )
        return
    if result.payment_url:
        await message.answer(
            f"{_payment_result_text(result)}\n\nПосле оплаты баланс обновится автоматически.",
            reply_markup=_payment_keyboard(result.payment_url),
        )
        return
    await message.answer(
        "Платёж создан, но ссылка не получена. Попробуйте ещё раз позже.",
        reply_markup=navigation_keyboard(back_callback="menu:packages"),
    )


def _payment_result_text(result: PackagePaymentInit) -> str:
    snapshot = result.package_snapshot
    return (
        f"Пакет: <b>{escape(str(snapshot.get('title') or 'пакет'))}</b>\n"
        f"Начисление: <b>{escape(_snapshot_amount_text(snapshot))}</b>\n"
        f"Сумма: <b>{_format_price_from_kopecks(result.amount_kopecks)}</b>"
    )


def _package_terms_text(package: CreditPackage) -> str:
    terms = str(package.terms or "").strip()
    return terms or "Кредиты зачисляются сразу после подтверждения оплаты."


def _format_price(price_rub: object) -> str:
    return f"{float(price_rub):.0f} ₽"


def _format_price_from_kopecks(amount_kopecks: int) -> str:
    return f"{amount_kopecks / 100:.0f} ₽"


def _snapshot_amount_text(snapshot: dict[str, object]) -> str:
    parts: list[str] = []
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


def _package_id_from_callback(value: str | None) -> int:
    for prefix in ("pay:preview:", "pay:package:"):
        result = _callback_int(value, prefix)
        if result:
            return result
    return 0


def _callback_int(value: str | None, prefix: str) -> int:
    if not value or not value.startswith(prefix):
        return 0
    try:
        result = int(value.removeprefix(prefix))
    except ValueError:
        return 0
    return result if result > 0 else 0


def _snapshot_int(value: object) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0
