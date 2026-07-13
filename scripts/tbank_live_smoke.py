from __future__ import annotations

import argparse
import asyncio
import json
from types import SimpleNamespace
from uuid import uuid4

from sqlalchemy import select

if __package__ in {None, ""}:
    from _bootstrap import add_project_root_to_path

    add_project_root_to_path()

from app.config import get_settings
from app.db import build_engine, build_session_factory, session_scope
from app.models import Payment, User
from app.services.financial_payment_patch import install_payment_patches
from app.services.referrals import install_repository_patches
from app.services.tbank import TBankClient

install_repository_patches()
install_payment_patches()

from app.services import payments as payment_service  # noqa: E402

CREATE_CONFIRMATION = "CREATE_TEST_PAYMENT"
READ_CONFIRMATION = "READ_TEST_PAYMENT"
REFUND_CONFIRMATION = "REFUND_TEST_PAYMENT"
FINAL_REFUND_STATES = {"REFUNDED", "REVERSED"}


def _snapshot(amount_kopecks: int) -> dict[str, object]:
    return {
        "package_id": None,
        "code": "staging-tbank-live-smoke",
        "title": "Staging T-Bank smoke — 1 photo credit",
        "description": "Audited staging payment smoke",
        "terms": "Synthetic staging user; full refund is expected",
        "credits": 0,
        "photo_credits": 1,
        "video_credits": 0,
        "price_rub": f"{amount_kopecks / 100:.2f}",
        "is_unlimited": False,
        "duration_days": None,
    }


def _require_enabled(settings: object) -> None:
    if not bool(getattr(settings, "tbank_test_smoke_enabled", False)):
        raise RuntimeError(
            "T-Bank live smoke is disabled. Set TBANK_TEST_SMOKE_ENABLED=true "
            "only on an approved staging test terminal."
        )


def _client(settings: object) -> TBankClient:
    client = TBankClient(
        terminal_key=getattr(settings, "tbank_terminal_key", None),
        password=getattr(settings, "tbank_password", None),
        success_url=getattr(settings, "tbank_success_url", None),
        fail_url=getattr(settings, "tbank_fail_url", None),
    )
    if not client.is_configured:
        raise RuntimeError("T-Bank credentials are not configured")
    return client


async def _load_payment(factory, order_id: str, *, lock: bool = False) -> Payment:
    async with session_scope(factory) as session:
        stmt = select(Payment).where(Payment.order_id == order_id)
        if lock:
            stmt = stmt.with_for_update()
        payment = await session.scalar(stmt)
        if not payment:
            raise RuntimeError(f"Smoke payment not found: {order_id}")
        source = str(dict(payment.raw_payload or {}).get("source") or "")
        if source != "tbank_live_smoke":
            raise RuntimeError("Refusing to operate on a non-smoke payment")
        return payment


async def create_payment(args: argparse.Namespace) -> None:
    if args.confirm != CREATE_CONFIRMATION:
        raise RuntimeError(f"create requires --confirm {CREATE_CONFIRMATION}")
    amount = int(args.amount_kopecks)
    if amount < 100 or amount > 100_000:
        raise RuntimeError("amount_kopecks must be between 100 and 100000")

    settings = get_settings()
    _require_enabled(settings)
    client = _client(settings)
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    suffix = uuid4().hex
    order_id = f"staging-tbank-smoke-{suffix[:20]}"
    referrer_tid = 8_400_000_000 + int(suffix[:7], 16)
    buyer_tid = referrer_tid + 1
    payment_db_id = 0
    try:
        async with session_scope(factory) as session:
            referrer = User(telegram_id=referrer_tid)
            session.add(referrer)
            await session.flush()
            buyer = User(
                telegram_id=buyer_tid,
                referred_by_user_id=referrer.id,
            )
            session.add(buyer)
            await session.flush()
            payment = Payment(
                user_id=buyer.id,
                package_id=None,
                order_id=order_id,
                amount_kopecks=amount,
                status="created",
                raw_payload={
                    "package_snapshot": _snapshot(amount),
                    "source": "tbank_live_smoke",
                    "smoke_referrer_user_id": referrer.id,
                    "smoke_buyer_user_id": buyer.id,
                },
            )
            session.add(payment)
            await session.flush()
            payment_db_id = payment.id

        try:
            result = await client.init_payment(
                order_id=order_id,
                amount_kopecks=amount,
                description="Staging T-Bank smoke",
                notification_url=settings.tbank_callback_url,
                customer_key=f"staging-smoke-{buyer_tid}",
            )
        except Exception as exc:
            async with session_scope(factory) as session:
                payment = await session.get(Payment, payment_db_id, with_for_update=True)
                if payment:
                    payment.status = "failed"
                    payment.raw_payload = {
                        **dict(payment.raw_payload or {}),
                        "provider_init_error": type(exc).__name__,
                    }
            raise

        payment_url = str(result.get("PaymentURL") or "")
        provider_payment_id = str(result.get("PaymentId") or "")
        if not payment_url or not provider_payment_id:
            raise RuntimeError("T-Bank Init did not return PaymentURL and PaymentId")
        async with session_scope(factory) as session:
            payment = await session.get(Payment, payment_db_id, with_for_update=True)
            if not payment:
                raise RuntimeError("Smoke payment disappeared after provider Init")
            payment.provider_payment_id = provider_payment_id
            payment.payment_url = payment_url
            payment.raw_payload = {
                **dict(payment.raw_payload or {}),
                "provider_init": result,
            }

        print(
            "TBANK_SMOKE_CREATE="
            + json.dumps(
                {
                    "order_id": order_id,
                    "payment_db_id": payment_db_id,
                    "provider_payment_id": provider_payment_id,
                    "amount_kopecks": amount,
                    "payment_url": payment_url,
                    "next": "Complete with an official T-Bank test card, then run status",
                },
                ensure_ascii=False,
                sort_keys=True,
            )
        )
    finally:
        await engine.dispose()


async def _state_summary(factory, client: TBankClient, order_id: str) -> dict[str, object]:
    payment = await _load_payment(factory, order_id)
    if not payment.provider_payment_id:
        raise RuntimeError("Smoke payment has no provider PaymentId")
    provider = await client.get_state(payment.provider_payment_id)
    async with session_scope(factory) as session:
        current = await session.get(Payment, payment.id)
        if not current:
            raise RuntimeError("Smoke payment disappeared")
        buyer = await session.get(User, current.user_id)
        referrer = (
            await session.get(User, current.affiliate_commission_user_id)
            if current.affiliate_commission_user_id
            else None
        )
        return {
            "order_id": current.order_id,
            "payment_db_id": current.id,
            "provider_payment_id": current.provider_payment_id,
            "provider_status": str(provider.get("Status") or ""),
            "db_status": current.status,
            "amount_kopecks": current.amount_kopecks,
            "buyer_photo_balance": int(buyer.photo_credits_balance or 0) if buyer else None,
            "buyer_photo_debt": int(buyer.photo_credit_debt or 0) if buyer else None,
            "affiliate_commission_kopecks": int(current.affiliate_commission_kopecks or 0),
            "affiliate_reversed_kopecks": int(current.affiliate_commission_reversed_kopecks or 0),
            "referrer_balance_kopecks": int(referrer.affiliate_balance_kopecks or 0)
            if referrer
            else None,
            "referrer_debt_kopecks": int(referrer.affiliate_debt_kopecks or 0)
            if referrer
            else None,
        }


async def read_status(args: argparse.Namespace) -> None:
    if args.confirm != READ_CONFIRMATION:
        raise RuntimeError(f"status requires --confirm {READ_CONFIRMATION}")
    if not args.order_id:
        raise RuntimeError("status requires --order-id")
    settings = get_settings()
    _require_enabled(settings)
    client = _client(settings)
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    try:
        summary = await _state_summary(factory, client, args.order_id)
        provider_status = str(summary["provider_status"]).upper()
        if provider_status == "CONFIRMED" and summary["db_status"] != "paid":
            raise RuntimeError(
                "Provider is CONFIRMED but webhook has not produced db_status=paid"
            )
        if summary["db_status"] == "paid":
            if int(summary["buyer_photo_balance"] or 0) != 1:
                raise RuntimeError("Paid smoke did not grant exactly one photo credit")
            if int(summary["affiliate_commission_kopecks"] or 0) <= 0:
                raise RuntimeError("Paid smoke did not grant affiliate commission")
        print("TBANK_SMOKE_STATUS=" + json.dumps(summary, ensure_ascii=False, sort_keys=True))
    finally:
        await engine.dispose()


async def _stored_reversal_callback(factory, payment_id: int) -> tuple[str, dict[str, object] | None]:
    async with session_scope(factory) as session:
        payment = await session.get(Payment, payment_id)
        if not payment:
            raise RuntimeError("Smoke payment disappeared while waiting for reversal webhook")
        raw = dict(payment.raw_payload or {})
        callback = raw.get("reversal_callback")
        return payment.status, callback if isinstance(callback, dict) else None


async def refund_payment(args: argparse.Namespace) -> None:
    if args.confirm != REFUND_CONFIRMATION:
        raise RuntimeError(f"refund requires --confirm {REFUND_CONFIRMATION}")
    if not args.order_id:
        raise RuntimeError("refund requires --order-id")
    settings = get_settings()
    _require_enabled(settings)
    client = _client(settings)
    engine = build_engine(settings)
    factory = build_session_factory(engine)
    context = SimpleNamespace(
        settings=settings,
        session_factory=factory,
        tbank=client,
        bot=None,
    )
    try:
        payment = await _load_payment(factory, args.order_id)
        if not payment.provider_payment_id:
            raise RuntimeError("Smoke payment has no provider PaymentId")
        state = await client.get_state(payment.provider_payment_id)
        provider_status = str(state.get("Status") or "").upper()
        if provider_status != "CONFIRMED":
            raise RuntimeError(
                f"Full refund smoke requires provider status CONFIRMED, got {provider_status}"
            )
        before = await _state_summary(factory, client, payment.order_id)
        if before["db_status"] != "paid":
            raise RuntimeError("DB payment must be paid before a live refund smoke")
        if int(before["buyer_photo_balance"] or 0) != 1:
            raise RuntimeError("Smoke buyer must have the single granted photo credit")
        if int(before["affiliate_commission_kopecks"] or 0) <= 0:
            raise RuntimeError("Smoke payment must have an affiliate commission")

        await client.cancel_payment(payment.provider_payment_id)
        deadline = asyncio.get_running_loop().time() + max(30, int(args.timeout_seconds))
        final_state: dict[str, object] | None = None
        real_callback: dict[str, object] | None = None
        while asyncio.get_running_loop().time() < deadline:
            candidate = await client.get_state(payment.provider_payment_id)
            candidate_status = str(candidate.get("Status") or "").upper()
            db_status, stored_callback = await _stored_reversal_callback(factory, payment.id)
            if (
                candidate_status in FINAL_REFUND_STATES
                and db_status == "reversed"
                and stored_callback is not None
            ):
                final_state = candidate
                real_callback = stored_callback
                break
            await asyncio.sleep(5)
        if final_state is None or real_callback is None:
            raise TimeoutError(
                "T-Bank refund did not produce both a final provider state and a real reversal webhook"
            )

        final_status = str(final_state.get("Status") or "").upper()
        callback_status = str(real_callback.get("Status") or "").upper()
        if callback_status != final_status:
            raise RuntimeError(
                f"Real reversal webhook status {callback_status} does not match provider state {final_status}"
            )
        if not client.verify_notification(real_callback):
            raise RuntimeError("Stored real reversal webhook has an invalid signature")
        if str(real_callback.get("OrderId") or "") != payment.order_id:
            raise RuntimeError("Stored reversal webhook OrderId mismatch")
        if str(real_callback.get("PaymentId") or "") != payment.provider_payment_id:
            raise RuntimeError("Stored reversal webhook PaymentId mismatch")

        summary_before_replay = await _state_summary(factory, client, payment.order_id)
        if not await payment_service.handle_tbank_notification(context, real_callback):
            raise RuntimeError("Duplicate replay of the real reversal webhook was rejected")
        summary_after_replay = await _state_summary(factory, client, payment.order_id)
        stable_fields = (
            "db_status",
            "buyer_photo_balance",
            "buyer_photo_debt",
            "affiliate_commission_kopecks",
            "affiliate_reversed_kopecks",
            "referrer_balance_kopecks",
            "referrer_debt_kopecks",
        )
        for field in stable_fields:
            if summary_after_replay[field] != summary_before_replay[field]:
                raise RuntimeError(f"Duplicate reversal webhook changed {field}")

        summary = summary_after_replay
        if summary["db_status"] != "reversed":
            raise RuntimeError(f"Expected db_status=reversed, got {summary['db_status']}")
        if int(summary["buyer_photo_balance"] or 0) != 0:
            raise RuntimeError("Full refund did not remove the granted photo credit")
        if int(summary["buyer_photo_debt"] or 0) != 0:
            raise RuntimeError("Unused smoke credit unexpectedly became debt")
        if int(summary["affiliate_reversed_kopecks"] or 0) != int(
            summary["affiliate_commission_kopecks"] or 0
        ):
            raise RuntimeError("Affiliate commission was not fully reversed")
        if int(summary["referrer_balance_kopecks"] or 0) != 0:
            raise RuntimeError("Available affiliate commission remained after full refund")
        if int(summary["referrer_debt_kopecks"] or 0) != 0:
            raise RuntimeError("Unused smoke commission unexpectedly became affiliate debt")
        print("TBANK_SMOKE_REFUND=" + json.dumps(summary, ensure_ascii=False, sort_keys=True))
    finally:
        await engine.dispose()


async def amain(args: argparse.Namespace) -> None:
    if args.action == "create":
        await create_payment(args)
    elif args.action == "status":
        await read_status(args)
    else:
        await refund_payment(args)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Guarded live T-Bank staging smoke")
    parser.add_argument("--action", choices=("create", "status", "refund"), required=True)
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--order-id")
    parser.add_argument("--amount-kopecks", type=int, default=1000)
    parser.add_argument("--timeout-seconds", type=int, default=180)
    asyncio.run(amain(parser.parse_args()))
