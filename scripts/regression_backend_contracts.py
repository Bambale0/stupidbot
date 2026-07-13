from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path

if __package__ in {None, ""}:
    from _bootstrap import add_project_root_to_path

    add_project_root_to_path()

from app.services.referrals import install_repository_patches


async def amain() -> None:
    install_repository_patches()

    from app import repositories
    from app.services import payments

    assert payments.CUSTOM_CREDIT_PRICE_RUB == Decimal("0")
    assert payments.CUSTOM_CREDIT_MIN_AMOUNT == 0
    assert payments.CUSTOM_CREDIT_MAX_AMOUNT == 0

    try:
        await payments.create_custom_credit_payment(
            None,
            user_id=1,
            credits=100,
            customer_key="1",
            source="regression",
        )
    except payments.PaymentCreditAmountInvalid as exc:
        assert str(exc) == "custom_credit_sales_disabled"
    else:
        raise AssertionError("custom credit payment unexpectedly succeeded")

    assert await repositories.increment_feed_share(None, 1) is None

    main_source = Path("app/main.py").read_text(encoding="utf-8")
    bot_import = main_source.index("from app.bot import")
    repository_import = main_source.index("from app.repositories import")
    payments_import = main_source.index("from app.services.payments import")
    assert bot_import < repository_import
    assert bot_import < payments_import

    feed_source = Path("app/plugins/feed/plugin.py").read_text(encoding="utf-8")
    assert "increment_feed_share" not in feed_source
    assert 'text=f"Share' not in feed_source

    runtime_source = Path("app/static/miniapp/assets/runtime-sync.js").read_text(encoding="utf-8")
    assert "custom-credit-panel" in runtime_source
    assert "localStorage" not in runtime_source

    print("Backend disabled-contract regression passed")


if __name__ == "__main__":
    asyncio.run(amain())
