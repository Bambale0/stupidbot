from __future__ import annotations

import asyncio
from collections import defaultdict
from pathlib import Path
from typing import Any

import httpx
from fastapi import FastAPI, Request

if __package__ in {None, ""}:
    from _bootstrap import add_project_root_to_path

    add_project_root_to_path()

from app.services.tma_request_integrity import install_tma_request_integrity

ROOT = Path(__file__).resolve().parents[1]


class FakeRedis:
    def __init__(self) -> None:
        self.values: dict[bytes, bytes] = {}
        self.counts: defaultdict[bytes, int] = defaultdict(int)

    async def incr(self, key: bytes) -> int:
        self.counts[key] += 1
        return self.counts[key]

    async def expire(self, key: bytes, seconds: int) -> bool:
        del key, seconds
        return True

    async def get(self, key: bytes) -> bytes | None:
        return self.values.get(key)

    async def set(
        self,
        key: bytes,
        value: bytes,
        *,
        ex: int | None = None,
        nx: bool = False,
    ) -> bool:
        del ex
        if nx and key in self.values:
            return False
        self.values[key] = value
        return True

    async def delete(self, key: bytes) -> int:
        return 1 if self.values.pop(key, None) is not None else 0


async def run_tma_request_integrity_regression() -> None:
    install_tma_request_integrity()
    app = FastAPI()
    app.state.redis = FakeRedis()
    calls = {"payments": 0, "feed": 0}

    @app.post("/api/tma/app/payments")
    async def payment(request: Request) -> dict[str, Any]:
        calls["payments"] += 1
        return {"ok": True, "package_id": (await request.json())["package_id"]}

    @app.post("/api/tma/app/feed/42/action")
    async def feed_action(request: Request) -> dict[str, Any]:
        calls["feed"] += 1
        return {"ok": True, "action": (await request.json())["action"]}

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post("/api/tma/app/payments", content=b"{}")
        assert response.status_code == 415

        headers = {
            "Content-Type": "application/json",
            "X-Telegram-Init-Data": "signed-user-one",
            "X-Idempotency-Key": "payment_attempt_123456789",
        }
        response = await client.post(
            "/api/tma/app/payments",
            headers=headers,
            json={"package_id": 1, "credits": 1000},
        )
        assert response.status_code == 400
        assert calls["payments"] == 0

        missing_key_headers = {
            "Content-Type": "application/json",
            "X-Telegram-Init-Data": "signed-user-two",
        }
        response = await client.post(
            "/api/tma/app/payments",
            headers=missing_key_headers,
            json={"package_id": 1},
        )
        assert response.status_code == 400
        assert "Idempotency" in response.json()["detail"]

        first = await client.post(
            "/api/tma/app/payments",
            headers=headers,
            json={"package_id": 7},
        )
        second = await client.post(
            "/api/tma/app/payments",
            headers=headers,
            json={"package_id": 7},
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json() == second.json() == {"ok": True, "package_id": 7}
        assert second.headers.get("x-idempotent-replay") == "true"
        assert calls["payments"] == 1

        response = await client.post(
            "/api/tma/app/feed/42/action",
            headers={
                "Content-Type": "application/json",
                "X-Telegram-Init-Data": "signed-feed-user",
            },
            json={"action": "delete", "prompt": "must never be accepted"},
        )
        assert response.status_code == 400
        assert calls["feed"] == 0

        response = await client.post(
            "/api/tma/app/feed/42/action",
            headers={
                "Content-Type": "application/json",
                "X-Telegram-Init-Data": "signed-feed-user",
            },
            json={"action": "like"},
        )
        assert response.status_code == 200
        assert calls["feed"] == 1

        rate_headers = {
            "Content-Type": "application/json",
            "X-Telegram-Init-Data": "signed-rate-user",
        }
        for _ in range(60):
            response = await client.post(
                "/api/tma/app/feed/42/action",
                headers=rate_headers,
                json={"action": "like"},
            )
            assert response.status_code == 200
        response = await client.post(
            "/api/tma/app/feed/42/action",
            headers=rate_headers,
            json={"action": "like"},
        )
        assert response.status_code == 429
        assert response.headers.get("retry-after") == "60"

    frontend = (
        ROOT / "app/static/miniapp/assets/package-catalog-runtime.js"
    ).read_text(encoding="utf-8")
    feed_service = (ROOT / "app/services/feed_social.py").read_text(encoding="utf-8")
    main_source = (ROOT / "app/main.py").read_text(encoding="utf-8")

    assert '"X-Idempotency-Key"' in frontend
    assert "crypto.randomUUID" in frontend
    assert "JSON.stringify({ package_id:" in frontend
    assert "JSON.stringify({ credits" not in frontend
    assert '"prompt": task.prompt' not in feed_service
    assert '"prompt": task.prompt' not in main_source


async def amain() -> None:
    await run_tma_request_integrity_regression()
    print("TMA request integrity regression passed")


if __name__ == "__main__":
    asyncio.run(amain())
