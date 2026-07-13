from __future__ import annotations

from aiogram import Dispatcher, F, Router
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.context import AppContext
from app.plugins.common import ensure_user_for_callback, is_admin_user
from app.ui import add_navigation_buttons

router = Router(name="ux")


def _admin_home_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    buttons = [
        ("Обзор", "admin:ux:overview"),
        ("Пользователи", "admin:users"),
        ("Генерации", "admin:orders"),
        ("Платежи", "admin:payments"),
        ("Каталог", "admin:ux:catalog"),
        ("Партнёрка", "admin:ux:affiliate"),
        ("Коммуникации", "admin:ux:communications"),
        ("Система", "admin:ux:system"),
    ]
    for text, callback_data in buttons:
        builder.button(text=text, callback_data=callback_data)
    builder.button(text="Главная", callback_data="menu:main")
    builder.adjust(2, 2, 2, 2, 1)
    return builder.as_markup()


def _section_keyboard(items: list[tuple[str, str]]) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for text, callback_data in items:
        builder.button(text=text, callback_data=callback_data)
    nav_count = add_navigation_buttons(builder, back_callback="admin:menu")
    builder.adjust(*([1] * len(items)), nav_count)
    return builder.as_markup()


async def _require_admin(callback: CallbackQuery, context: AppContext) -> bool:
    user = await ensure_user_for_callback(callback, context)
    if not is_admin_user(user, context):
        await callback.answer("Нет доступа", show_alert=True)
        return False
    return True


async def _render_section(
    callback: CallbackQuery,
    context: AppContext,
    state: FSMContext,
    *,
    title: str,
    description: str,
    items: list[tuple[str, str]],
) -> None:
    await state.clear()
    if not await _require_admin(callback, context):
        return
    if callback.message:
        from app.plugins.admin import plugin as admin_plugin

        await admin_plugin._edit_or_answer_admin(
            callback,
            f"<b>{title}</b>\n\n{description}",
            reply_markup=_section_keyboard(items),
        )
    await callback.answer()


@router.callback_query(F.data == "admin:ux:overview")
async def admin_overview(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await _render_section(
        callback,
        context,
        state,
        title="Обзор",
        description="Ключевые показатели проекта и финансовая целостность.",
        items=[
            ("Статистика", "admin:stats"),
            ("Аналитика", "admin:analytics"),
            ("Финансы", "admin:finance"),
        ],
    )


@router.callback_query(F.data == "admin:ux:catalog")
async def admin_catalog(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await _render_section(
        callback,
        context,
        state,
        title="Каталог",
        description="Модели, цены, пакеты и публичные работы.",
        items=[
            ("Модели и цены", "admin:models"),
            ("Пакеты", "admin:packages"),
            ("Публичные работы", "admin:gallery"),
        ],
    )


@router.callback_query(F.data == "admin:ux:affiliate")
async def admin_affiliate(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await _render_section(
        callback,
        context,
        state,
        title="Партнёрка",
        description="Рефералы, выплаты и полезные партнёрские ссылки.",
        items=[
            ("Рефералы", "admin:referrals"),
            ("Заявки на вывод", "admin:withdrawals"),
            ("Партнёрские ссылки", "admin:partners"),
        ],
    )


@router.callback_query(F.data == "admin:ux:communications")
async def admin_communications(
    callback: CallbackQuery,
    context: AppContext,
    state: FSMContext,
) -> None:
    await _render_section(
        callback,
        context,
        state,
        title="Коммуникации",
        description="Приветствие, обращения пользователей и рассылки.",
        items=[
            ("Тексты и настройки", "admin:settings"),
            ("Обращения", "admin:support"),
            ("Рассылка", "admin:broadcast"),
        ],
    )


@router.callback_query(F.data == "admin:ux:system")
async def admin_system(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await _render_section(
        callback,
        context,
        state,
        title="Система",
        description="Диагностика и безопасная повторная проверка операций.",
        items=[
            ("Логи ошибок", "admin:logs"),
            ("Операции", "admin:orders"),
        ],
    )


def _install_admin_navigation() -> None:
    from app.plugins.admin import plugin as admin_plugin

    admin_plugin._admin_keyboard = _admin_home_keyboard
    admin_plugin._admin_home_text = lambda: (
        "<b>Админка</b>\n\n"
        "Выберите раздел. Опасные действия остаются внутри соответствующих карточек."
    )


def setup(dispatcher: Dispatcher, context: AppContext) -> None:
    del context
    _install_admin_navigation()
    dispatcher.include_router(router)
