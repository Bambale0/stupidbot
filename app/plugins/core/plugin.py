from __future__ import annotations

from contextlib import suppress
from html import escape

from aiogram import Dispatcher, F, Router
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.context import AppContext
from app.db import session_scope
from app.models import BotSetting, User
from app.plugins.common import ensure_user_for_callback, ensure_user_for_message, is_admin_user
from app.repositories import (
    bind_referral,
    ensure_partner_code,
    list_enabled_models,
    model_credit_type,
    user_credit_balance,
    user_generates_for_free,
    user_has_unlimited,
)
from app.services.referrals import build_ref_link
from app.ui import main_menu, mini_app_keyboard, more_menu, navigation_keyboard

router = Router(name="core")
EMPTY_HOME_TEXTS = {
    "В публичной галерее пока пусто.",
    "В публичной ленте пока пусто.",
}


def _main_menu_markup(user: User, context: AppContext):
    return main_menu(is_admin_user(user, context), mini_app_url=context.settings.mini_app_url)


def _more_menu_markup(user: User, context: AppContext):
    return more_menu(is_admin_user(user, context), mini_app_url=context.settings.mini_app_url)


async def _welcome_text(context: AppContext) -> str:
    async with session_scope(context.session_factory) as session:
        setting = await session.get(BotSetting, "welcome_text")
        if setting and setting.value.get("text"):
            return escape(str(setting.value["text"]))
    return "Привет. Выберите раздел в меню."


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
                    referrer_name = referrer.first_name or referrer.username or "партнера"
    await message.answer(
        await _welcome_text(context),
        reply_markup=_main_menu_markup(user, context),
    )
    if referrer_name:
        await message.answer(f"Вы пришли по приглашению от {escape(referrer_name)}.")


@router.message(Command("menu"))
@router.message(F.text.in_({"Меню", "Главное меню", "Домой", "Назад / Домой"}))
async def menu(message: Message, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    user = await ensure_user_for_message(message, context)
    await message.answer(await _welcome_text(context), reply_markup=_main_menu_markup(user, context))


@router.message(Command("app"))
async def mini_app(message: Message, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    await ensure_user_for_message(message, context)
    await message.answer(
        "Апка BANANA", reply_markup=mini_app_keyboard(context.settings.mini_app_url)
    )


@router.callback_query(F.data == "menu:main")
async def menu_callback(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    user = await ensure_user_for_callback(callback, context)
    if callback.message:
        markup = _main_menu_markup(user, context)
        text = await _welcome_text(context)
        with suppress(Exception):
            await callback.message.edit_text(text, reply_markup=markup)
            await callback.answer()
            return
        await callback.message.answer(text, reply_markup=markup)
    await callback.answer()


@router.message(F.text == "Еще")
async def more(message: Message, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    user = await ensure_user_for_message(message, context)
    await message.answer("Еще", reply_markup=_more_menu_markup(user, context))


@router.callback_query(F.data == "menu:more")
async def more_callback(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    user = await ensure_user_for_callback(callback, context)
    if callback.message:
        await callback.message.answer("Еще", reply_markup=_more_menu_markup(user, context))
    await callback.answer()


@router.callback_query(F.data == "menu:balance")
async def balance_callback(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    user = await ensure_user_for_callback(callback, context)
    if callback.message:
        await callback.message.answer(
            await _balance_text(user, context), reply_markup=navigation_keyboard()
        )
    await callback.answer()


@router.callback_query(F.data == "menu:support")
async def support_callback(callback: CallbackQuery) -> None:
    await callback.answer()
    if callback.message:
        await callback.message.answer(
            "🆘 <b>Поддержка BANANA</b>\n\n"
            "По техническим вопросам и багам — пиши в <b>Тех. Отдел</b>\n"
            "@Chillcreative\n\n"
            "По вопросам сотрудничества и рекламы — пиши <b>Поддержка / Реклама</b>\n"
            "@LeLu88",
            parse_mode="HTML",
            reply_markup=navigation_keyboard(back_callback="menu:more"),
        )


@router.message(Command("balance"))
@router.message(F.text == "Баланс")
async def balance(message: Message, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    user = await ensure_user_for_message(message, context)
    await message.answer(await _balance_text(user, context), reply_markup=navigation_keyboard())


async def _balance_text(user, context: AppContext) -> str:
    async with session_scope(context.session_factory) as session:
        fresh_user = await session.get(User, user.id)
        if fresh_user:
            await ensure_partner_code(session, fresh_user)
            await session.flush()
            user = fresh_user
        image_models = await list_enabled_models(session, "image")
        video_models = await list_enabled_models(session, "video")
        models = [*image_models, *video_models]
    unlimited = ""
    if user.is_admin:
        unlimited = "\nАдмин-доступ: генерации без списания кредитов."
    elif user_has_unlimited(user):
        unlimited = f"\nБезлимит активен до: {user.unlimited_until:%Y-%m-%d %H:%M}"
    affiliate = user.affiliate_balance_kopecks / 100
    link = await build_ref_link(context.bot, user.partner_code)
    generation_status = _generation_status_text(user, models)
    text = (
        "💰 <b>Ваш баланс</b>\n\n"
        f"📸 Фото-кредиты: <b>{int(user.photo_credits_balance or 0)}</b>\n"
        f"🎬 Видео-кредиты: <b>{int(user.video_credits_balance or 0)}</b>\n"
        f"🪙 Универсальные: <b>{int(user.credits_balance or 0)}</b>{unlimited}\n"
        f"💵 Партнёрский: <b>{affiliate:.0f} ₽</b>\n"
        f"\n{generation_status}\n\n"
        "🤝 <b>Партнёрская программа</b>\n"
        "30% с покупок приглашённых пользователей.\n"
        "Амбасадоры — 50%. Интересно? В поддержку!"
    )
    if link:
        text += f"\n\n🔗 <b>Ваша партнёрская ссылка:</b>\n{link}"
    return text


def _generation_status_text(user: User, models: list) -> str:
    if not models:
        return "Статус генераций: модели сейчас недоступны."
    lines = ["Статус генераций:"]
    free_generation = user_generates_for_free(user)
    for model in models:
        price = int(model.price_credits or 0)
        credit_type = model_credit_type(model)
        balance = user_credit_balance(user, credit_type)
        price_unit = "/сек" if (model.config or {}).get("price_unit") == "second" else ""
        if free_generation or price <= 0:
            available = "безлимит"
        elif price_unit:
            available = f"{max(0, balance // price)} сек"
        else:
            available = str(max(0, balance // price))
        lines.append(
            f"{escape(model.title)}: {_credit_amount_text(price, credit_type)}{price_unit} · "
            f"доступно {available}"
        )
    return "\n".join(lines)


def _credit_amount_text(value: int, credit_type: str) -> str:
    if credit_type == "photo":
        return f"{value} фото-кредитов"
    if credit_type == "video":
        return f"{value} видео-кредитов"
    return f"{value} кредитов"


@router.message(Command("help"))
async def help_command(message: Message, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    user = await ensure_user_for_message(message, context)
    admin_hint = "\nАдминка: «Еще» -> «Админка» или /admin." if is_admin_user(user, context) else ""
    await message.answer(
        "Доступные разделы:\n"
        "Banana - генерация изображений.\n"
        "AI Video - видео по изображению и prompt.\n"
        "BANANA - мини-апп для сборки визуального брифа.\n"
        "Лента - публичные работы пользователей, лайки и повторы.\n"
        "Галерея - публичные результаты и промпты.\n"
        "Пакеты - пополнение кредитов и безлимит."
        f"{admin_hint}",
        reply_markup=navigation_keyboard(),
    )


def setup(dispatcher: Dispatcher, context: AppContext) -> None:
    dispatcher.include_router(router)
