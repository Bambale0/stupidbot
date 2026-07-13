from __future__ import annotations

from aiogram import Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.context import AppContext
from app.plugins.common import ensure_user_for_callback, ensure_user_for_message
from app.ui import add_navigation_buttons

router = Router(name="gallery")


@router.message(F.text == "Галерея")
@router.message(Command("gallery"))
async def gallery(message: Message, context: AppContext, state: FSMContext) -> None:
    """Backward-compatible route after merging gallery into the interactive feed."""

    await state.clear()
    await ensure_user_for_message(message, context)
    await message.answer(
        "Галерея объединена с лентой. Там можно смотреть работы, ставить лайки и повторять настройки.",
        reply_markup=_open_feed_keyboard(),
    )


@router.callback_query(F.data == "menu:gallery")
async def gallery_callback(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    await ensure_user_for_callback(callback, context)
    if callback.message:
        await callback.message.answer(
            "Галерея объединена с лентой.",
            reply_markup=_open_feed_keyboard(),
        )
    await callback.answer()


def _open_feed_keyboard():
    builder = InlineKeyboardBuilder()
    builder.button(text="Открыть ленту", callback_data="menu:feed")
    nav_count = add_navigation_buttons(builder, back_callback="menu:main")
    builder.adjust(1, nav_count)
    return builder.as_markup()


def setup(dispatcher: Dispatcher, context: AppContext) -> None:
    dispatcher.include_router(router)
