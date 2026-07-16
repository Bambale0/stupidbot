from __future__ import annotations

from typing import Any

from aiogram.utils.keyboard import InlineKeyboardBuilder


def _legacy_feed_keyboard(
    task: Any,
    *,
    viewer_user_id: int,
    index: int,
    total: int,
    dislikes: int = 0,
    post_url: str | None = None,
):
    del post_url
    builder = InlineKeyboardBuilder()
    builder.button(text=f"❤️ {int(task.likes_count or 0)}", callback_data=f"feed:like:{task.id}")
    builder.button(text=f"👎 {int(dislikes or 0)}", callback_data=f"feed:dislike:{task.id}")
    builder.button(text="Автор", callback_data=f"feed:profile:{task.id}")
    repeat_text = "Повторить видео" if "video" in str(task.model_code) else "Повторить фото"
    builder.button(text=repeat_text, callback_data=f"feed:repeat:{task.id}")
    if total > 1:
        builder.button(text="Следующая", callback_data=f"feed:next:{index + 1}")
    if task.user_id == viewer_user_id:
        builder.button(text="Убрать из ленты", callback_data=f"feed:remove:{task.id}")
    builder.button(text="Главная", callback_data="menu:main")
    rows = [2, 2]
    if total > 1:
        rows.append(1)
    if task.user_id == viewer_user_id:
        rows.append(1)
    rows.append(1)
    builder.adjust(*rows)
    return builder.as_markup()


def install(adapter: Any) -> None:
    original_static_logic = adapter.LEGACY_STATIC_LOGIC

    def compatible_static_logic(regression: Any) -> None:
        current_feed_keyboard = adapter.legacy.feed_plugin._feed_keyboard
        adapter.legacy.feed_plugin._feed_keyboard = _legacy_feed_keyboard
        try:
            original_static_logic(regression)
        finally:
            adapter.legacy.feed_plugin._feed_keyboard = current_feed_keyboard

    adapter.LEGACY_STATIC_LOGIC = compatible_static_logic
