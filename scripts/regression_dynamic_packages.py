from __future__ import annotations

import asyncio
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace

import httpx
from fastapi import FastAPI

if __package__ in {None, ""}:
    from _bootstrap import add_project_root_to_path

    add_project_root_to_path()

from app import repositories
from app.services import payments
from app.services.package_policy import PACKAGE_CATALOG_PATH

ROOT = Path(__file__).resolve().parents[1]


def _package(*, price: str = "100", enabled: bool = True, credits: int = 10) -> SimpleNamespace:
    return SimpleNamespace(
        id=1,
        code="regression-package",
        title="Regression Package",
        is_enabled=enabled,
        credits=credits,
        photo_credits=0,
        video_credits=0,
        is_unlimited=False,
        duration_days=None,
        price_rub=Decimal(price),
    )


async def run_dynamic_package_regression() -> None:
    assert repositories.package_is_user_visible(_package())
    assert not repositories.package_is_user_visible(_package(price="0"))
    assert not repositories.package_is_user_visible(_package(price="-1"))
    assert not repositories.package_is_user_visible(_package(enabled=False))
    assert not repositories.package_is_user_visible(_package(credits=0))

    try:
        await payments.create_custom_credit_payment(
            SimpleNamespace(),
            user_id=1,
            credits=10,
            source="regression",
        )
    except payments.PaymentCreditAmountInvalid as exc:
        assert str(exc) == "custom_credit_sales_disabled"
    else:
        raise AssertionError("custom universal-credit payment must be disabled")

    test_app = FastAPI()

    @test_app.get(PACKAGE_CATALOG_PATH)
    async def package_catalog() -> dict[str, list[object]]:
        return {"items": []}

    transport = httpx.ASGITransport(app=test_app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get(PACKAGE_CATALOG_PATH)
    assert response.status_code == 200
    assert response.headers.get("cache-control") == "no-store, max-age=0"
    assert response.headers.get("pragma") == "no-cache"
    assert response.headers.get("expires") == "0"

    index_source = (ROOT / "app/static/miniapp/index.html").read_text(encoding="utf-8")
    runtime_source = (
        ROOT / "app/static/miniapp/assets/package-catalog-runtime.js"
    ).read_text(encoding="utf-8")
    services_init = (ROOT / "app/services/__init__.py").read_text(encoding="utf-8")

    app_script = index_source.index("assets/app.js")
    package_script = index_source.index("assets/package-catalog-runtime.js")
    assert app_script < package_script
    assert "install_package_policy()" in services_init
    assert 'cache: "no-store"' in runtime_source
    assert 'document.addEventListener("visibilitychange"' in runtime_source
    assert 'window.addEventListener("focus"' in runtime_source
    assert 'JSON.stringify({ package_id:' in runtime_source
    assert 'JSON.stringify({ credits' not in runtime_source
    assert 'action === "pay-custom"' in runtime_source
    assert "Свободная покупка универсальных кредитов отключена" in runtime_source
    assert "moneyParts" in runtime_source


async def amain() -> None:
    await run_dynamic_package_regression()
    print("dynamic package regression: ok")


if __name__ == "__main__":
    asyncio.run(amain())
