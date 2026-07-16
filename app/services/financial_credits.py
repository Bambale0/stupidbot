from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CreditPackage, GenerationTask, Payment, User

_BALANCE_FIELDS = {
    "common": ("credits_balance", "common_credit_debt"),
    "photo": ("photo_credits_balance", "photo_credit_debt"),
    "video": ("video_credits_balance", "video_credit_debt"),
}


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def positive_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def normalize_credit_type(value: Any) -> str:
    value = str(value or "common").lower()
    if value in {"image", "photo", "photos"}:
        return "photo"
    if value in {"video", "motion", "videos"}:
        return "video"
    return "common"


def package_is_user_visible(package: CreditPackage) -> bool:
    credits = (
        int(package.credits or 0)
        + int(package.photo_credits or 0)
        + int(package.video_credits or 0)
    )
    subscription_days = positive_int(package.duration_days) if package.is_unlimited else 0
    technical = str(package.code or "").startswith("scenario-package-") or str(
        package.title or ""
    ).startswith("Scenario Package")
    grants_value = credits > 0 or subscription_days > 0
    return bool(package.is_enabled) and grants_value and not technical


def grant_credit(user: User, kind: str, amount: int) -> None:
    amount = positive_int(amount)
    balance_field, debt_field = _BALANCE_FIELDS[normalize_credit_type(kind)]
    debt = positive_int(getattr(user, debt_field, 0))
    settled = min(debt, amount)
    setattr(user, debt_field, debt - settled)
    amount -= settled
    if amount:
        setattr(user, balance_field, positive_int(getattr(user, balance_field, 0)) + amount)


def reverse_credit(user: User, kind: str, amount: int) -> int:
    amount = positive_int(amount)
    balance_field, debt_field = _BALANCE_FIELDS[normalize_credit_type(kind)]
    balance = positive_int(getattr(user, balance_field, 0))
    deducted = min(balance, amount)
    setattr(user, balance_field, balance - deducted)
    debt = amount - deducted
    if debt:
        setattr(user, debt_field, positive_int(getattr(user, debt_field, 0)) + debt)
    return debt


def grant_affiliate_balance(user: User, amount_kopecks: int) -> int:
    """Restore affiliate funds without bypassing debt created by reversals."""

    amount = positive_int(amount_kopecks)
    debt = positive_int(user.affiliate_debt_kopecks)
    settled = min(debt, amount)
    user.affiliate_debt_kopecks = debt - settled
    available = amount - settled
    if available:
        user.affiliate_balance_kopecks = positive_int(user.affiliate_balance_kopecks) + available
    return available


async def apply_package_snapshot_to_user(
    session: AsyncSession, *, user: User, snapshot: dict[str, Any]
) -> None:
    if snapshot.get("is_unlimited"):
        days = positive_int(snapshot.get("duration_days"))
        if days:
            now = now_utc()
            base = user.unlimited_until if user.unlimited_until and user.unlimited_until > now else now
            user.unlimited_until = base + timedelta(days=days)
    grant_credit(user, "common", snapshot.get("credits", 0))
    grant_credit(user, "photo", snapshot.get("photo_credits", 0))
    grant_credit(user, "video", snapshot.get("video_credits", 0))
    await session.flush()


async def apply_package_to_user(
    session: AsyncSession, *, user: User, package: CreditPackage
) -> None:
    await apply_package_snapshot_to_user(
        session,
        user=user,
        snapshot={
            "credits": package.credits,
            "photo_credits": package.photo_credits,
            "video_credits": package.video_credits,
            "is_unlimited": package.is_unlimited,
            "duration_days": package.duration_days,
        },
    )


async def apply_affiliate_commission(
    session: AsyncSession, *, payment: Payment, buyer: User
) -> int:
    if payment.affiliate_commission_user_id or payment.affiliate_commission_kopecks or not buyer.referred_by_user_id:
        return 0
    referrer = await session.get(User, buyer.referred_by_user_id, with_for_update=True)
    if not referrer or referrer.is_blocked:
        return 0
    rate = max(0, min(10000, int(referrer.affiliate_commission_rate_bps or 3000)))
    commission = positive_int(payment.amount_kopecks) * rate // 10000
    if not commission:
        return 0
    payment.affiliate_commission_user_id = referrer.id
    payment.affiliate_commission_kopecks = commission
    referrer.affiliate_earned_kopecks = positive_int(referrer.affiliate_earned_kopecks) + commission
    grant_affiliate_balance(referrer, commission)
    return commission


def task_spend_allocation(task: GenerationTask) -> dict[str, int]:
    payload = dict(task.input_payload or {})
    raw = payload.get("credit_spend")
    if not isinstance(raw, dict):
        return {normalize_credit_type(payload.get("credit_type")): positive_int(task.cost_credits)}
    result: dict[str, int] = {}
    for kind, amount in raw.items():
        amount = positive_int(amount)
        if amount:
            normalized = normalize_credit_type(kind)
            result[normalized] = result.get(normalized, 0) + amount
    return result


async def refund_locked_task(session: AsyncSession, task: GenerationTask) -> bool:
    if task.refunded_at or positive_int(task.cost_credits) == 0:
        return False
    user = await session.get(User, task.user_id, with_for_update=True)
    if not user:
        return False
    for kind, amount in task_spend_allocation(task).items():
        grant_credit(user, kind, amount)
    task.refunded_at = now_utc()
    return True


async def refund_task_credits(session: AsyncSession, *, task: GenerationTask) -> None:
    if task.id:
        locked = await session.get(GenerationTask, task.id, with_for_update=True)
        if locked:
            await refund_locked_task(session, locked)


async def reverse_paid_payment(
    session: AsyncSession, *, payment: Payment, reason: str
) -> tuple[bool, dict[str, int]]:
    payment = await session.get(Payment, payment.id, with_for_update=True)
    if not payment or payment.status != "paid" or payment.reversed_at:
        return False, {}
    snapshot = dict(payment.raw_payload or {}).get("package_snapshot")
    if not isinstance(snapshot, dict):
        return False, {}
    user = await session.get(User, payment.user_id, with_for_update=True)
    if not user:
        return False, {}
    debts = {
        "common": reverse_credit(user, "common", snapshot.get("credits", 0)),
        "photo": reverse_credit(user, "photo", snapshot.get("photo_credits", 0)),
        "video": reverse_credit(user, "video", snapshot.get("video_credits", 0)),
    }
    if snapshot.get("is_unlimited") and user.unlimited_until:
        adjusted = user.unlimited_until - timedelta(days=positive_int(snapshot.get("duration_days")))
        user.unlimited_until = adjusted if adjusted > now_utc() else None
    commission = max(0, positive_int(payment.affiliate_commission_kopecks) - positive_int(payment.affiliate_commission_reversed_kopecks))
    if commission and payment.affiliate_commission_user_id:
        referrer = await session.get(User, payment.affiliate_commission_user_id, with_for_update=True)
        if referrer:
            available = positive_int(referrer.affiliate_balance_kopecks)
            deducted = min(available, commission)
            referrer.affiliate_balance_kopecks = available - deducted
            referrer.affiliate_debt_kopecks = positive_int(referrer.affiliate_debt_kopecks) + commission - deducted
            referrer.affiliate_earned_kopecks = max(0, positive_int(referrer.affiliate_earned_kopecks) - commission)
            debts["affiliate_kopecks"] = commission - deducted
        payment.affiliate_commission_reversed_kopecks += commission
    payment.status = "reversed"
    payment.reversed_at = now_utc()
    payment.reversal_reason = reason[:255]
    payment.raw_payload = {**dict(payment.raw_payload or {}), "reversal": {"reason": reason, "debts": debts}}
    await session.flush()
    return True, debts
