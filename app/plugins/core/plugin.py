from __future__ import annotations

from contextlib import suppress
from html import escape

from aiogram import Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

from app.context import AppContext
from app.db import session_scope
from app.models import BotSetting, User
from app.plugins.common import ensure_user_for_callback, ensure_user_for_message, is_admin_user
from app.repositories import bind_referral, ensure_partner_code, user_has_unlimited
from app.ui import account_menu, balance_keyboard, main_menu, mini_app_keyboard, navigation_keyboard

router = Router(name="core")


def _main_menu_markup(user: User, context: AppContext) -> InlineKeyboardMarkup:
    return main_menu(is_admin_user(user, context), mini_app_url=context.settings.mini_app_url)


def _account_menu_markup(user: User, context: AppContext) -> InlineKeyboardMarkup:
    return account_menu(is_admin_user(user, context))


async def _welcome_text(context: AppContext) -> str:
    async with session_scope(context.session_factory) as session:
        setting = await session.get(BotSetting, "welcome_text")
        if setting and setting.value.get("text"):
            return escape(str(setting.value["text"]))
    return "Что создаём?"


async def _edit_or_answer(
    callback: CallbackQuery,
    text: str,
    *,
    reply_markup: InlineKeyboardMarkup,
) -> None:
    if not callback.message:
        return
    try:
        await callback.message.edit_text(text, reply_markup=reply_markup)
    except TelegramBadRequest as exc:
        error = str(exc).lower()
        if "message is not modified" in error:
            return
        if "there is no text in the message to edit" not in error:
            raise
        await callback.message.answer(text, reply_markup=reply_markup)


@router.message(CommandStart())
async def start(
    message: Message,
    context: AppContext,
    command: CommandObject,
    state: FSMContext,
) -> None:
    await state.clear()
    user = await ensure_user_for_message(message, context)
    referrer_name = None
    if command.args:
        async with session_scope(context.session_factory) as session:
            fresh_user = await session.get(User, user.id)
            if fresh_user:
                referrer = await bind_referral(session, user=fresh_user, ref_code=command.args)
                if referrer:
                    referrer_name = referrer.first_name or referrer.username or "партнёра"
    await message.answer(
        await _welcome_text(context),
        reply_markup=_main_menu_markup(user, context),
    )
    if referrer_name:
        await message.answer(f"Приглашение от {escape(referrer_name)} принято.")


@router.message(Command("menu"))
@router.message(F.text.in_({"Меню", "Главное меню", "Главная", "Домой", "Назад / Домой"}))
async def menu(message: Message, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    user = await ensure_user_for_message(message, context)
    await message.answer(await _welcome_text(context), reply_markup=_main_menu_markup(user, context))


@router.message(Command("app"))
async def mini_app(message: Message, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    await ensure_user_for_message(message, context)
    await message.answer(
        "Студия BANANA — быстрый визуальный бриф и запуск генерации.",
        reply_markup=mini_app_keyboard(context.settings.mini_app_url),
    )


@router.callback_query(F.data == "menu:main")
async def menu_callback(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    user = await ensure_user_for_callback(callback, context)
    await _edit_or_answer(
        callback,
        await _welcome_text(context),
        reply_markup=_main_menu_markup(user, context),
    )
    await callback.answer()


@router.message(F.text.in_({"Ещё", "Еще", "Профиль"}))
async def account(message: Message, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    user = await ensure_user_for_message(message, context)
    await message.answer("Профиль", reply_markup=_account_menu_markup(user, context))


@router.callback_query(F.data.in_({"menu:account", "menu:more"}))
async def account_callback(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    user = await ensure_user_for_callback(callback, context)
    await _edit_or_answer(callback, "Профиль", reply_markup=_account_menu_markup(user, context))
    await callback.answer()


@router.callback_query(F.data == "menu:balance")
async def balance_callback(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    user = await ensure_user_for_callback(callback, context)
    await _edit_or_answer(
        callback,
        await _balance_text(user, context),
        reply_markup=balance_keyboard(),
    )
    await callback.answer()


@router.callback_query(F.data == "menu:support")
async def support_callback(callback: CallbackQuery) -> None:
    await _edit_or_answer(
        callback,
        "<b>Поддержка</b>\n\n"
        "Технические вопросы: @Chillcreative\n"
        "Сотрудничество и реклама: @LeLu88",
        reply_markup=navigation_keyboard(back_callback="menu:account"),
    )
    await callback.answer()


@router.message(Command("balance"))
@router.message(F.text == "Баланс")
async def balance(message: Message, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    user = await ensure_user_for_message(message, context)
    await message.answer(await _balance_text(user, context), reply_markup=balance_keyboard())


async def _balance_text(user: User, context: AppContext) -> str:
    async with session_scope(context.session_factory) as session:
        fresh_user = await session.get(User, user.id)
        if fresh_user:
            await ensure_partner_code(session, fresh_user)
            await session.flush()
            user = fresh_user
    access = ""
    if user.is_admin:
        access = "\nДоступ: <b>администратор</b>"
    elif user_has_unlimited(user):
        access = f"\nБезлимит до: <b>{user.unlimited_until:%d.%m.%Y %H:%M}</b>"
    affiliate = int(user.affiliate_balance_kopecks or 0) / 100
    return (
        "<b>Баланс</b>\n\n"
        f"Фото: <b>{int(user.photo_credits_balance or 0)}</b>\n"
        f"Видео: <b>{int(user.video_credits_balance or 0)}</b>\n"
        f"Универсальные: <b>{int(user.credits_balance or 0)}</b>\n"
        f"Партнёрский баланс: <b>{affiliate:.0f} ₽</b>"
        f"{access}"
    )


@router.message(Command("help"))
async def help_command(message: Message, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    user = await ensure_user_for_message(message, context)
    admin_hint = "\n/admin — управление проектом" if is_admin_user(user, context) else ""
    await message.answer(
        "<b>Команды</b>\n\n"
        "/image — создать фото\n"
        "/motion — создать видео\n"
        "/feed — открыть ленту\n"
        "/balance — посмотреть баланс\n"
        "/packages — пополнить кредиты\n"
        "/partners — партнёрская программа"
        f"{admin_hint}",
        reply_markup=navigation_keyboard(),
    )


def setup(dispatcher: Dispatcher, context: AppContext) -> None:
    dispatcher.include_router(router)
