from __future__ import annotations

from contextlib import suppress
from html import escape

from aiogram import Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from app.context import AppContext
from app.db import session_scope
from app.models import User
from app.plugins.common import ensure_user_for_callback, ensure_user_for_message, is_admin_user
from app.repositories import ensure_partner_code, get_public_gallery
from app.services.referrals import build_ref_link
from app.ui import main_menu, navigation_keyboard

router = Router(name="gallery")


@router.message(F.text == "Галерея")
@router.message(Command("gallery"))
async def gallery(message: Message, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    user = await ensure_user_for_message(message, context)
    await _send_gallery(message, context, user=user)


@router.callback_query(F.data == "menu:gallery")
async def gallery_callback(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    user = await ensure_user_for_callback(callback, context)
    if callback.message:
        await _send_gallery(callback.message, context, user=user)
    await callback.answer()


async def _send_gallery(message: Message, context: AppContext, *, user: User) -> None:
    async with session_scope(context.session_factory) as session:
        items = await get_public_gallery(session, limit=10)
        author_codes: dict[int, str | None] = {}
        for item in items:
            if not item.user_id or item.user_id in author_codes:
                continue
            user = await session.get(User, item.user_id)
            if user:
                await ensure_partner_code(session, user)
                author_codes[item.user_id] = user.partner_code
            else:
                author_codes[item.user_id] = None
    if not items:
        await message.answer(
            "В публичной галерее пока пусто.",
            reply_markup=main_menu(
                is_admin_user(user, context),
                mini_app_url=context.settings.mini_app_url,
            ),
        )
        return
    await message.answer("Публичная галерея:", reply_markup=navigation_keyboard())
    for item in items:
        caption = _gallery_caption(item)
        keyboard = None
        if item.user_id:
            ref_link = await build_ref_link(context.bot, author_codes.get(item.user_id))
            if ref_link:
                keyboard = InlineKeyboardMarkup(
                    inline_keyboard=[
                        [InlineKeyboardButton(text="Открыть по ссылке автора", url=ref_link)]
                    ]
                )
        with suppress(Exception):
            if item.media_type == "video":
                await message.answer_video(
                    item.media_url, caption=caption[:1024], reply_markup=keyboard
                )
            else:
                await message.answer_photo(
                    item.media_url, caption=caption[:1024], reply_markup=keyboard
                )
            continue
        await message.answer(f"{caption}\n\n{escape(str(item.media_url))}", reply_markup=keyboard)


def _gallery_caption(item) -> str:
    title = escape(str(item.title or "Работа"))
    prompt = escape(str(item.prompt or "не указан"))
    return f"{title}\n\nПромпт:\n{prompt}"


def setup(dispatcher: Dispatcher, context: AppContext) -> None:
    dispatcher.include_router(router)
