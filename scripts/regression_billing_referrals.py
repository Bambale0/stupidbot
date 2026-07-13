from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy.ext.asyncio import AsyncSession

from app import repositories
from app.models import CreditPackage, Payment, User
from app.services.billing_catalog import (
    CREATOR_PHOTO_CREDITS,
    CREATOR_VIDEO_CREDITS,
    ONE_TIME_TERMS,
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

    legacy_creator = SimpleNamespace(
        code="creator",
        credits=0,
        photo_credits=50,
        video_credits=10,
    )
    assert repositories._should_sync_default_package_split(legacy_creator, creator)

    unlimited = CreditPackage(
        code="legacy-subscription",
        title="Legacy subscription",
        price_rub=100,
        is_unlimited=True,
        duration_days=30,
        is_enabled=True,
    )
    assert not package_is_user_visible(unlimited)


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

    debt_user = User(
        telegram_id=int(f"82{suffix}", 16),
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

    referrer = User(telegram_id=int(f"83{suffix}", 16))
    buyer = User(telegram_id=int(f"84{suffix}", 16))
    other = User(telegram_id=int(f"85{suffix}", 16))
    session.add_all([referrer, buyer, other])
    await session.flush()
    ref_code = await ensure_partner_code(session, referrer)
    await ensure_partner_code(session, buyer)
    await ensure_partner_code(session, other)

    bound = await bind_referral(session, user=buyer, ref_code=f"ref_{ref_code}")
    assert bound and bound.id == referrer.id
    rebound = await bind_referral(session, user=buyer, ref_code=other.partner_code)
    assert rebound is None
    cycle = await bind_referral(session, user=referrer, ref_code=buyer.partner_code)
    assert cycle is None
    assert referrer.referred_by_user_id is None

    payment = Payment(
        user_id=buyer.id,
        order_id=f"hybrid-referral-{suffix}",
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
    session.add(payment)
    await apply_package_snapshot_to_user(
        session,
        user=buyer,
        snapshot=payment.raw_payload["package_snapshot"],
    )
    await session.flush()

    first_commission = await apply_affiliate_commission(session, payment=payment, buyer=buyer)
    second_commission = await apply_affiliate_commission(session, payment=payment, buyer=buyer)
    assert first_commission == 3000
    assert second_commission == 0
    assert referrer.affiliate_balance_kopecks == 3000
    assert referrer.affiliate_earned_kopecks == 3000

    # Simulate a pending withdrawal: available commission is reserved before bank reversal.
    referrer.affiliate_balance_kopecks = 0
    reversed_ok, reversal_debts = await reverse_paid_payment(
        session,
        payment=payment,
        reason="billing-referral-regression",
    )
    assert reversed_ok
    assert reversal_debts["affiliate_kopecks"] == 3000
    assert referrer.affiliate_debt_kopecks == 3000
    assert referrer.affiliate_earned_kopecks == 0

    restored = grant_affiliate_balance(referrer, 3000)
    assert restored == 0
    assert referrer.affiliate_debt_kopecks == 0
    assert referrer.affiliate_balance_kopecks == 0
