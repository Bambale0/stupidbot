from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot
from sqlalchemy import select

from app.context import AppContext
from app.db import session_scope
from app.models import Broadcast, User

logger = logging.getLogger(__name__)
BROADCAST_BATCH_SIZE = 100
BROADCAST_SEND_DELAY_SECONDS = 0.05

_active_tasks: set[asyncio.Task[Any]] = set()
_recovery_task: asyncio.Task[Any] | None = None


def install_admin_hardening_patch(context: AppContext) -> None:
    """Install bounded background broadcasts after the admin plugin is loaded."""

    from app.plugins.admin import plugin as admin_plugin

    if getattr(admin_plugin, "_admin_hardening_patch_installed", False):
        return

    admin_plugin._send_broadcast = send_broadcast_in_background
    admin_plugin._admin_hardening_patch_installed = True

    global _recovery_task
    if _recovery_task is None or _recovery_task.done():
        _recovery_task = asyncio.create_task(
            mark_stale_broadcasts_interrupted(context),
            name="admin-broadcast-recovery",
        )
        _track_task(_recovery_task)


def _track_task(task: asyncio.Task[Any]) -> None:
    _active_tasks.add(task)
    task.add_done_callback(_active_tasks.discard)


async def mark_stale_broadcasts_interrupted(context: AppContext) -> int:
    """Do not silently restart a partially delivered broadcast after process restart."""

    changed = 0
    async with session_scope(context.session_factory) as session:
        stale = list(
            await session.scalars(
                select(Broadcast).where(Broadcast.status.in_(["queued", "sending"]))
            )
        )
        for broadcast in stale:
            broadcast.status = "interrupted"
            changed += 1
    if changed:
        logger.warning("admin_broadcasts_marked_interrupted count=%d", changed)
    return changed


async def send_broadcast_in_background(
    context: AppContext,
    bot: Bot,
    broadcast_id: int,
) -> None:
    """Send a broadcast in bounded batches without blocking Telegram update handling."""

    current_task = asyncio.current_task()
    if current_task is not None:
        _track_task(current_task)

    sent = 0
    failed = 0
    last_user_id = 0
    text = ""

    try:
        async with session_scope(context.session_factory) as session:
            broadcast = await session.get(Broadcast, broadcast_id, with_for_update=True)
            if not broadcast:
                return
            text = str(broadcast.text or "")
            if not text:
                broadcast.status = "failed"
                return
            broadcast.status = "sending"
            broadcast.sent_count = 0
            broadcast.fail_count = 0

        while True:
            async with session_scope(context.session_factory) as session:
                rows = list(
                    (
                        await session.execute(
                            select(User.id, User.telegram_id)
                            .where(
                                User.is_blocked.is_(False),
                                User.id > last_user_id,
                            )
                            .order_by(User.id)
                            .limit(BROADCAST_BATCH_SIZE)
                        )
                    ).all()
                )

            if not rows:
                break

            for user_id, telegram_id in rows:
                last_user_id = int(user_id)
                try:
                    await bot.send_message(int(telegram_id), text, parse_mode=None)
                    sent += 1
                except asyncio.CancelledError:
                    raise
                except Exception:
                    failed += 1
                    logger.info(
                        "admin_broadcast_recipient_failed broadcast_id=%s user_id=%s",
                        broadcast_id,
                        user_id,
                    )
                await asyncio.sleep(BROADCAST_SEND_DELAY_SECONDS)

            await _update_broadcast_progress(
                context,
                broadcast_id=broadcast_id,
                status="sending",
                sent=sent,
                failed=failed,
            )
            await asyncio.sleep(0)

        await _update_broadcast_progress(
            context,
            broadcast_id=broadcast_id,
            status="sent",
            sent=sent,
            failed=failed,
            finished=True,
        )
        logger.info(
            "admin_broadcast_completed broadcast_id=%s sent=%s failed=%s",
            broadcast_id,
            sent,
            failed,
        )
    except asyncio.CancelledError:
        await _update_broadcast_progress(
            context,
            broadcast_id=broadcast_id,
            status="interrupted",
            sent=sent,
            failed=failed,
        )
        raise
    except Exception:
        logger.exception(
            "admin_broadcast_failed broadcast_id=%s sent=%s failed=%s",
            broadcast_id,
            sent,
            failed,
        )
        await _update_broadcast_progress(
            context,
            broadcast_id=broadcast_id,
            status="failed",
            sent=sent,
            failed=failed,
        )


async def _update_broadcast_progress(
    context: AppContext,
    *,
    broadcast_id: int,
    status: str,
    sent: int,
    failed: int,
    finished: bool = False,
) -> None:
    async with session_scope(context.session_factory) as session:
        broadcast = await session.get(Broadcast, broadcast_id, with_for_update=True)
        if not broadcast:
            return
        broadcast.status = status
        broadcast.sent_count = max(0, int(sent))
        broadcast.fail_count = max(0, int(failed))
        if finished:
            broadcast.sent_at = datetime.now(timezone.utc)


async def shutdown_admin_background_tasks() -> None:
    tasks = [task for task in _active_tasks if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
