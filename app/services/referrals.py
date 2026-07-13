from __future__ import annotations

from typing import Any

from aiogram import Bot
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import ReferralCodeAlias, User


def _base36(value: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    value = abs(int(value))
    if value == 0:
        return "0"
    result = ""
    while value:
        value, rest = divmod(value, 36)
        result = alphabet[rest] + result
    return result


def partner_code_for_telegram_id(telegram_id: int) -> str:
    return f"u{_base36(telegram_id)}"


def normalize_ref_code(value: str | None) -> str:
    normalized = str(value or "").strip()
    for prefix in ("ref_", "ref-"):
        if normalized.lower().startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    return normalized.strip().lower()


async def ensure_partner_code(session: AsyncSession, user: User) -> str:
    if not user.id:
        await session.flush()
    expected = partner_code_for_telegram_id(user.telegram_id)
    current = str(user.partner_code or "").strip().lower()
    if current and current != expected:
        existing_alias = await session.scalar(
            select(ReferralCodeAlias.id).where(func.lower(ReferralCodeAlias.code) == current)
        )
        if not existing_alias:
            session.add(ReferralCodeAlias(user_id=user.id, code=current))
    user.partner_code = expected
    return expected


async def _resolve_referrer(session: AsyncSession, normalized: str) -> User | None:
    referrer = await session.scalar(
        select(User).where(func.lower(User.partner_code) == normalized)
    )
    if referrer:
        return referrer

    alias_user_id = await session.scalar(
        select(ReferralCodeAlias.user_id).where(func.lower(ReferralCodeAlias.code) == normalized)
    )
    if alias_user_id:
        return await session.get(User, alias_user_id)

    if normalized.isdigit():
        return await session.scalar(select(User).where(User.telegram_id == int(normalized)))
    return None


async def _referral_would_create_cycle(
    session: AsyncSession,
    *,
    user_id: int,
    referrer_id: int,
) -> bool:
    current_id: int | None = referrer_id
    visited: set[int] = set()
    while current_id:
        if current_id == user_id or current_id in visited:
            return True
        visited.add(current_id)
        current = await session.get(User, current_id)
        current_id = current.referred_by_user_id if current else None
    return False


async def bind_referral(
    session: AsyncSession,
    *,
    user: User,
    ref_code: str | None,
) -> User | None:
    normalized = normalize_ref_code(ref_code)
    if not normalized:
        return None
    if not user.id:
        await session.flush()

    referrer = await _resolve_referrer(session, normalized)
    if not referrer or referrer.id == user.id or referrer.is_blocked:
        return None

    locked_users = list(
        await session.scalars(
            select(User)
            .where(User.id.in_(sorted({user.id, referrer.id})))
            .order_by(User.id)
            .with_for_update()
        )
    )
    by_id = {item.id: item for item in locked_users}
    locked_user = by_id.get(user.id)
    locked_referrer = by_id.get(referrer.id)
    if not locked_user or not locked_referrer:
        return None
    if locked_user.referred_by_user_id or locked_referrer.is_blocked:
        return None
    if await _referral_would_create_cycle(
        session,
        user_id=locked_user.id,
        referrer_id=locked_referrer.id,
    ):
        return None

    locked_user.referred_by_user_id = locked_referrer.id
    return locked_referrer


async def build_ref_link(bot: Bot | None, partner_code: str | None) -> str | None:
    if not bot or not partner_code:
        return None
    me = await bot.get_me()
    username = me.username
    if not username:
        return None
    return f"https://t.me/{username}?start=ref_{partner_code}"


async def disabled_increment_feed_share(session: AsyncSession, task_id: int) -> None:
    """Keep stale clients harmless without maintaining an artificial share counter."""

    del session, task_id
    return None


def install_repository_patches() -> None:
    """Install financial/referral implementations before plugins import repository symbols."""
    from app import repositories
    from app.services.financial_integrity import (
        apply_affiliate_commission,
        apply_package_snapshot_to_user,
        apply_package_to_user,
        package_is_user_visible,
        refund_task_credits,
    )

    for package in repositories.DEFAULT_PACKAGES:
        if bool(package.get("is_unlimited")):
            package["is_enabled"] = False

    patches: dict[str, Any] = {
        "normalize_ref_code": normalize_ref_code,
        "ensure_partner_code": ensure_partner_code,
        "bind_referral": bind_referral,
        "apply_affiliate_commission": apply_affiliate_commission,
        "apply_package_snapshot_to_user": apply_package_snapshot_to_user,
        "apply_package_to_user": apply_package_to_user,
        "package_is_user_visible": package_is_user_visible,
        "refund_task_credits": refund_task_credits,
        "increment_feed_share": disabled_increment_feed_share,
    }
    for name, implementation in patches.items():
        setattr(repositories, name, implementation)

    from app.services.financial_payment_patch import install_payment_patches
    from app.services.financial_tracker_patch import install_tracker_patches

    install_payment_patches()
    install_tracker_patches()
