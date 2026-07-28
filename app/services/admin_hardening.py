from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from aiogram import Bot
from sqlalchemy import select

from app.context import AppContext
from app.db import session_scope
from app.models import BotSetting, Broadcast, User

logger = logging.getLogger(__name__)
BROADCAST_BATCH_SIZE = 100
BROADCAST_SEND_DELAY_SECONDS = 0.05
BROADCAST_FAILURE_LIMIT = 100

_active_tasks: set[asyncio.Task[Any]] = set()
_broadcast_tasks: dict[int, asyncio.Task[Any]] = {}
_recovery_task: asyncio.Task[Any] | None = None


def install_admin_hardening_patch(context: AppContext) -> None:
    """Install bounded, resumable background broadcasts after the admin plugin is loaded."""

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


def _broadcast_state_key(broadcast_id: int) -> str:
    return f"broadcast_runtime:{int(broadcast_id)}"


async def _runtime_state(session: Any, broadcast_id: int) -> dict[str, Any]:
    setting = await session.get(BotSetting, _broadcast_state_key(broadcast_id))
    value = setting.value if setting and isinstance(setting.value, dict) else {}
    failures = value.get("failures")
    return {
        "last_user_id": max(0, int(value.get("last_user_id") or 0)),
        "sent": max(0, int(value.get("sent") or 0)),
        "failed": max(0, int(value.get("failed") or 0)),
        "attempts": max(0, int(value.get("attempts") or 0)),
        "failures": list(failures) if isinstance(failures, list) else [],
    }


async def _write_runtime_state(
    session: Any,
    *,
    broadcast_id: int,
    last_user_id: int,
    sent: int,
    failed: int,
    attempts: int,
    failures: list[dict[str, Any]],
) -> None:
    key = _broadcast_state_key(broadcast_id)
    setting = await session.get(BotSetting, key)
    value = {
        "last_user_id": max(0, int(last_user_id)),
        "sent": max(0, int(sent)),
        "failed": max(0, int(failed)),
        "attempts": max(0, int(attempts)),
        "failures": list(failures[-BROADCAST_FAILURE_LIMIT:]),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    if setting:
        setting.value = value
    else:
        session.add(
            BotSetting(
                key=key,
                value=value,
                description=f"Runtime cursor and failures for broadcast {broadcast_id}",
            )
        )


async def broadcast_runtime_state(context: AppContext, broadcast_id: int) -> dict[str, Any]:
    async with session_scope(context.session_factory) as session:
        return await _runtime_state(session, broadcast_id)


def broadcast_task_is_active(broadcast_id: int) -> bool:
    task = _broadcast_tasks.get(int(broadcast_id))
    return bool(task and not task.done())


def broadcast_runtime_metrics() -> dict[str, int]:
    active = sum(1 for task in _broadcast_tasks.values() if not task.done())
    return {
        "active_broadcasts": active,
        "background_tasks": sum(1 for task in _active_tasks if not task.done()),
    }


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


def start_broadcast_task(context: AppContext, bot: Bot, broadcast_id: int) -> asyncio.Task[Any] | None:
    broadcast_id = int(broadcast_id)
    current = _broadcast_tasks.get(broadcast_id)
    if current and not current.done():
        return None
    task = asyncio.create_task(
        send_broadcast_in_background(context, bot, broadcast_id),
        name=f"broadcast-{broadcast_id}",
    )
    _broadcast_tasks[broadcast_id] = task
    _track_task(task)

    def cleanup(completed: asyncio.Task[Any]) -> None:
        if _broadcast_tasks.get(broadcast_id) is completed:
            _broadcast_tasks.pop(broadcast_id, None)

    task.add_done_callback(cleanup)
    return task


async def restart_broadcast(context: AppContext, bot: Bot, broadcast_id: int) -> tuple[bool, str]:
    broadcast_id = int(broadcast_id)
    if broadcast_task_is_active(broadcast_id):
        return False, "Рассылка уже выполняется."
    async with session_scope(context.session_factory) as session:
        broadcast = await session.get(Broadcast, broadcast_id, with_for_update=True)
        if not broadcast:
            return False, "Рассылка не найдена."
        if broadcast.status == "sent":
            return False, "Рассылка уже завершена."
        if broadcast.status not in {"interrupted", "failed", "queued", "sending"}:
            return False, f"Нельзя продолжить рассылку со статусом {broadcast.status}."
        broadcast.status = "queued"
    task = start_broadcast_task(context, bot, broadcast_id)
    if not task:
        return False, "Рассылка уже выполняется."
    return True, "Рассылка продолжена с сохранённого получателя."


async def send_broadcast_in_background(
    context: AppContext,
    bot: Bot,
    broadcast_id: int,
) -> None:
    """Send a broadcast in bounded batches and persist a safe resume cursor."""

    current_task = asyncio.current_task()
    if current_task is not None:
        _track_task(current_task)
        _broadcast_tasks[int(broadcast_id)] = current_task

    sent = 0
    failed = 0
    last_user_id = 0
    attempts = 0
    failures: list[dict[str, Any]] = []
    text = ""

    try:
        async with session_scope(context.session_factory) as session:
            broadcast = await session.get(Broadcast, broadcast_id, with_for_update=True)
            if not broadcast:
                return
            if broadcast.status == "sent":
                return
            text = str(broadcast.text or "")
            if not text:
                broadcast.status = "failed"
                return
            state = await _runtime_state(session, broadcast_id)
            sent = max(int(broadcast.sent_count or 0), state["sent"])
            failed = max(int(broadcast.fail_count or 0), state["failed"])
            last_user_id = state["last_user_id"]
            attempts = state["attempts"] + 1
            failures = state["failures"]
            broadcast.status = "sending"
            await _write_runtime_state(
                session,
                broadcast_id=broadcast_id,
                last_user_id=last_user_id,
                sent=sent,
                failed=failed,
                attempts=attempts,
                failures=failures,
            )

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
                except Exception as exc:
                    failed += 1
                    failures.append(
                        {
                            "user_id": int(user_id),
                            "telegram_id": int(telegram_id),
                            "error": type(exc).__name__,
                            "at": datetime.now(timezone.utc).isoformat(),
                        }
                    )
                    failures = failures[-BROADCAST_FAILURE_LIMIT:]
                    logger.info(
                        "admin_broadcast_recipient_failed broadcast_id=%s user_id=%s error=%s",
                        broadcast_id,
                        user_id,
                        type(exc).__name__,
                    )
                await asyncio.sleep(BROADCAST_SEND_DELAY_SECONDS)

            await _update_broadcast_progress(
                context,
                broadcast_id=broadcast_id,
                status="sending",
                sent=sent,
                failed=failed,
                last_user_id=last_user_id,
                attempts=attempts,
                failures=failures,
            )
            await asyncio.sleep(0)

        await _update_broadcast_progress(
            context,
            broadcast_id=broadcast_id,
            status="sent",
            sent=sent,
            failed=failed,
            last_user_id=last_user_id,
            attempts=attempts,
            failures=failures,
            finished=True,
        )
        logger.info(
            "admin_broadcast_completed broadcast_id=%s sent=%s failed=%s attempts=%s",
            broadcast_id,
            sent,
            failed,
            attempts,
        )
    except asyncio.CancelledError:
        await _update_broadcast_progress(
            context,
            broadcast_id=broadcast_id,
            status="interrupted",
            sent=sent,
            failed=failed,
            last_user_id=last_user_id,
            attempts=attempts,
            failures=failures,
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
            last_user_id=last_user_id,
            attempts=attempts,
            failures=failures,
        )


async def _update_broadcast_progress(
    context: AppContext,
    *,
    broadcast_id: int,
    status: str,
    sent: int,
    failed: int,
    last_user_id: int,
    attempts: int,
    failures: list[dict[str, Any]],
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
        await _write_runtime_state(
            session,
            broadcast_id=broadcast_id,
            last_user_id=last_user_id,
            sent=sent,
            failed=failed,
            attempts=attempts,
            failures=failures,
        )


async def shutdown_admin_background_tasks() -> None:
    tasks = [task for task in _active_tasks if not task.done()]
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)
