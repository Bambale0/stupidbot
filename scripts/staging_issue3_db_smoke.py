from __future__ import annotations

import asyncio
import hashlib
from datetime import timedelta
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

if __package__ in {None, ""}:
    from _bootstrap import add_project_root_to_path

    add_project_root_to_path()

from app.config import get_settings
from app.db import build_engine, session_scope
from app.models import (
    AffiliateLedgerEntry,
    CreditLedgerEntry,
    GenerationTask,
    Payment,
    ReferralCodeAlias,
    User,
)
from app.services.financial_credits import now_utc
from app.services.financial_tasks import reconcile_orphan_tasks
from app.services.referrals import (
    bind_referral,
    install_repository_patches,
    partner_code_for_telegram_id,
)
from app.services.tbank import TBankClient

install_repository_patches()

from app.services import payments as payment_service  # noqa: E402

REVERSAL_STATUSES = ("REFUNDED", "REVERSED", "CHARGEBACK")
TEST_TERMINAL = "staging-smoke-terminal"
TEST_PASSWORD = "staging-smoke-password"


def _token_value(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _signed_payload(
    *, order_id: str, payment_id: str, amount_kopecks: int, status: str
) -> dict[str, object]:
    payload: dict[str, object] = {
        "TerminalKey": TEST_TERMINAL,
        "OrderId": order_id,
        "PaymentId": payment_id,
        "Amount": amount_kopecks,
        "Success": True,
        "Status": status,
        "ErrorCode": "0",
    }
    token_payload = {**payload, "Password": TEST_PASSWORD}
    raw = "".join(_token_value(token_payload[key]) for key in sorted(token_payload))
    payload["Token"] = hashlib.sha256(raw.encode("utf-8")).hexdigest()
    return payload


async def _create_callback_case(
    factory: async_sessionmaker, *, suffix: str, index: int
) -> tuple[int, int, int, str, str]:
    referrer_tid = 8_900_000_000 + int(suffix[:6], 16) + index * 10
    buyer_tid = referrer_tid + 1
    order_id = f"staging-smoke-{suffix}-{index}"
    provider_payment_id = f"provider-{suffix}-{index}"
    async with session_scope(factory) as session:
        referrer = User(telegram_id=referrer_tid)
        buyer = User(telegram_id=buyer_tid)
        session.add_all([referrer, buyer])
        await session.flush()
        await session.refresh(referrer)
        await session.refresh(buyer)
        buyer.referred_by_user_id = referrer.id
        payment = Payment(
            user_id=buyer.id,
            order_id=order_id,
            provider_payment_id=provider_payment_id,
            amount_kopecks=10_000,
            status="created",
            raw_payload={
                "package_snapshot": {
                    "credits": 0,
                    "photo_credits": 10,
                    "video_credits": 0,
                    "is_unlimited": False,
                    "duration_days": None,
                    "title": "Staging smoke package",
                },
                "source": "staging_issue3_db_smoke",
            },
        )
        session.add(payment)
        await session.flush()
        return referrer.id, buyer.id, payment.id, order_id, provider_payment_id


async def _assert_callback_case(
    factory: async_sessionmaker,
    context: SimpleNamespace,
    *,
    suffix: str,
    index: int,
    reversal_status: str,
) -> None:
    referrer_id, buyer_id, payment_id, order_id, provider_payment_id = await _create_callback_case(
        factory, suffix=suffix, index=index
    )
    confirmed = _signed_payload(
        order_id=order_id,
        payment_id=provider_payment_id,
        amount_kopecks=10_000,
        status="CONFIRMED",
    )
    assert await payment_service.handle_tbank_notification(context, confirmed)
    assert await payment_service.handle_tbank_notification(context, confirmed)

    async with session_scope(factory) as session:
        buyer = await session.get(User, buyer_id, with_for_update=True)
        referrer = await session.get(User, referrer_id, with_for_update=True)
        payment = await session.get(Payment, payment_id, with_for_update=True)
        assert buyer and referrer and payment
        assert payment.status == "paid"
        assert buyer.photo_credits_balance == 10
        assert payment.affiliate_commission_kopecks == 3_000
        assert referrer.affiliate_balance_kopecks == 3_000
        buyer.photo_credits_balance = 4
        referrer.affiliate_balance_kopecks = 1_000

    reversal = _signed_payload(
        order_id=order_id,
        payment_id=provider_payment_id,
        amount_kopecks=10_000,
        status=reversal_status,
    )
    assert await payment_service.handle_tbank_notification(context, reversal)
    assert await payment_service.handle_tbank_notification(context, reversal)

    async with session_scope(factory) as session:
        buyer = await session.get(User, buyer_id)
        referrer = await session.get(User, referrer_id)
        payment = await session.get(Payment, payment_id)
        assert buyer and referrer and payment
        assert payment.status == "reversed"
        assert payment.affiliate_commission_reversed_kopecks == 3_000
        assert buyer.photo_credits_balance == 0
        assert buyer.photo_credit_debt == 6
        assert referrer.affiliate_balance_kopecks == 0
        assert referrer.affiliate_debt_kopecks == 2_000


async def _assert_referrals(factory: async_sessionmaker, suffix: str) -> None:
    async with session_scope(factory) as session:
        referrer = User(telegram_id=8_800_000_000 + int(suffix[:6], 16))
        current_buyer = User(telegram_id=referrer.telegram_id + 1)
        alias_buyer = User(telegram_id=referrer.telegram_id + 2)
        session.add_all([referrer, current_buyer, alias_buyer])
        await session.flush()
        await session.refresh(referrer)
        await session.refresh(current_buyer)
        await session.refresh(alias_buyer)
        expected = partner_code_for_telegram_id(referrer.telegram_id)
        assert referrer.partner_code == expected
        legacy_code = f"legacy-{suffix}"
        session.add(ReferralCodeAlias(user_id=referrer.id, code=legacy_code))
        await session.flush()
        assert (await bind_referral(session, user=current_buyer, ref_code=f"ref_{expected}")).id == referrer.id
        assert (await bind_referral(session, user=alias_buyer, ref_code=legacy_code)).id == referrer.id
        assert current_buyer.referred_by_user_id == referrer.id
        assert alias_buyer.referred_by_user_id == referrer.id


async def _assert_orphan(factory: async_sessionmaker, suffix: str) -> None:
    async with session_scope(factory) as session:
        user = User(telegram_id=8_700_000_000 + int(suffix[:6], 16))
        session.add(user)
        await session.flush()
        task = GenerationTask(
            user_id=user.id,
            model_code="staging-orphan-smoke",
            status="submitting",
            input_payload={"provider": "smoke", "credit_spend": {"photo": 3}},
            cost_credits=3,
            idempotency_key=f"staging-orphan:{suffix}",
        )
        session.add(task)
        await session.flush()
        task.updated_at = now_utc() - timedelta(minutes=10)
        await session.flush()
        task_id, user_id = task.id, user.id

    async with session_scope(factory) as session:
        first = await reconcile_orphan_tasks(session, timeout_seconds=60)
        second = await reconcile_orphan_tasks(session, timeout_seconds=60)
        assert [item.id for item in first] == [task_id]
        assert second == []

    async with session_scope(factory) as session:
        task = await session.get(GenerationTask, task_id)
        user = await session.get(User, user_id)
        assert task and user
        assert task.status == "fail" and task.finalized_at and task.refunded_at
        assert user.photo_credits_balance == 3
        ledger_count = int(
            await session.scalar(
                select(func.count()).select_from(CreditLedgerEntry).where(
                    CreditLedgerEntry.user_id == user_id,
                    CreditLedgerEntry.credit_type == "photo",
                    CreditLedgerEntry.balance_delta == 3,
                )
            )
            or 0
        )
        assert ledger_count == 1


async def amain() -> None:
    settings = get_settings()
    engine = build_engine(settings)
    assert engine.dialect.name == "postgresql", "staging smoke requires PostgreSQL"
    suffix = uuid4().hex[:10]
    try:
        async with engine.connect() as connection:
            outer = await connection.begin()
            factory = async_sessionmaker(
                bind=connection,
                expire_on_commit=False,
                autoflush=False,
                join_transaction_mode="create_savepoint",
            )
            context = SimpleNamespace(
                settings=settings,
                session_factory=factory,
                tbank=TBankClient(terminal_key=TEST_TERMINAL, password=TEST_PASSWORD),
                bot=None,
            )
            try:
                await _assert_referrals(factory, suffix)
                for index, status in enumerate(REVERSAL_STATUSES, start=1):
                    await _assert_callback_case(
                        factory,
                        context,
                        suffix=suffix,
                        index=index,
                        reversal_status=status,
                    )
                await _assert_orphan(factory, suffix)
                async with session_scope(factory) as session:
                    assert int(await session.scalar(select(func.count()).select_from(CreditLedgerEntry)) or 0) > 0
                    assert int(await session.scalar(select(func.count()).select_from(AffiliateLedgerEntry)) or 0) > 0
            finally:
                await outer.rollback()
    finally:
        await engine.dispose()
    print("staging DB smoke passed: referrals, callbacks, reversals, debt, orphan reconciliation")


if __name__ == "__main__":
    asyncio.run(amain())
