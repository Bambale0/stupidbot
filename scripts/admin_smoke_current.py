from __future__ import annotations

import asyncio
from decimal import Decimal
from uuid import uuid4

if __package__ in {None, ""}:
    from _bootstrap import add_project_root_to_path

    add_project_root_to_path()

from sqlalchemy import select
from sqlalchemy.ext.asyncio import async_sessionmaker

from app.config import get_settings
from app.db import build_engine, init_db
from app.models import (
    AffiliateWithdrawal,
    CreditPackage,
    GalleryItem,
    GenerationModel,
    GenerationTask,
    PartnerLink,
    Payment,
    User,
)
from app.plugins.admin import plugin as admin_plugin
from app.repositories import (
    apply_affiliate_commission,
    charge_user_for_model,
    get_feed_tasks,
    increment_feed_share,
    like_feed_task,
    remove_task_from_feed,
    share_task_to_feed,
)


def _check_keyboards() -> None:
    keyboards = [
        admin_plugin._admin_keyboard(),
        admin_plugin._users_keyboard(),
        admin_plugin._model_keyboard(1),
        admin_plugin._package_keyboard(1),
        admin_plugin._payment_keyboard(1, "manual_pending"),
        admin_plugin._gallery_keyboard(),
        admin_plugin._gallery_item_keyboard(1),
        admin_plugin._partners_keyboard(),
        admin_plugin._partner_item_keyboard(1),
        admin_plugin._settings_keyboard(),
        admin_plugin._broadcast_confirm_keyboard(),
    ]
    for keyboard in keyboards:
        assert keyboard.inline_keyboard, "admin keyboard must not be empty"


def _check_parsers() -> None:
    assert admin_plugin._parse_bool("да") is True
    assert admin_plugin._parse_bool("off") is False
    assert admin_plugin._looks_like_url("https://example.com")
    assert not admin_plugin._looks_like_url("ftp://example.com")


async def _check_database_workflows() -> None:
    settings = get_settings()
    engine = build_engine(settings)
    await init_db(engine)
    async with engine.connect() as conn:
        transaction = await conn.begin()
        session_factory = async_sessionmaker(conn, expire_on_commit=False, autoflush=False)
        try:
            async with session_factory() as session:
                suffix = uuid4().hex[:10]
                admin_user = User(telegram_id=int(f"9001{suffix[:6]}", 16), is_admin=True)
                regular_user = User(telegram_id=int(f"9002{suffix[:6]}", 16), is_admin=False)
                model = GenerationModel(
                    code=f"smoke-model-{suffix}",
                    title="Smoke Model",
                    category="image",
                    price_credits=3,
                    config={},
                )
                package = CreditPackage(
                    code=f"smoke-package-{suffix}",
                    title="Smoke Package",
                    credits=7,
                    price_rub=Decimal("10.00"),
                    is_enabled=True,
                )
                session.add_all([admin_user, regular_user, model, package])
                await session.flush()

                assert await charge_user_for_model(session, user=admin_user, model=model)
                assert admin_user.credits_balance == 0
                assert not await charge_user_for_model(session, user=regular_user, model=model)

                payment = Payment(
                    user_id=regular_user.id,
                    package_id=package.id,
                    provider="manual",
                    order_id=f"smoke-order-{suffix}",
                    amount_kopecks=1000,
                    status="manual_pending",
                )
                session.add(payment)
                await session.flush()
                result = await admin_plugin._mark_payment_paid(session, payment)
                assert result.ok, result.admin_text
                assert payment.status == "paid"
                assert regular_user.credits_balance == 7

                referred_user = User(
                    telegram_id=int(f"9003{suffix[:6]}", 16),
                    is_admin=False,
                    referred_by_user_id=admin_user.id,
                )
                session.add(referred_user)
                await session.flush()
                affiliate_payment = Payment(
                    user_id=referred_user.id,
                    package_id=package.id,
                    provider="manual",
                    order_id=f"smoke-affiliate-30-{suffix}",
                    amount_kopecks=10000,
                    status="paid",
                )
                session.add(affiliate_payment)
                await session.flush()
                commission = await apply_affiliate_commission(
                    session,
                    payment=affiliate_payment,
                    buyer=referred_user,
                )
                assert commission == 3000
                assert admin_user.affiliate_balance_kopecks == 3000

                admin_user.affiliate_commission_rate_bps = 5000
                ambassador_payment = Payment(
                    user_id=referred_user.id,
                    package_id=package.id,
                    provider="manual",
                    order_id=f"smoke-affiliate-50-{suffix}",
                    amount_kopecks=10000,
                    status="paid",
                )
                session.add(ambassador_payment)
                await session.flush()
                commission = await apply_affiliate_commission(
                    session,
                    payment=ambassador_payment,
                    buyer=referred_user,
                )
                assert commission == 5000
                assert admin_user.affiliate_balance_kopecks == 8000

                withdrawal = AffiliateWithdrawal(
                    user_id=admin_user.id,
                    amount_kopecks=admin_user.affiliate_balance_kopecks,
                    status="pending",
                    details="Smoke payout details",
                )
                admin_user.affiliate_balance_kopecks = 0
                session.add(withdrawal)
                await session.flush()
                assert withdrawal.amount_kopecks == 8000
                assert admin_user.affiliate_balance_kopecks == 0

                gallery = GalleryItem(
                    title="Smoke Gallery",
                    prompt="prompt",
                    media_url="https://example.com/image.jpg",
                    media_type="image",
                    is_public=True,
                )
                partner = PartnerLink(
                    code=f"smoke-partner-{suffix}",
                    title="Smoke Partner",
                    url="https://example.com",
                    is_enabled=True,
                )
                session.add_all([gallery, partner])
                await session.flush()
                assert "Smoke Gallery" in admin_plugin._gallery_detail_text(gallery)
                assert "Smoke Partner" in admin_plugin._partner_detail_text(partner)

                feed_task = GenerationTask(
                    user_id=regular_user.id,
                    model_code="nano-banana",
                    status="success",
                    prompt="feed prompt",
                    result_urls=["https://example.com/feed.jpg"],
                    input_payload={"resolution": "1K"},
                )
                foreign_source = GenerationTask(
                    user_id=admin_user.id,
                    model_code="nano-banana",
                    status="success",
                    prompt="foreign source",
                    result_urls=["https://example.com/source.jpg"],
                )
                session.add_all([feed_task, foreign_source])
                await session.flush()

                ok, reason = await share_task_to_feed(
                    session,
                    task_id=feed_task.id,
                    user_id=regular_user.id,
                )
                assert ok, reason
                feed_items = await get_feed_tasks(session, limit=5)
                assert any(item.id == feed_task.id for item in feed_items)

                likes, active = await like_feed_task(
                    session,
                    task_id=feed_task.id,
                    user_id=regular_user.id,
                )
                assert likes == 1 and active

                likes, active = await like_feed_task(
                    session,
                    task_id=feed_task.id,
                    user_id=regular_user.id,
                )
                assert likes == 0 and not active

                dislikes, active = await like_feed_task(
                    session,
                    task_id=-feed_task.id,
                    user_id=regular_user.id,
                )
                assert dislikes == 1 and active

                likes, active = await like_feed_task(
                    session,
                    task_id=feed_task.id,
                    user_id=regular_user.id,
                )
                assert likes == 1 and active, "like must replace the existing dislike"

                assert await increment_feed_share(session, feed_task.id) is None
                assert await remove_task_from_feed(
                    session,
                    task_id=feed_task.id,
                    user_id=regular_user.id,
                )

                derivative = GenerationTask(
                    user_id=regular_user.id,
                    model_code="nano-banana",
                    status="success",
                    prompt="derivative",
                    result_urls=["https://example.com/derivative.jpg"],
                    source_feed_task_id=foreign_source.id,
                )
                session.add(derivative)
                await session.flush()
                ok, reason = await share_task_to_feed(
                    session,
                    task_id=derivative.id,
                    user_id=regular_user.id,
                )
                assert not ok and reason == "foreign_source"

                banana_models = list(
                    await session.scalars(
                        select(GenerationModel).where(
                            GenerationModel.code.in_(
                                ["nano-banana", "nano-banana-pro", "nano-banana-2"]
                            )
                        )
                    )
                )
                assert banana_models, "banana models must exist"
                by_code = {banana.code: banana for banana in banana_models}

                lite = by_code["nano-banana"]
                assert lite.title == "Nano Banana 2 Lite"
                assert lite.config.get("provider") == "comet", lite.config
                assert lite.config.get("provider_model") == "gemini-3.1-flash-lite-image", lite.config
                assert lite.config.get("fallback_provider") == "kie", lite.config
                assert lite.config.get("fallback_model") == "nano-banana-2-lite", lite.config
                assert lite.config.get("resolutions") == ["1K"], lite.config
                assert lite.config.get("output_formats") == [], lite.config
                assert lite.config.get("max_images") == 14, lite.config
                assert lite.config.get("fallback_max_images") == 10, lite.config

                pro = by_code["nano-banana-pro"]
                assert pro.config.get("provider_model") == "gemini-3-pro-image", pro.config
                assert pro.config.get("resolutions") == ["1K", "2K", "4K"], pro.config
                assert pro.config.get("max_images") == 14, pro.config

                flash = by_code["nano-banana-2"]
                assert flash.config.get("provider_model") == "gemini-3.1-flash-image", flash.config
                assert flash.config.get("resolutions") == ["512", "1K", "2K", "4K"], flash.config
                assert flash.config.get("max_images") == 14, flash.config
        finally:
            await transaction.rollback()
    await engine.dispose()


async def amain() -> None:
    _check_keyboards()
    _check_parsers()
    await _check_database_workflows()
    print("admin current-policy smoke passed")


def main() -> None:
    asyncio.run(amain())
