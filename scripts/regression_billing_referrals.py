from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace

from sqlalchemy.ext.asyncio import AsyncSession

from app import repositories
from app.models import CreditPackage, Payment, User
from app.services.billing_catalog import (
    CREATOR_PHOTO_CREDITS,
    CREATOR_VIDEO_CREDITS,
    ONE_TIME_TERMS,
    SUBSCRIPTION_CODE,
    SUBSCRIPTION_TERMS,
    SUBSCRIPTION_TITLE,
)
from app.services.financial_credits import (
    apply_affiliate_commission,
    apply_package_snapshot_to_user,
    grant_affiliate_balance,
    package_is_user_visible,
    reverse_paid_payment,
)
from app.services.generation_catalog import DEFAULT_MODELS
from app.services.nano_banana_lite import (
    MAX_REFERENCE_IMAGES,
    PROVIDER_MODEL,
    build_nano_banana_2_lite_payload,
)
from app.services.referrals import bind_referral, ensure_partner_code


def check_catalog_contracts() -> None:
    lite = next(item for item in DEFAULT_MODELS if item["code"] == "nano-banana")
    assert lite["title"] == "Nano Banana 2 Lite"
    assert lite["price_credits"] == 2
    assert lite["config"]["provider"] == "kie"
    assert lite["config"]["provider_model"] == PROVIDER_MODEL
    assert lite["config"]["resolutions"] == ["1K"]
    assert lite["config"]["max_images"] == MAX_REFERENCE_IMAGES
    assert lite["config"]["output_formats"] == []

    payload = build_nano_banana_2_lite_payload(
        prompt="test prompt",
        image_urls=["https://example.com/ref.webp"],
        aspect_ratio="9:16",
        callback_url="https://example.com/callback",
    )
    assert payload == {
        "model": "nano-banana-2-lite",
        "callBackUrl": "https://example.com/callback",
        "input": {
            "prompt": "test prompt",
            "image_urls": ["https://example.com/ref.webp"],
            "aspect_ratio": "9:16",
        },
    }
    assert "image_input" not in payload["input"]
    assert "resolution" not in payload["input"]
    assert "output_format" not in payload["input"]

    creator = next(item for item in repositories.DEFAULT_PACKAGES if item["code"] == "creator")
    assert int(creator["photo_credits"]) == CREATOR_PHOTO_CREDITS
    assert int(creator["video_credits"]) == CREATOR_VIDEO_CREDITS
    assert int(creator["video_credits"]) >= 18
    assert creator["terms"] == ONE_TIME_TERMS
    assert "автопродления" in str(creator["terms"]).lower()

    subscription = next(
        item for item in repositories.DEFAULT_PACKAGES if item["code"] == SUBSCRIPTION_CODE
    )
    assert subscription["title"] == SUBSCRIPTION_TITLE
    assert subscription["terms"] == SUBSCRIPTION_TERMS
    assert subscription["is_enabled"] is True
    assert subscription["is_unlimited"] is True
    assert int(subscription["duration_days"]) == 30

    legacy_creator = SimpleNamespace(
        code="creator",
        credits=0,
        photo_credits=50,
        video_credits=10,
    )
    assert repositories._should_sync_default_package_split(legacy_creator, creator)

    paid_subscription = CreditPackage(
        code="subscription-regression",
        title="Subscription",
        price_rub=100,
        is_unlimited=True,
        duration_days=30,
        is_enabled=True,
    )
    assert package_is_user_visible(paid_subscription)

    invalid_subscription = CreditPackage(
        code="invalid-subscription-regression",
        title="Invalid subscription",
        price_rub=100,
        is_unlimited=True,
        duration_days=0,
        is_enabled=True,
    )
    assert not package_is_user_visible(invalid_subscription)


async def run_billing_referral_regression(session: AsyncSession, suffix: str) -> None:
    check_catalog_contracts()

    hybrid_user = User(
        telegram_id=int(f"81{suffix}", 16),
        photo_credits_balance=1,
        credits_balance=2,
    )
    session.add(hybrid_user)
    await session.flush()
    allocation = repositories.spend_user_credits(
        hybrid_user,
        credit_type="photo",
        amount=3,
    )
    assert allocation == {"photo": 1, "common": 2}
    assert hybrid_user.photo_credits_balance == 0
    assert hybrid_user.credits_balance == 0

    subscription_user = User(
        telegram_id=int(f"82{suffix}", 16),
        photo_credits_balance=7,
        video_credits_balance=4,
    )
    session.add(subscription_user)
    await session.flush()
    subscription_snapshot = {
        "credits": 0,
        "photo_credits": 0,
        "video_credits": 0,
        "is_unlimited": True,
        "duration_days": 30,
    }
    await apply_package_snapshot_to_user(
        session,
        user=subscription_user,
        snapshot=subscription_snapshot,
    )
    first_subscription_until = subscription_user.unlimited_until
    assert first_subscription_until and first_subscription_until > datetime.now(timezone.utc)
    assert subscription_user.photo_credits_balance == 7
    assert subscription_user.video_credits_balance == 4

    await apply_package_snapshot_to_user(
        session,
        user=subscription_user,
        snapshot=subscription_snapshot,
    )
    assert subscription_user.unlimited_until
    assert (subscription_user.unlimited_until - first_subscription_until).days == 30

    debt_user = User(
        telegram_id=int(f"83{suffix}", 16),
        photo_credit_debt=5,
    )
    session.add(debt_user)
    await session.flush()
    await apply_package_snapshot_to_user(
        session,
        user=debt_user,
        snapshot={
            "credits": 0,
            "photo_credits": CREATOR_PHOTO_CREDITS,
            "video_credits": CREATOR_VIDEO_CREDITS,
            "is_unlimited": False,
        },
    )
    assert debt_user.photo_credit_debt == 0
    assert debt_user.photo_credits_balance == CREATOR_PHOTO_CREDITS - 5
    assert debt_user.video_credits_balance == CREATOR_VIDEO_CREDITS

    referrer = User(
        telegram_id=int(f"84{suffix}", 16),
        partner_code=f"legacy-{suffix}",
    )
    buyer = User(telegram_id=int(f"85{suffix}", 16))
    subscription_buyer = User(telegram_id=int(f"86{suffix}", 16))
    other = User(telegram_id=int(f"87{suffix}", 16))
    blocked_referrer = User(
        telegram_id=int(f"88{suffix}", 16),
        is_blocked=True,
    )
    blocked_buyer = User(telegram_id=int(f"89{suffix}", 16))
    session.add_all(
        [referrer, buyer, subscription_buyer, other, blocked_referrer, blocked_buyer]
    )
    await session.flush()

    legacy_code = str(referrer.partner_code)
    ref_code = await ensure_partner_code(session, referrer)
    await ensure_partner_code(session, buyer)
    await ensure_partner_code(session, subscription_buyer)
    await ensure_partner_code(session, other)
    blocked_code = await ensure_partner_code(session, blocked_referrer)
    await ensure_partner_code(session, blocked_buyer)
    await session.flush()

    bound = await bind_referral(session, user=buyer, ref_code=f"ref_{legacy_code}")
    assert bound and bound.id == referrer.id
    subscription_bound = await bind_referral(
        session,
        user=subscription_buyer,
        ref_code=f"ref-{ref_code}",
    )
    assert subscription_bound and subscription_bound.id == referrer.id
    blocked_bind = await bind_referral(
        session,
        user=blocked_buyer,
        ref_code=blocked_code,
    )
    assert blocked_bind is None
    rebound = await bind_referral(session, user=buyer, ref_code=other.partner_code)
    assert rebound is None
    self_bind = await bind_referral(session, user=other, ref_code=other.partner_code)
    assert self_bind is None
    cycle = await bind_referral(session, user=referrer, ref_code=buyer.partner_code)
    assert cycle is None
    assert referrer.referred_by_user_id is None

    credit_payment = Payment(
        user_id=buyer.id,
        order_id=f"hybrid-credit-referral-{suffix}",
        amount_kopecks=10_000,
        status="paid",
        raw_payload={
            "package_snapshot": {
                "credits": 0,
                "photo_credits": 10,
                "video_credits": 20,
                "is_unlimited": False,
            }
        },
    )
    subscription_payment = Payment(
        user_id=subscription_buyer.id,
        order_id=f"hybrid-subscription-referral-{suffix}",
        amount_kopecks=20_000,
        status="paid",
        raw_payload={"package_snapshot": subscription_snapshot},
    )
    session.add_all([credit_payment, subscription_payment])

    await apply_package_snapshot_to_user(
        session,
        user=buyer,
        snapshot=credit_payment.raw_payload["package_snapshot"],
    )
    await apply_package_snapshot_to_user(
        session,
        user=subscription_buyer,
        snapshot=subscription_snapshot,
    )
    await session.flush()

    first_commission = await apply_affiliate_commission(
        session,
        payment=credit_payment,
        buyer=buyer,
    )
    duplicate_commission = await apply_affiliate_commission(
        session,
        payment=credit_payment,
        buyer=buyer,
    )
    subscription_commission = await apply_affiliate_commission(
        session,
        payment=subscription_payment,
        buyer=subscription_buyer,
    )
    assert first_commission == 3000
    assert duplicate_commission == 0
    assert subscription_commission == 6000
    assert referrer.affiliate_balance_kopecks == 9000
    assert referrer.affiliate_earned_kopecks == 9000

    # Simulate a pending withdrawal: available commission is reserved before bank reversals.
    referrer.affiliate_balance_kopecks = 0
    credit_reversed, credit_reversal_debts = await reverse_paid_payment(
        session,
        payment=credit_payment,
        reason="billing-referral-credit-regression",
    )
    subscription_reversed, subscription_reversal_debts = await reverse_paid_payment(
        session,
        payment=subscription_payment,
        reason="billing-referral-subscription-regression",
    )
    assert credit_reversed and subscription_reversed
    assert credit_reversal_debts["affiliate_kopecks"] == 3000
    assert subscription_reversal_debts["affiliate_kopecks"] == 6000
    assert subscription_buyer.unlimited_until is None
    assert referrer.affiliate_debt_kopecks == 9000
    assert referrer.affiliate_earned_kopecks == 0

    restored = grant_affiliate_balance(referrer, 9000)
    assert restored == 0
    assert referrer.affiliate_debt_kopecks == 0
    assert referrer.affiliate_balance_kopecks == 0
