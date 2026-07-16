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
        likes, active = await like_feed_task(session, task_id=task_id, user_id=user.id)
    if likes is None:
        await callback.answer("Работа уже недоступна", show_alert=True)
        return
    await callback.answer("Лайк добавлен" if active else "Лайк снят")
    if callback.message:
        await _refresh_feed_card(callback.message, context, viewer_user_id=user.id, task_id=task_id)


@router.callback_query(F.data.startswith("feed:dislike:"))
async def dislike_feed_card(callback: CallbackQuery, context: AppContext) -> None:
    user = await ensure_user_for_callback(callback, context)
    task_id = _callback_int(callback.data, "feed:dislike:", default=0)
    async with session_scope(context.session_factory) as session:
        dislikes, active = await like_feed_task(session, task_id=-task_id, user_id=user.id)
    if dislikes is None:
        await callback.answer("Работа уже недоступна", show_alert=True)
        return
    await callback.answer("Дизлайк добавлен" if active else "Дизлайк снят")
    if callback.message:
        await _refresh_feed_card(callback.message, context, viewer_user_id=user.id, task_id=task_id)


@router.callback_query(F.data.startswith("feed:profile:"))
async def feed_author_profile(callback: CallbackQuery, context: AppContext) -> None:
    await ensure_user_for_callback(callback, context)
    task_id = _callback_int(callback.data, "feed:profile:", default=0)
    async with session_scope(context.session_factory) as session:
        task = await get_public_feed_task(session, task_id)
        row = await serialize_feed_task(session, task) if task else None
    if not row:
        await callback.answer("Профиль уже недоступен", show_alert=True)
        return
    profile = dict(row.get("author_profile") or {})
    text = (
        f"<b>{escape(str(profile.get('name') or row.get('author') or 'BANANA user'))}</b>\n\n"
        f"Публичных работ: <b>{int(profile.get('works') or 0)}</b>\n"
        f"Получено лайков: <b>{int(profile.get('likes') or 0)}</b>\n"
        f"Дизлайков: <b>{int(profile.get('dislikes') or 0)}</b>\n\n"
        "Откройте Mini App, чтобы посмотреть визуальную галерею автора."
    )
    if callback.message:
        await callback.message.answer(
            text,
            reply_markup=navigation_keyboard(back_callback="menu:feed"),
        )
    await callback.answer()


@router.callback_query(F.data.startswith("feed:share:"))
async def legacy_share_feed_card(callback: CallbackQuery, context: AppContext) -> None:
    """Keep stale keyboards safe without incrementing a fake share counter."""

    await ensure_user_for_callback(callback, context)
    await callback.answer("Используйте кнопку «Ссылка на пост»", show_alert=True)


@router.callback_query(F.data.startswith("feed:publish:confirm:"))
async def publish_confirm(callback: CallbackQuery, context: AppContext) -> None:
    await ensure_user_for_callback(callback, context)
    task_id = _callback_int(callback.data, "feed:publish:confirm:", default=0)
    if callback.message:
        await callback.message.answer(
            "Опубликовать работу в ленте?\n\n"
            "Другие пользователи увидят результат, модель и ваше публичное имя. Промпт останется скрыт.",
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
        "published": "Работа опубликована",
        "not_owner": "Можно публиковать только свои работы",
        "not_completed": "Генерация ещё не завершена",
        "no_result": "У работы нет результата",
        "foreign_source": "Повтор чужой работы нельзя публиковать как свою",
    }
    await callback.answer(texts.get(reason, "Не получилось опубликовать"), show_alert=not ok)
    if callback.message and ok:
        post_url = _feed_post_url(context, task_id)
        await callback.message.answer(
            f"Работа опубликована.\n\nСсылка на пост:\n{escape(post_url)}",
            reply_markup=navigation_keyboard(back_callback="menu:feed"),
        )


@router.callback_query(F.data.startswith("feed:remove:"))
async def remove_feed_card(callback: CallbackQuery, context: AppContext) -> None:
    user = await ensure_user_for_callback(callback, context)
    task_id = _callback_int(callback.data, "feed:remove:", default=0)
    async with session_scope(context.session_factory) as session:
        removed = await remove_task_from_feed(session, task_id=task_id, user_id=user.id)
    await callback.answer(
        "Работа снята с ленты" if removed else "Не получилось снять работу",
        show_alert=not removed,
    )


@router.callback_query(F.data.startswith("feed:repeat:"))
async def repeat_feed_card(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await ensure_user_for_callback(callback, context)
    task_id = _callback_int(callback.data, "feed:repeat:", default=0)
    async with session_scope(context.session_factory) as session:
        task = await get_public_feed_task(session, task_id)
        model = await get_model(session, task.model_code) if task else None
    if not task or not model:
        await callback.answer("Работа уже недоступна", show_alert=True)
        return

    await state.clear()
    payload = dict(task.input_payload or {})
    if generation_media_type(task) == "video":
        await state.set_state(MotionFlow.image)
        await state.update_data(
            model_code=task.model_code,
            duration=str(payload.get("duration") or "5"),
            mode=str(payload.get("mode") or "pro"),
            source_feed_task_id=task.id,
        )
        text = (
            "Модель и параметры сохранены. Промпт автора скрыт. "
            "Отправьте изображение, затем напишите собственный промпт для видео."
        )
        back = "menu:motion"
    else:
        await state.set_state(ImageFlow.reference_prompt)
        await state.update_data(
            model_code=task.model_code,
            aspect_ratio=payload.get("aspect_ratio") or DEFAULT_IMAGE_ASPECT_RATIO,
            resolution=payload.get("resolution") or "2K",
            source_feed_task_id=task.id,
        )
        text = (
            "Модель и формат сохранены. Промпт автора скрыт. "
            "Отправьте своё фото, затем напишите собственный промпт."
        )
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
            "В ленте пока нет работ.",
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
    keyboard = _feed_keyboard(
        task,
        viewer_user_id=viewer_user_id,
        index=index,
        total=total,
        dislikes=int(row.get("dislikes") or 0),
        post_url=_feed_post_url(context, task.id),
    )
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
    author = escape(str(row.get("author") or "Пользователь BANANA"))
    profile = dict(row.get("author_profile") or {})
    return (
        "<b>Лента BANANA</b>\n"
        f"{author} · {model}\n"
        f"❤️ <b>{int(row.get('likes') or 0)}</b> · "
        f"👎 <b>{int(row.get('dislikes') or 0)}</b> · "
        f"работ автора <b>{int(profile.get('works') or 0)}</b>\n\n"
        "🔒 Промпт автора скрыт. «Создать своё» перенесёт только модель и формат — "
        "текст нужно написать самостоятельно."
    )


def _feed_keyboard(
    task: GenerationTask,
    *,
    viewer_user_id: int,
    index: int,
    total: int,
    dislikes: int = 0,
    post_url: str | None = None,
) -> InlineKeyboardMarkup:
    builder = InlineKeyboardBuilder()
    builder.button(
        text=f"❤️ {int(task.likes_count or 0)}",
        callback_data=f"feed:like:{task.id}",
    )
    builder.button(
        text=f"👎 {int(dislikes or 0)}",
        callback_data=f"feed:dislike:{task.id}",
    )
    builder.button(text="Автор", callback_data=f"feed:profile:{task.id}")
    builder.button(text="Создать своё", callback_data=f"feed:repeat:{task.id}")
    if post_url:
        builder.button(text="Ссылка на пост", url=post_url)
    if total > 1:
        builder.button(text="Следующая", callback_data=f"feed:next:{index + 1}")
    if task.user_id == viewer_user_id:
        builder.button(text="Убрать из ленты", callback_data=f"feed:remove:{task.id}")
    builder.button(text="Главная", callback_data="menu:main")
    rows = [2, 2]
    if post_url:
        rows.append(1)
    if total > 1:
        rows.append(1)
    if task.user_id == viewer_user_id:
        rows.append(1)
    rows.append(1)
    builder.adjust(*rows)
    return builder.as_markup()


def _feed_post_url(context: AppContext, task_id: int) -> str:
    return f"{context.settings.mini_app_url}?post={int(task_id)}"


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
