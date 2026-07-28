from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import CreditLedgerEntry, CreditPackage, Payment, User
from app.plugins.admin_finance import plugin as finance_admin
from app.plugins.loader import normalized_plugin_names


async def run_admin_financial_audit_regression(
    session: AsyncSession,
    suffix: str,
) -> None:
    payload, reason = finance_admin._reasoned_text("25 | компенсация за сбой")
    assert payload == "25"
    assert reason == "компенсация за сбой"
    try:
        finance_admin._reasoned_text("25")
    except ValueError as exc:
        assert "причин" in str(exc).lower()
    else:
        raise AssertionError("admin financial action accepted a missing reason")

    plugins = normalized_plugin_names(["core", "admin", "ux"])
    assert plugins.index("admin_finance") < plugins.index("admin")
    assert plugins[-1] == "ux"

    invalid_package = CreditPackage(
        code=f"audit-invalid-{suffix}",
        title="Invalid audit package",
        credits=0,
        photo_credits=0,
        video_credits=0,
        price_rub=Decimal("0"),
        is_enabled=False,
    )
    valid_package = CreditPackage(
        code=f"audit-valid-{suffix}",
        title="Valid audit package",
        credits=7,
        price_rub=Decimal("700"),
        is_enabled=True,
    )
    assert not finance_admin._package_can_be_enabled(invalid_package)
    assert finance_admin._package_can_be_enabled(valid_package)

    admin = User(
        telegram_id=int(f"981{suffix}", 16),
        is_admin=True,
        free_photo_generations_remaining=0,
    )
    customer = User(
        telegram_id=int(f"982{suffix}", 16),
        free_photo_generations_remaining=0,
    )
    session.add_all([admin, customer, valid_package])
    await session.flush()

    before = int(customer.credits_balance or 0)
    customer.credits_balance = before + 3
    await finance_admin._append_credit_audit(
        session,
        user=customer,
        credit_type="common",
        before=before,
        after=customer.credits_balance,
        reason="ручная компенсация",
        admin=admin,
        operation_key=f"audit-adjust-{suffix}",
        reference_type="admin_user",
        reference_id=str(customer.id),
    )
    await session.flush()
    adjustment = await session.scalar(
        select(CreditLedgerEntry).where(
            CreditLedgerEntry.operation_key == f"audit-adjust-{suffix}:common"
        )
    )
    assert adjustment is not None
    assert adjustment.balance_delta == 3
    assert adjustment.metadata_json["actor_user_id"] == admin.id
    assert adjustment.metadata_json["reason"] == "ручная компенсация"

    payment = Payment(
        user_id=customer.id,
        package_id=valid_package.id,
        provider="manual",
        order_id=f"audit-payment-{suffix}",
        amount_kopecks=70_000,
        status="manual_pending",
        raw_payload={
            "package_snapshot": {
                "title": valid_package.title,
                "credits": 7,
                "photo_credits": 0,
                "video_credits": 0,
                "is_unlimited": False,
                "duration_days": None,
                "price_rub": "700.00",
            }
        },
    )
    session.add(payment)
    await session.flush()

    paid_ok, _, _, _ = await finance_admin._execute_payment_action(
        session=session,
        payment=payment,
        action="paid",
        reason="перевод подтверждён банком",
        admin=admin,
        operation_key=f"audit-paid-{suffix}",
    )
    assert paid_ok
    assert payment.status == "paid"
    assert customer.credits_balance == 10
    assert len(payment.raw_payload["admin_audit"]) == 1

    # Simulate credits spent before the administrator reverses the payment.
    customer.credits_balance = 2
    reversed_ok, _, _, _ = await finance_admin._execute_payment_action(
        session=session,
        payment=payment,
        action="reverse",
        reason="возврат покупателю",
        admin=admin,
        operation_key=f"audit-reverse-{suffix}",
    )
    assert reversed_ok
    assert payment.status == "reversed"
    assert payment.reversal_reason == "возврат покупателю"
    assert customer.credits_balance == 0
    assert customer.common_credit_debt == 5
    assert len(payment.raw_payload["admin_audit"]) == 2

    duplicate_ok, _, _, _ = await finance_admin._execute_payment_action(
        session=session,
        payment=payment,
        action="reverse",
        reason="повторное сторно",
        admin=admin,
        operation_key=f"audit-reverse-duplicate-{suffix}",
    )
    assert not duplicate_ok
    assert customer.common_credit_debt == 5

    reverse_entry = await session.scalar(
        select(CreditLedgerEntry).where(
            CreditLedgerEntry.operation_key == f"audit-reverse-{suffix}:common"
        )
    )
    assert reverse_entry is not None
    assert reverse_entry.metadata_json["payment_action"] == "reverse"

    keyboard_callbacks = {
        button.callback_data
        for row in finance_admin._audited_payment_keyboard(payment.id, "paid").inline_keyboard
        for button in row
        if button.callback_data
    }
    assert f"admin:payment:reverse:{payment.id}" in keyboard_callbacks
