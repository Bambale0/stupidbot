from __future__ import annotations

from contextlib import suppress
from html import escape

from aiogram import Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message
from aiogram.utils.keyboard import InlineKeyboardBuilder

from app.context import AppContext
from app.db import session_scope
from app.models import GenerationTask, User
from app.plugins.common import ensure_user_for_callback, ensure_user_for_message, is_admin_user
from app.plugins.generation.plugin import ImageFlow, MotionFlow
from app.repositories import (
    generation_media_type,
    get_feed_tasks,
    get_model,
    get_public_feed_task,
    increment_feed_share,
    like_feed_task,
    remove_task_from_feed,
    serialize_feed_task,
    share_task_to_feed,
)
from app.services.generation_catalog import DEFAULT_IMAGE_ASPECT_RATIO
from app.ui import add_navigation_buttons, main_menu, navigation_keyboard

router = Router(name="feed")


@router.message(F.text == "Лента")
@router.message(Command("feed"))
async def feed(message: Message, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    user = await ensure_user_for_message(message, context)
    await _send_feed_card(message, context, viewer_user=user, index=0)


@router.callback_query(F.data == "menu:feed")
async def feed_callback(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    user = await ensure_user_for_callback(callback, context)
    if callback.message:
        await _send_feed_card(callback.message, context, viewer_user=user, index=0)
    await callback.answer()


@router.callback_query(F.data.startswith("feed:next:"))
async def next_feed_card(callback: CallbackQuery, context: AppContext) -> None:
    user = await ensure_user_for_callback(callback, context)
    index = _callback_int(callback.data, "feed:next:", default=0)
    if callback.message:
        await _send_feed_card(callback.message, context, viewer_user=user, index=index)
    await callback.answer()


@router.callback_query(F.data.startswith("feed:like:"))
async def like_feed_card(callback: CallbackQuery, context: AppContext) -> None:
    user = await ensure_user_for_callback(callback, context)
    task_id = _callback_int(callback.data, "feed:like:", default=0)
    async with session_scope(context.session_factory) as session:
        likes, is_new = await like_feed_task(session, task_id=task_id, user_id=user.id)
    if likes is None:
        await callback.answer("Публикация уже недоступна", show_alert=True)
        return
    await callback.answer("Лайк засчитан" if is_new else "Вы уже лайкали эту работу")
    if callback.message:
        await _refresh_feed_card(callback.message, context, viewer_user_id=user.id, task_id=task_id)


@router.callback_query(F.data.startswith("feed:share:"))
async def share_feed_card(callback: CallbackQuery, context: AppContext) -> None:
    task_id = _callback_int(callback.data, "feed:share:", default=0)
    async with session_scope(context.session_factory) as session:
        shares = await increment_feed_share(session, task_id)
    if shares is None:
        await callback.answer("Публикация уже недоступна", show_alert=True)
        return
    await callback.answer("Share засчитан")


@router.callback_query(F.data.startswith("feed:publish:confirm:"))
async def publish_confirm(callback: CallbackQuery, context: AppContext) -> None:
    await ensure_user_for_callback(callback, context)
    task_id = _callback_int(callback.data, "feed:publish:confirm:", default=0)
    if callback.message:
        await callback.message.answer(
            "Опубликовать работу в ленте?\n\n"
            "Результат, модель и публичное имя будут видны другим пользователям. Промпт скрыт.",
            reply_markup=_publish_confirm_keyboard(task_id),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("feed:publish:yes:"))
async def publish_task(callback: CallbackQuery, context: AppContext) -> None:
    user = await ensure_user_for_callback(callback, context)
    task_id = _callback_int(callback.data, "feed:publish:yes:", default=0)
    async with session_scope(context.session_factory) as session:
        ok, reason = await share_task_to_feed(session, task_id=task_id, user_id=user.id)
    texts = {
        "published": "Готово, работа опубликована в ленте.",
        "not_owner": "Можно публиковать только свои работы.",
        "not_completed": "Публиковать можно только готовые генерации.",
        "no_result": "У работы нет результата для публикации.",
        "foreign_source": "Повтор чужой работы нельзя публиковать как свою.",
    }
    await callback.answer(texts.get(reason, "Не получилось опубликовать"), show_alert=not ok)
    if callback.message and ok:
        await callback.message.answer(
            "Работа уже в ленте.", reply_markup=navigation_keyboard(back_callback="menu:feed")
        )


@router.callback_query(F.data.startswith("feed:remove:"))
async def remove_feed_card(callback: CallbackQuery, context: AppContext) -> None:
    user = await ensure_user_for_callback(callback, context)
    task_id = _callback_int(callback.data, "feed:remove:", default=0)
    async with session_scope(context.session_factory) as session:
        removed = await remove_task_from_feed(session, task_id=task_id, user_id=user.id)
    await callback.answer(
        "Снято с ленты" if removed else "Не получилось снять публикацию", show_alert=not removed
    )


@router.callback_query(F.data.startswith("feed:repeat:"))
async def repeat_feed_card(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await ensure_user_for_callback(callback, context)
    task_id = _callback_int(callback.data, "feed:repeat:", default=0)
    async with session_scope(context.session_factory) as session:
        task = await get_public_feed_task(session, task_id)
        model = await get_model(session, task.model_code) if task else None
    if not task or not model:
        await callback.answer("Публикация уже недоступна", show_alert=True)
        return

    await state.clear()
    payload = dict(task.input_payload or {})
    if generation_media_type(task) == "video":
        await state.set_state(MotionFlow.image)
        await state.update_data(
            model_code=task.model_code,
            prompt=task.prompt or "",
            duration=str(payload.get("duration") or "5"),
            mode=str(payload.get("mode") or "pro"),
            source_feed_task_id=task.id,
        )
        text = "Повтор из ленты сохранен. Отправьте изображение-референс для видео."
        back = "menu:motion"
    else:
        await state.set_state(ImageFlow.reference_prompt)
        await state.update_data(
            model_code=task.model_code,
            prompt=task.prompt or "",
            aspect_ratio=payload.get("aspect_ratio") or DEFAULT_IMAGE_ASPECT_RATIO,
            resolution=payload.get("resolution") or "2K",
            source_feed_task_id=task.id,
        )
        text = "Повтор из ленты сохранен. Отправьте фото-референс для изображения."
        back = "menu:image"

    if callback.message:
        await callback.message.answer(text, reply_markup=navigation_keyboard(back_callback=back))
    await callback.answer()


async def _send_feed_card(
    message: Message,
    context: AppContext,
    *,
    viewer_user: User,
    index: int,
) -> None:
    async with session_scope(context.session_factory) as session:
        tasks = await get_feed_tasks(session, limit=30)
    if not tasks:
        await message.answer(
            "В публичной ленте пока пусто.",
            reply_markup=main_menu(
                is_admin_user(viewer_user, context),
                mini_app_url=context.settings.mini_app_url,
            ),
        )
        return
    bounded_index = index % len(tasks)
    await _deliver_feed_card(
        message,
        context,
        tasks[bounded_index],
        viewer_user.id,
        bounded_index,
        len(tasks),
    )


async def _refresh_feed_card(
    message: Message,
    context: AppContext,
    *,
    viewer_user_id: int,
    task_id: int,
) -> None:
    async with session_scope(context.session_factory) as session:
        task = await get_public_feed_task(session, task_id)
    if task:
        await _deliver_feed_card(message, context, task, viewer_user_id, 0, 1)


async def _deliver_feed_card(
    message: Message,
    context: AppContext,
    task: GenerationTask,
    viewer_user_id: int,
    index: int,
    total: int,
) -> None:
    async with session_scope(context.session_factory) as session:
        row = await serialize_feed_task(session, task)
    caption = _feed_caption(row)
    keyboard = _feed_keyboard(task, viewer_user_id=viewer_user_id, index=index, total=total)
    media_url = str(task.result_urls[0]) if task.result_urls else ""
    with suppress(Exception):
        if generation_media_type(task) == "video":
            await message.answer_video(media_url, caption=caption[:1024], reply_markup=keyboard)
        else:
            await message.answer_photo(media_url, caption=caption[:1024], reply_markup=keyboard)
        return
    await message.answer(f"{caption}\n\n{escape(media_url)}", reply_markup=keyboard)


def _feed_caption(row: dict) -> str:
    model = escape(str(row.get("model_code") or "model"))
    author = str(row.get("author") or "BANANA user")
    return (
        f"<b>BANANA feed</b>\n"
        f"Автор: {escape(author)}\n"
        f"Модель: {model}\n"
        f"Лайки: {int(row.get('likes') or 0)} · Shares: {int(row.get('shares') or 0)}\n\n"
        "🔄 Нажми «Повторить» чтобы сделать похожее"
    )


def _feed_keyboard(
    task: GenerationTask, *, viewer_user_id: int, index: int, total: int
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text=f"Лайк {int(task.likes_count or 0)}", callback_data=f"feed:like:{task.id}")
    builder.button(
        text=f"Share {int(task.shares_count or 0)}", callback_data=f"feed:share:{task.id}"
    )
    repeat_text = "Повторить видео" if generation_media_type(task) == "video" else "Повторить фото"
    builder.button(text=repeat_text, callback_data=f"feed:repeat:{task.id}")
    if total > 1:
        builder.button(text="Следующая", callback_data=f"feed:next:{index + 1}")
    if task.user_id == viewer_user_id:
        builder.button(text="Снять с ленты", callback_data=f"feed:remove:{task.id}")
    builder.button(text="Главное меню", callback_data="menu:main")
    builder.adjust(2, 2, 1, 1)
    return builder.as_markup()


def _publish_confirm_keyboard(task_id: int) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(text="Опубликовать", callback_data=f"feed:publish:yes:{task_id}")
    nav_count = add_navigation_buttons(builder, back_callback="menu:feed")
    builder.adjust(1, nav_count)
    return builder.as_markup()


def _callback_int(value: str | None, prefix: str, *, default: int) -> int:
    if not value or not value.startswith(prefix):
        return default
    try:
        return int(value.removeprefix(prefix))
    except ValueError:
        return default


def setup(dispatcher: Dispatcher, context: AppContext) -> None:
    dispatcher.include_router(router)
