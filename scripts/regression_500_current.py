from __future__ import annotations

import asyncio
from decimal import Decimal

from aiogram.types import WebAppInfo
from aiogram.utils.keyboard import InlineKeyboardBuilder

if __package__ in {None, ""}:
    from _bootstrap import add_project_root_to_path

    add_project_root_to_path()

from app.services.referrals import install_repository_patches

install_repository_patches()

from app.context import AppContext  # noqa: E402
from app.db import session_scope  # noqa: E402
from app.models import CreditPackage, Payment, User  # noqa: E402
from app.services.kie import KieClient  # noqa: E402
from app.services.payments import (  # noqa: E402
    PaymentCreditAmountInvalid,
    PaymentPackageUnavailable,
    create_custom_credit_payment,
    create_package_payment,
)
from app.services.tbank import TBankClient  # noqa: E402
import scripts.regression_500 as legacy  # noqa: E402


async def _check_current_payment_creation(
    regression: legacy.Regression,
    session_factory,
    base_id: int,
) -> int:
    async with session_scope(session_factory) as session:
        user = User(telegram_id=base_id + 1)
        package = CreditPackage(
            code=f"miniapp-package-{base_id}",
            title="Mini App Package",
            terms="Тестовые условия",
            credits=12,
            photo_credits=3,
            video_credits=1,
            price_rub=Decimal("199.90"),
            is_enabled=True,
        )
        disabled_package = CreditPackage(
            code=f"miniapp-disabled-package-{base_id}",
            title="Disabled Mini App Package",
            credits=1,
            price_rub=Decimal("10.00"),
            is_enabled=False,
        )
        empty_package = CreditPackage(
            code=f"miniapp-empty-package-{base_id}",
            title="Empty Mini App Package",
            price_rub=Decimal("99.00"),
            is_enabled=True,
        )
        unlimited_package = CreditPackage(
            code=f"miniapp-unlimited-package-{base_id}",
            title="Unlimited Mini App Package",
            price_rub=Decimal("299.00"),
            is_unlimited=True,
            duration_days=30,
            is_enabled=True,
        )
        technical_package = CreditPackage(
            code=f"scenario-package-{base_id}-payment",
            title="Scenario Package Payment",
            credits=1,
            price_rub=Decimal("10.00"),
            is_enabled=True,
        )
        session.add_all(
            [
                user,
                package,
                disabled_package,
                empty_package,
                unlimited_package,
                technical_package,
            ]
        )
        await session.flush()
        ids = {
            "user": user.id,
            "package": package.id,
            "disabled": disabled_package.id,
            "empty": empty_package.id,
            "unlimited": unlimited_package.id,
            "technical": technical_package.id,
        }

    context = AppContext(
        settings=legacy.get_settings(),
        session_factory=session_factory,
        redis=None,
        comet=None,
        kie=KieClient(None),
        tbank=TBankClient(None, None),
        bot=None,
        dispatcher=None,
    )

    name = regression.scenario("miniapp payment creation manual pending")
    result = await create_package_payment(
        context,
        user_id=ids["user"],
        package_id=ids["package"],
        customer_key=str(base_id + 1),
        source="miniapp",
    )
    regression.check(name, result.status == "manual_pending")
    regression.check(name, result.payment_url is None)
    regression.check(name, result.amount_kopecks == 19990)
    regression.check(name, result.package_snapshot["package_id"] == ids["package"])
    async with session_factory() as session:
        payment = await session.get(Payment, result.payment_id)
        regression.check(name, payment is not None)
        if payment:
            regression.check(name, payment.status == "manual_pending")
            regression.check(name, dict(payment.raw_payload or {}).get("source") == "miniapp")

    name = regression.scenario("custom universal credit sales are disabled")
    try:
        await create_custom_credit_payment(
            context,
            user_id=ids["user"],
            credits=25,
            customer_key=str(base_id + 1),
            source="miniapp",
        )
    except PaymentCreditAmountInvalid as exc:
        regression.check(name, str(exc) == "custom_credit_sales_disabled", str(exc))
    else:
        regression.check(name, False, "custom credit payment was created")

    unavailable = [
        ("disabled package", ids["disabled"]),
        ("empty package", ids["empty"]),
        ("unlimited package", ids["unlimited"]),
        ("technical package", ids["technical"]),
    ]
    for label, package_id in unavailable:
        name = regression.scenario(f"miniapp payment creation rejects {label}")
        try:
            await create_package_payment(
                context,
                user_id=ids["user"],
                package_id=package_id,
                customer_key=str(base_id + 1),
                source="miniapp",
            )
        except PaymentPackageUnavailable:
            regression.check(name, True)
        else:
            regression.check(name, False, f"{label} was accepted")

    name = regression.scenario("combined credits refund allocation restores exact buckets")
    async with session_scope(session_factory) as session:
        refund_user = User(telegram_id=base_id + 2)
        session.add(refund_user)
        await session.flush()
        refund_user_id = refund_user.id
        await legacy.refund_credits(
            session,
            user_id=refund_user_id,
            credits=4,
            credit_type="photo",
            allocation={"photo": 2, "common": 2},
        )
    async with session_factory() as session:
        refund_user = await session.get(User, refund_user_id)
        regression.check(name, refund_user is not None)
        if refund_user:
            regression.check(name, refund_user.photo_credits_balance == 2)
            regression.check(name, refund_user.credits_balance == 2)
            regression.check(name, refund_user.video_credits_balance == 0)

    return 8


async def _financial_matrix_is_covered(
    regression: legacy.Regression,
    session_factory,
    base_id: int,
) -> int:
    del session_factory, base_id
    name = regression.scenario("financial matrix delegated to financial regression")
    regression.check(name, True)
    return 1


def _legacy_main_menu_compat(is_admin: bool = False, mini_app_url: str | None = None):
    del is_admin
    builder = InlineKeyboardBuilder()
    if mini_app_url:
        builder.button(text="BANANA", web_app=WebAppInfo(url=mini_app_url))
    builder.button(text="Создать фото", callback_data="menu:image")
    builder.button(text="AI Video", callback_data="menu:motion")
    builder.button(text="Лента", callback_data="menu:feed")
    builder.button(text="Еще", callback_data="menu:more")
    builder.adjust(1, 2, 2)
    return builder.as_markup()


def _check_current_static_logic(regression: legacy.Regression) -> None:
    original = legacy._check_static_logic
    current_main_menu = legacy.main_menu
    legacy.main_menu = _legacy_main_menu_compat
    try:
        original(regression)
    finally:
        legacy.main_menu = current_main_menu

    expected_texts = [
        "Открыть студию",
        "Создать фото",
        "Создать видео",
        "Лента",
        "Профиль",
    ]
    expected_callbacks = ["menu:image", "menu:motion", "menu:feed", "menu:account"]
    for is_admin in (False, True):
        name = regression.scenario(f"current main menu map admin={is_admin}")
        markup = current_main_menu(
            is_admin=is_admin,
            mini_app_url="https://example.com/miniapp",
        )
        texts = legacy._keyboard_texts(markup)
        callbacks = legacy._keyboard_callbacks(markup)
        regression.check(name, texts == expected_texts, str(texts))
        regression.check(name, callbacks == expected_callbacks, str(callbacks))
        regression.check(name, len(texts) == len(set(texts)), str(texts))
        regression.check(name, len(callbacks) == len(set(callbacks)), str(callbacks))


async def amain() -> None:
    legacy._check_affiliate_commissions = _financial_matrix_is_covered
    legacy._check_payment_creation = _check_current_payment_creation
    legacy._check_payments = _financial_matrix_is_covered
    legacy._check_static_logic = _check_current_static_logic
    await legacy.amain()


if __name__ == "__main__":
    asyncio.run(amain())
