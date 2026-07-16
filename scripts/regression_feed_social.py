from __future__ import annotations

from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app import repositories
from app.models import GenerationTask, User
from app.plugins.feed import plugin as feed_plugin
from app.plugins.ux import plugin as ux_plugin
from app.services.feed_social import FeedDislike, feed_dislike_count


async def run_feed_social_regression(session: AsyncSession, suffix: str) -> None:
    author = User(
        telegram_id=int(f"a1{suffix}", 16),
        username=f"creator_{suffix}",
        first_name="Creator",
        free_photo_generations_remaining=0,
    )
    viewer = User(
        telegram_id=int(f"a2{suffix}", 16),
        free_photo_generations_remaining=0,
    )
    second_viewer = User(
        telegram_id=int(f"a3{suffix}", 16),
        free_photo_generations_remaining=0,
    )
    session.add_all([author, viewer, second_viewer])
    await session.flush()

    first = GenerationTask(
        user_id=author.id,
        model_code="nano-banana",
        status="success",
        prompt="Neon community portrait",
        result_urls=["https://example.com/community-first.webp"],
        input_payload={"aspect_ratio": "4:5"},
        is_public_feed=True,
        feed_status="approved",
        likes_count=0,
    )
    second = GenerationTask(
        user_id=author.id,
        model_code="nano-banana-2",
        status="success",
        prompt="Editorial community cover",
        result_urls=["https://example.com/community-second.webp"],
        input_payload={"aspect_ratio": "16:9"},
        is_public_feed=True,
        feed_status="approved",
        likes_count=1,
    )
    hidden = GenerationTask(
        user_id=author.id,
        model_code="nano-banana",
        status="success",
        result_urls=["https://example.com/hidden.webp"],
        is_public_feed=False,
        feed_status="hidden",
    )
    session.add_all([first, second, hidden])
    await session.flush()

    likes, active = await repositories.like_feed_task(
        session,
        task_id=first.id,
        user_id=viewer.id,
    )
    assert (likes, active) == (1, True)

    likes, active = await repositories.like_feed_task(
        session,
        task_id=first.id,
        user_id=viewer.id,
    )
    assert (likes, active) == (0, False), "second click must remove the like"

    dislikes, active = await repositories.like_feed_task(
        session,
        task_id=-first.id,
        user_id=viewer.id,
    )
    assert (dislikes, active) == (1, True)

    likes, active = await repositories.like_feed_task(
        session,
        task_id=first.id,
        user_id=viewer.id,
    )
    assert (likes, active) == (1, True)
    assert await feed_dislike_count(session, first.id) == 0, (
        "liking a disliked work must remove the dislike"
    )

    dislikes, active = await repositories.like_feed_task(
        session,
        task_id=-first.id,
        user_id=second_viewer.id,
    )
    assert (dislikes, active) == (1, True)
    assert first.likes_count == 1

    row = await repositories.serialize_feed_task(session, first)
    assert row["author"] == f"@creator_{suffix}"
    assert row["author_key"]
    assert row["author_profile"]["works"] == 2
    assert row["author_profile"]["likes"] == 2
    assert row["author_profile"]["dislikes"] == 1
    assert row["dislikes"] == 1
    assert "prompt" not in row, "author prompt must never enter the public feed payload"

    stats_cache = session.info.get("feed_author_stats")
    assert isinstance(stats_cache, dict)
    assert author.id in stats_cache
    stats_cache[author.id]["works"] = 77
    second_row = await repositories.serialize_feed_task(session, second)
    assert second_row["author_profile"]["works"] == 77, (
        "creator aggregates must be reused within one feed serialization session"
    )
    assert "prompt" not in second_row

    ordered = await repositories.get_feed_tasks(session, limit=10)
    assert ordered[0].id == second.id, (
        "a dislike must reduce ranking score instead of being ignored"
    )
    assert hidden.id not in {task.id for task in ordered}

    dislike_row = await session.scalar(
        select(FeedDislike).where(
            FeedDislike.user_id == second_viewer.id,
            FeedDislike.task_id == first.id,
        )
    )
    assert dislike_row is not None

    post_url = f"https://example.com/miniapp/?post={first.id}"
    keyboard = feed_plugin._feed_keyboard(
        first,
        viewer_user_id=viewer.id,
        index=0,
        total=2,
        dislikes=1,
        post_url=post_url,
    )
    callbacks = {
        button.callback_data
        for row_buttons in keyboard.inline_keyboard
        for button in row_buttons
        if button.callback_data
    }
    urls = {
        button.url
        for row_buttons in keyboard.inline_keyboard
        for button in row_buttons
        if button.url
    }
    assert f"feed:like:{first.id}" in callbacks
    assert f"feed:dislike:{first.id}" in callbacks
    assert f"feed:profile:{first.id}" in callbacks
    assert f"feed:repeat:{first.id}" in callbacks
    assert post_url in urls

    admin_callbacks = {callback for _, callback in ux_plugin.ADMIN_HOME_BUTTONS}
    assert "admin:tariff:add" in admin_callbacks
    catalog_callbacks = {
        callback for _, callback in ux_plugin.ADMIN_SECTIONS["admin:ux:catalog"][2]
    }
    assert "admin:tariff:add" in catalog_callbacks
    assert "admin:packages" in catalog_callbacks

    project_root = Path(__file__).resolve().parents[1]
    feed_js = (project_root / "app/static/miniapp/assets/feed-experience.js").read_text(
        encoding="utf-8"
    )
    feed_css = (project_root / "app/static/miniapp/assets/feed-experience.css").read_text(
        encoding="utf-8"
    )
    post_js = (project_root / "app/static/miniapp/assets/feed-posts.js").read_text(
        encoding="utf-8"
    )
    post_css = (project_root / "app/static/miniapp/assets/feed-posts.css").read_text(
        encoding="utf-8"
    )
    feed_plugin_source = (project_root / "app/plugins/feed/plugin.py").read_text(encoding="utf-8")
    miniapp_index = (project_root / "app/static/miniapp/index.html").read_text(
        encoding="utf-8"
    )
    for contract in (
        "community-mosaic",
        "data-community-reaction",
        "community-profile-page",
        "data-feed-repeat",
        "FEED_CACHE_TTL_MS",
        'data-action="refresh-feed"',
        "loadCommunityFeed();",
    ):
        assert contract in feed_js
    for contract in (
        ".community-mosaic",
        ".community-card.is-spotlight",
        ".community-profile-cover",
        ".community-reaction.is-dislike",
    ):
        assert contract in feed_css
    for contract in (
        "canonicalPostUrl",
        "data-community-post-link",
        "community-protected-prompt",
        "URLSearchParams",
        'searchParams.set("post"',
        "Промпт автора скрыт и защищён",
    ):
        assert contract in post_js
    for contract in (
        ".community-permalink-bar",
        ".community-protected-prompt",
        ".community-link-button",
        ".community-card.is-permalink-post",
    ):
        assert contract in post_css
    assert "prompt=task.prompt" not in feed_plugin_source
    assert "Промпт автора скрыт" in feed_plugin_source
    assert "Ссылка на пост" in feed_plugin_source
    assert "feed-experience.css" in miniapp_index
    assert "feed-experience.js" in miniapp_index
    assert "feed-posts.css" in miniapp_index
    assert "feed-posts.js" in miniapp_index
