from __future__ import annotations

from decimal import Decimal
from types import SimpleNamespace

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app import db as app_db
from app.models import Broadcast, CreditPackage, Payment, User
from app.plugins.admin import plugin as admin_plugin
from app.services import admin_hardening
from app.services.billing_catalog import _is_legacy_unlimited_disable


class FakeBroadcastBot:
    def __init__(self, *, fail_chat_ids: set[int] | None = None) -> None:
        self.fail_chat_ids = set(fail_chat_ids or set())
        self.sent_chat_ids: list[int] = []

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        parse_mode: str | None = None,
        **_: object,
    ) -> None:
        assert text
        assert parse_mode is None
        chat_id = int(chat_id)
        self.sent_chat_ids.append(chat_id)
        if chat_id in self.fail_chat_ids:
            raise RuntimeError("simulated Telegram delivery failure")


async def run_admin_operations_regression(
    session: AsyncSession,
    session_factory: async_sessionmaker[AsyncSession],
    suffix: str,
) -> None:
    assert not any(
        _is_legacy_unlimited_disable(statement)
        for statement in app_db.SCHEMA_COMPAT_SQL
    ), "init_db must not disable valid paid subscriptions on every restart"

    admin = User(
        telegram_id=int(f"971{suffix}", 16),
        is_admin=True,
        free_photo_generations_remaining=0,
    )
    customer = User(
        telegram_id=int(f"972{suffix}", 16),
        free_photo_generations_remaining=0,
    )
    blocked = User(
        telegram_id=int(f"973{suffix}", 16),
        is_blocked=True,
        free_photo_generations_remaining=0,
    )
    session.add_all([admin, customer, blocked])
    await session.flush()

    credit_package = CreditPackage(
        code=f"admin-credit-{suffix}",
        title="Admin credit package",
        credits=7,
        price_rub=Decimal("700.00"),
        is_enabled=True,
        position=900,
    )
    subscription_package = CreditPackage(
        code=f"admin-subscription-{suffix}",
        title="Admin subscription",
        price_rub=Decimal("1500.00"),
        is_unlimited=True,
        duration_days=30,
        is_enabled=True,
        position=901,
    )
    session.add_all([credit_package, subscription_package])
    await session.flush()

    credit_payment = Payment(
        user_id=customer.id,
        package_id=credit_package.id,
        provider="manual",
        order_id=f"admin-credit-payment-{suffix}",
        amount_kopecks=70_000,
        status="manual_pending",
    )
    subscription_payment = Payment(
        user_id=customer.id,
        package_id=subscription_package.id,
        provider="manual",
        order_id=f"admin-subscription-payment-{suffix}",
        amount_kopecks=150_000,
        status="manual_pending",
        raw_payload={
            "package_snapshot": {
                "title": subscription_package.title,
                "credits": 0,
                "photo_credits": 0,
                "video_credits": 0,
                "is_unlimited": True,
                "duration_days": 30,
            }
        },
    )
    session.add_all([credit_payment, subscription_payment])
    await session.flush()

    credit_result = await admin_plugin._mark_payment_paid(session, credit_payment)
    assert credit_result.ok, credit_result.admin_text
    assert credit_payment.status == "paid"
    assert customer.credits_balance == 7

    duplicate_credit_result = await admin_plugin._mark_payment_paid(session, credit_payment)
    assert not duplicate_credit_result.ok
    assert customer.credits_balance == 7

    subscription_result = await admin_plugin._mark_payment_paid(
        session,
        subscription_payment,
    )
    assert subscription_result.ok, subscription_result.admin_text
    assert subscription_payment.status == "paid"
    subscription_until = customer.unlimited_until
    assert subscription_until is not None
    assert "Безлимит активен" in str(subscription_result.notify_text)

    duplicate_subscription_result = await admin_plugin._mark_payment_paid(
        session,
        subscription_payment,
    )
    assert not duplicate_subscription_result.ok
    assert customer.unlimited_until == subscription_until

    package_view, package_keyboard = admin_plugin._admin_packages_view(
        [credit_package, subscription_package]
    )
    assert "Admin credit package" in package_view
    assert "Admin subscription" in package_view
    assert package_keyboard.inline_keyboard

    admin_callbacks = {
        button.callback_data
        for row in admin_plugin._admin_keyboard().inline_keyboard
        for button in row
        if button.callback_data
    }
    for expected in {
        "admin:stats",
        "admin:users",
        "admin:payments",
        "admin:packages",
        "admin:referrals",
        "admin:withdrawals",
        "admin:broadcast",
    }:
        assert expected in admin_callbacks

    broadcast = Broadcast(
        created_by_user_id=admin.id,
        title=f"Admin regression {suffix}",
        text="Background broadcast regression",
        status="sending",
    )
    session.add(broadcast)
    await session.flush()

    expected_recipients = int(
        await session.scalar(
            select(func.count()).select_from(User).where(User.is_blocked.is_(False))
        )
        or 0
    )
    fake_bot = FakeBroadcastBot(fail_chat_ids={customer.telegram_id})
    context = SimpleNamespace(session_factory=session_factory)
    original_delay = admin_hardening.BROADCAST_SEND_DELAY_SECONDS
    admin_hardening.BROADCAST_SEND_DELAY_SECONDS = 0
    try:
        await admin_hardening.send_broadcast_in_background(
            context,
            fake_bot,
            broadcast.id,
        )
    finally:
        admin_hardening.BROADCAST_SEND_DELAY_SECONDS = original_delay

    await session.refresh(broadcast)
    assert broadcast.status == "sent"
    assert broadcast.sent_count == expected_recipients - 1
    assert broadcast.fail_count == 1
    assert blocked.telegram_id not in fake_bot.sent_chat_ids
    assert customer.telegram_id in fake_bot.sent_chat_ids

    stale_broadcast = Broadcast(
        created_by_user_id=admin.id,
        title=f"Stale admin regression {suffix}",
        text="Interrupted broadcast",
        status="sending",
    )
    session.add(stale_broadcast)
    await session.flush()
    changed = await admin_hardening.mark_stale_broadcasts_interrupted(context)
    assert changed >= 1
    await session.refresh(stale_broadcast)
    assert stale_broadcast.status == "interrupted"
