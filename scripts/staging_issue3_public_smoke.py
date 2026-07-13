from __future__ import annotations

import asyncio

import httpx

if __package__ in {None, ""}:
    from _bootstrap import add_project_root_to_path

    add_project_root_to_path()

from app.config import get_settings


async def amain() -> None:
    settings = get_settings()
    base_url = settings.public_base_url.rstrip("/")
    assert base_url.startswith("https://"), "PUBLIC_BASE_URL must use HTTPS"
    miniapp_path = settings.mini_app_route

    async with httpx.AsyncClient(
        base_url=base_url,
        timeout=20.0,
        follow_redirects=True,
    ) as client:
        health = await client.get("/health")
        health.raise_for_status()
        assert health.json() == {"status": "ok"}

        miniapp = await client.get(f"{miniapp_path}/")
        miniapp.raise_for_status()
        assert "runtime-sync.js" in miniapp.text

        runtime = await client.get(f"{miniapp_path}/assets/runtime-sync.js")
        runtime.raise_for_status()
        for marker in (
            "partnerCodeForTelegramId",
            "ref_",
            "custom-credit-panel",
            'balanceButton.textContent = "Пополнить"',
        ):
            assert marker in runtime.text
        assert "localStorage" not in runtime.text

        packages = await client.get("/api/tma/app/packages")
        packages.raise_for_status()
        payload = packages.json()
        assert isinstance(payload.get("items"), list)
        for package in payload["items"]:
            assert float(package.get("price_rub") or 0) >= 0
            assert not bool(package.get("is_unlimited"))

    print("staging public smoke passed: health, Mini App runtime and backend packages")


if __name__ == "__main__":
    asyncio.run(amain())
