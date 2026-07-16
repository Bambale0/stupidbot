from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import func, select
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    AffiliateLedgerEntry,
    CreditLedgerEntry,
    CreditPackage,
    GenerationModel,
    GenerationTask,
    ProviderCostEntry,
    User,
)
from app.services import payments as payment_service
from app.services.financial_integrity import package_is_user_visible, record_task_financials
from app.services.financial_settings import photo_credit_value_kopecks
from app.services.partial_refunds import (
    PartialRefundPolicyError,
    cumulative_reversal_target,
    incremental_reversal_delta,
)


async def custom_sales_are_disabled() -> None:
    try:
        await payment_service.create_custom_credit_payment(
            SimpleNamespace(), user_id=1, credits=1
        )
    except payment_service.PaymentCreditAmountInvalid as exc:
        assert str(exc) == "custom_credit_sales_disabled"
    else:
        raise AssertionError("custom universal-credit sales are enabled")


def partial_refund_policy_is_deterministic() -> None:
    payment = 390
    grant = 10

    first_target = cumulative_reversal_target(grant, 39, payment)
    assert first_target == 1
    assert incremental_reversal_delta(grant, 0, 39, payment) == 1
    assert incremental_reversal_delta(grant, first_target, 39, payment) == 0

    second_target = cumulative_reversal_target(grant, 129, payment)
    assert second_target == 3
    assert incremental_reversal_delta(grant, first_target, 129, payment) == 2

    commission_target = cumulative_reversal_target(3000, 3333, 10000)
    assert commission_target == 999

    assert cumulative_reversal_target(grant, payment, payment) == grant
    assert cumulative_reversal_target(grant, payment - 1, payment, force_full=True) == grant
    assert incremental_reversal_delta(grant, second_target, payment, payment) == 7
    assert cumulative_reversal_target(grant, payment * 2, payment) == grant
    assert cumulative_reversal_target(grant, -100, payment) == 0

    try:
        cumulative_reversal_target(grant, 1, 0)
    except PartialRefundPolicyError:
        pass
    else:
        raise AssertionError("zero payment amount was accepted by partial refund policy")


async def run_guards(
    session: AsyncSession,
    settings: object,
    suffix: str,
    context: dict[str, object],
) -> None:
    partial_refund_policy_is_deterministic()

    buyer = context["buyer"]
    model = context["model"]
    assert isinstance(buyer, User)
    assert isinstance(model, GenerationModel)

    success = GenerationTask(
        user_id=buyer.id,
        model_code=model.code,
        status="success",
        input_payload={"provider": "comet", "provider_model": "test"},
        result_urls=["https://example.com/result.jpg"],
        cost_credits=2,
        chat_id=7003,
        message_id=8003,
    )
    session.add(success)
    await session.flush()
    await record_task_financials(session, task_id=success.id, settings=settings)
    expected_revenue = 2 * photo_credit_value_kopecks(settings)
    assert success.provider_cost_kopecks == 250
    assert success.estimated_revenue_kopecks == expected_revenue
    assert success.estimated_margin_kopecks == expected_revenue - 250
    assert await session.scalar(
        select(ProviderCostEntry.id).where(
            ProviderCostEntry.generation_task_id == success.id
        )
    )

    first = GenerationTask(
        user_id=buyer.id,
        model_code=model.code,
        status="submitted",
        chat_id=9001,
        message_id=9002,
    )
    session.add(first)
    await session.flush()
    try:
        async with session.begin_nested():
            session.add(
                GenerationTask(
                    user_id=buyer.id,
                    model_code=model.code,
                    status="submitted",
                    chat_id=9001,
                    message_id=9002,
                )
            )
            await session.flush()
    except IntegrityError:
        pass
    else:
        raise AssertionError("idempotency key is not unique")

    try:
        async with session.begin_nested():
            session.add(
                GenerationModel(
                    code=f"negative-{suffix}",
                    title="Negative",
                    category="image",
                    price_credits=-1,
                    config={},
                )
            )
            await session.flush()
    except IntegrityError:
        pass
    else:
        raise AssertionError("negative model price was accepted")

    subscription = CreditPackage(
        code=f"subscription-{suffix}",
        title="Subscription",
        price_rub=100,
        is_unlimited=True,
        duration_days=30,
        is_enabled=True,
    )
    assert package_is_user_visible(subscription)

    malformed_subscription = CreditPackage(
        code=f"subscription-invalid-{suffix}",
        title="Invalid subscription",
        price_rub=100,
        is_unlimited=True,
        duration_days=0,
        is_enabled=True,
    )
    assert not package_is_user_visible(malformed_subscription)

    assert int(
        await session.scalar(select(func.count()).select_from(CreditLedgerEntry)) or 0
    ) > 0
    assert int(
        await session.scalar(select(func.count()).select_from(AffiliateLedgerEntry)) or 0
    ) > 0
    ledger = await session.scalar(select(CreditLedgerEntry).limit(1))
    assert ledger
    try:
        async with session.begin_nested():
            ledger.reason = "mutated"
            await session.flush()
    except DBAPIError:
        session.expire(ledger)
    else:
        raise AssertionError("credit ledger is mutable")
