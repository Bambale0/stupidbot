from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GenerationModel, GenerationTask, Payment, User
from app.repositories import refund_task_credits
from app.services.financial_integrity import finalize_generation_task, reverse_paid_payment
from app.services.referrals import partner_code_for_telegram_id


async def run_core(session: AsyncSession, suffix: str) -> dict[str, object]:
    referrer = User(
        telegram_id=int(f"71{suffix}", 16),
        affiliate_balance_kopecks=3000,
        affiliate_earned_kopecks=3000,
    )
    buyer = User(
        telegram_id=int(f"72{suffix}", 16),
        photo_credits_balance=10,
    )
    refund_user = User(telegram_id=int(f"73{suffix}", 16))
    session.add_all([referrer, buyer, refund_user])
    await session.flush()
    await session.refresh(buyer)
    assert buyer.partner_code == partner_code_for_telegram_id(buyer.telegram_id)
    buyer.referred_by_user_id = referrer.id

    model = GenerationModel(
        code=f"financial-{suffix}",
        title="Financial",
        category="image",
        price_credits=2,
        config={"provider_cost_kopecks": 250},
    )
    session.add(model)
    await session.flush()

    task = GenerationTask(
        user_id=refund_user.id,
        model_code=model.code,
        status="generating",
        input_payload={
            "credit_type": "photo",
            "credit_spend": {"photo": 3, "common": 2},
        },
        cost_credits=5,
        chat_id=7001,
        message_id=8001,
    )
    session.add(task)
    await session.flush()
    await refund_task_credits(session, task=task)
    await refund_task_credits(session, task=task)
    await session.flush()
    assert (refund_user.photo_credits_balance, refund_user.credits_balance) == (3, 2)
    assert task.refunded_at is not None

    atomic_user = User(telegram_id=int(f"74{suffix}", 16))
    session.add(atomic_user)
    await session.flush()
    atomic_task = GenerationTask(
        user_id=atomic_user.id,
        model_code=model.code,
        status="submitted",
        input_payload={"credit_type": "photo", "credit_spend": {"photo": 2}},
        cost_credits=2,
        chat_id=7002,
        message_id=8002,
    )
    session.add(atomic_task)
    await session.flush()
    _, changed_first = await finalize_generation_task(
        session, task_id=atomic_task.id, status="fail", error_message="test"
    )
    _, changed_second = await finalize_generation_task(
        session, task_id=atomic_task.id, status="fail", error_message="duplicate"
    )
    assert changed_first and not changed_second
    assert atomic_user.photo_credits_balance == 2

    paid = Payment(
        user_id=buyer.id,
        order_id=f"reversal-{suffix}",
        amount_kopecks=10000,
        status="paid",
        affiliate_commission_user_id=referrer.id,
        affiliate_commission_kopecks=3000,
        raw_payload={
            "package_snapshot": {
                "credits": 0,
                "photo_credits": 10,
                "video_credits": 0,
                "is_unlimited": False,
            }
        },
    )
    session.add(paid)
    await session.flush()
    reversed_ok, debts = await reverse_paid_payment(session, payment=paid, reason="test")
    repeated_ok, _ = await reverse_paid_payment(session, payment=paid, reason="again")
    assert reversed_ok and not repeated_ok and debts["photo"] == 0
    assert buyer.photo_credits_balance == 0
    assert referrer.affiliate_balance_kopecks == 0
    assert paid.status == "reversed"

    debt_user = User(telegram_id=int(f"75{suffix}", 16), photo_credits_balance=2)
    session.add(debt_user)
    await session.flush()
    debt_payment = Payment(
        user_id=debt_user.id,
        order_id=f"debt-{suffix}",
        amount_kopecks=10000,
        status="paid",
        raw_payload={
            "package_snapshot": {
                "credits": 0,
                "photo_credits": 10,
                "video_credits": 0,
                "is_unlimited": False,
            }
        },
    )
    session.add(debt_payment)
    await session.flush()
    ok, debt = await reverse_paid_payment(session, payment=debt_payment, reason="spent")
    assert ok and debt["photo"] == 8
    assert debt_user.photo_credits_balance == 0
    assert debt_user.photo_credit_debt == 8
    return {"buyer": buyer, "model": model}
