from __future__ import annotations

from contextlib import suppress

from aiogram import Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.context import AppContext
from app.db import session_scope
from app.plugins.common import ensure_user_for_callback, is_admin_user
from app.repositories import get_public_feed_task, serialize_feed_task
from app.ui import add_navigation_buttons

router = Router(name="ux")

ADMIN_HOME_BUTTONS: tuple[tuple[str, str], ...] = (
    ("Обзор", "admin:ux:overview"),
    ("Пользователи", "admin:users"),
    ("Генерации", "admin:orders"),
    ("Платежи", "admin:payments"),
    ("Каталог", "admin:ux:catalog"),
    ("Добавить тариф", "admin:tariff:add"),
    ("Партнёрка", "admin:ux:affiliate"),
    ("Коммуникации", "admin:ux:communications"),
    ("Система", "admin:ux:system"),
)

ADMIN_SECTIONS: dict[str, tuple[str, str, tuple[tuple[str, str], ...]]] = {
    "admin:ux:overview": (
        "Обзор",
        "Ключевые показатели проекта и финансовая целостность.",
        (
            ("Статистика", "admin:stats"),
            ("Аналитика", "admin:analytics"),
            ("Финансы", "admin:finance"),
        ),
    ),
    "admin:ux:catalog": (
        "Каталог",
        "Модели, цены, тарифы, подписки и публичные работы.",
        (
            ("Добавить тариф", "admin:tariff:add"),
            ("Тарифы и подписки", "admin:packages"),
            ("Модели и цены", "admin:models"),
            ("Публичные работы", "admin:gallery"),
        ),
    ),
    "admin:ux:affiliate": (
        "Партнёрка",
        "Рефералы, выплаты и полезные партнёрские ссылки.",
        (
            ("Рефералы", "admin:referrals"),
            ("Заявки на вывод", "admin:withdrawals"),
            ("Партнёрские ссылки", "admin:partners"),
        ),
    ),
    "admin:ux:communications": (
        "Коммуникации",
        "Приветствие, обращения пользователей и рассылки.",
        (
            ("Тексты и настройки", "admin:settings"),
            ("Обращения", "admin:support"),
            ("Рассылка", "admin:broadcast"),
        ),
    ),
    "admin:ux:system": (
        "Система",
        "Диагностика работы сервиса.",
        (("Логи ошибок", "admin:logs"),),
    ),
}


def _admin_home_keyboard() -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    for text, callback_data in ADMIN_HOME_BUTTONS:
        builder.button(text=text, callback_data=callback_data)
    builder.button(text="Главная", callback_data="menu:main")
    builder.adjust(2, 2, 2, 2, 1, 1)
    return builder.as_markup()


def _section_keyboard(items: tuple[tuple[str, str], ...]) -> InlineKeyboardMarkup:
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


@router.callback_query(F.data.in_(set(ADMIN_SECTIONS)))
async def admin_section(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    if not await _require_admin(callback, context):
        return
    title, description, items = ADMIN_SECTIONS[str(callback.data)]
    if callback.message:
        from app.plugins.admin import plugin as admin_plugin

        await admin_plugin._edit_or_answer_admin(
            callback,
            f"<b>{title}</b>\n\n{description}",
            reply_markup=_section_keyboard(items),
        )
    await callback.answer()


@router.callback_query(F.data == "admin:tariff:add")
async def admin_tariff_add(
    callback: CallbackQuery,
    context: AppContext,
    state: FSMContext,
) -> None:
    if not await _require_admin(callback, context):
        return
    from app.plugins.admin import plugin as admin_plugin

    await state.set_state(admin_plugin.AdminStates.package_add)
    await state.update_data(package_step="code")
    if callback.message:
        await callback.message.answer(
            "Новый тариф\n\n"
            "Шаг 1 из 9. Введите уникальный короткий код латиницей.\n"
            "Например: pro_month или video_50\n\n"
            "Дальше можно настроить фото-, видео- и универсальные кредиты, "
            "цену, срок подписки и условия.",
            reply_markup=admin_plugin._cancel_keyboard("admin:packages"),
        )
    await callback.answer()


def _install_admin_navigation() -> None:
    from app.plugins.admin import plugin as admin_plugin

    admin_plugin._admin_keyboard = _admin_home_keyboard
    admin_plugin._admin_home_text = lambda: (
        "<b>Админка</b>\n\n"
        "Выберите раздел. Новый тариф можно создать прямо с главного экрана. "
        "Опасные действия остаются внутри соответствующих карточек."
    )


def _install_generation_navigation() -> None:
    from app.plugins.generation import plugin as generation_plugin

    original = generation_plugin._send_image_request_screen
    if getattr(original, "_ux_model_choice_installed", False):
        return

    async def wrapped(message: Message, context: AppContext, state: FSMContext) -> None:
        data = await state.get_data()
        if not data.get("explicit_model_selected"):
            await generation_plugin._send_model_menu(
                message,
                context,
                "image",
                "Выберите модель для фото:",
            )
            return
        await original(message, context, state)

    setattr(wrapped, "_ux_model_choice_installed", True)
    generation_plugin._send_image_request_screen = wrapped


async def _start_image_status_after_prompt(
    message: Message,
    state: FSMContext,
    bot: Bot,
) -> int:
    """Create the mutable generation status below the user's prompt message."""

    data = await state.get_data()
    previous_status_message_id = data.get("status_message_id")
    status_message = await message.answer(
        _generation_status_text("Готовлю референсы", 45)
    )
    await state.update_data(status_message_id=status_message.message_id)

    if (
        isinstance(previous_status_message_id, int)
        and previous_status_message_id != status_message.message_id
    ):
        with suppress(TelegramBadRequest):
            await bot.edit_message_reply_markup(
                chat_id=message.chat.id,
                message_id=previous_status_message_id,
                reply_markup=None,
            )
    return status_message.message_id


def _generation_status_text(title: str, percent: int) -> str:
    from app.plugins.generation import plugin as generation_plugin

    return generation_plugin._status_text(title, percent)


def _install_generation_status_after_prompt() -> None:
    from app.plugins.generation import plugin as generation_plugin

    original = generation_plugin._submit_image_task_from_message
    if getattr(original, "_ux_status_after_prompt_installed", False):
        return

    async def wrapped(
        message: Message,
        context: AppContext,
        state: FSMContext,
        bot: Bot,
    ) -> None:
        await _start_image_status_after_prompt(message, state, bot)
        await original(message, context, state, bot)

    setattr(wrapped, "_ux_status_after_prompt_installed", True)
    generation_plugin._submit_image_task_from_message = wrapped


def _install_feed_refresh() -> None:
    from app.plugins.feed import plugin as feed_plugin

    original = feed_plugin._refresh_feed_card
    if getattr(original, "_ux_edit_caption_installed", False):
        return

    async def wrapped(
        message: Message,
        context: AppContext,
        *,
        viewer_user_id: int,
        task_id: int,
    ) -> None:
        async with session_scope(context.session_factory) as session:
            task = await get_public_feed_task(session, task_id)
            row = await serialize_feed_task(session, task) if task else None
        if not task or not row:
            return
        caption = feed_plugin._feed_caption(row)
        keyboard = feed_plugin._feed_keyboard(
            task,
            viewer_user_id=viewer_user_id,
            index=0,
            total=1,
            dislikes=int(row.get("dislikes") or 0),
        )
        try:
            await message.edit_caption(caption=caption[:1024], reply_markup=keyboard)
        except TelegramBadRequest as exc:
            error = str(exc).lower()
            if "message is not modified" in error:
                return
            await original(
                message,
                context,
                viewer_user_id=viewer_user_id,
                task_id=task_id,
            )

    setattr(wrapped, "_ux_edit_caption_installed", True)
    feed_plugin._refresh_feed_card = wrapped


def setup(dispatcher: Dispatcher, context: AppContext) -> None:
    del context
    _install_admin_navigation()
    _install_generation_navigation()
    _install_generation_status_after_prompt()
    _install_feed_refresh()
    dispatcher.include_router(router)
