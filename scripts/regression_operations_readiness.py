from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

if __package__ in {None, ""}:
    from _bootstrap import add_project_root_to_path

    add_project_root_to_path()

from app.readiness import readiness_payload


class _Result:
    def scalar_one(self) -> int:
        return 1


class _Connection:
    async def execute(self, _query):
        return _Result()


class _ConnectionContext:
    async def __aenter__(self):
        return _Connection()

    async def __aexit__(self, exc_type, exc, traceback):
        return False


class _Engine:
    def connect(self):
        return _ConnectionContext()


class _Redis:
    async def ping(self) -> bool:
        return True


class _Bot:
    async def get_me(self):
        return SimpleNamespace(id=123456, username="stupidbot_test")


class _BrokenBot:
    async def get_me(self):
        raise RuntimeError("telegram unavailable")


class _Task:
    def done(self) -> bool:
        return False


class _Stop:
    def is_set(self) -> bool:
        return False


class _Tracker:
    _task = _Task()
    _stop = _Stop()


async def amain() -> None:
    ready = await readiness_payload(
        engine=_Engine(),
        redis=_Redis(),
        bot=_Bot(),
        tracker=_Tracker(),
    )
    assert ready["status"] == "ready"
    assert ready["checks"] == {
        "database": "ok",
        "redis": "ok",
        "telegram": "ok",
        "tracker": "ok",
    }
    assert set(ready["latency_ms"]) == {"database", "redis", "telegram", "tracker"}

    unavailable = await readiness_payload(
        engine=_Engine(),
        redis=_Redis(),
        bot=_BrokenBot(),
        tracker=_Tracker(),
    )
    assert unavailable["status"] == "not_ready"
    assert unavailable["checks"]["telegram"] == "error"

    operations_source = Path("app/operations.py").read_text(encoding="utf-8")
    readiness_source = Path("app/readiness.py").read_text(encoding="utf-8")
    bot_source = Path("app/bot.py").read_text(encoding="utf-8")
    config_source = Path("app/config.py").read_text(encoding="utf-8")

    required_operations_contracts = (
        'OPERATIONS_METRICS_PATH = "/ops/metrics"',
        'METRICS_REDIS_KEY = "ops:http:metrics"',
        'return "package_catalog"',
        'return "payment_create"',
        'return "provider_callback"',
        'return "payment_callback"',
        'request.headers.get("x-operations-token")',
        'authorization.lower().startswith("bearer ")',
        "hmac.compare_digest(supplied, expected)",
        '"generation_tasks"',
        '"payments"',
        '"broadcasts"',
        '"tracker_running"',
        '"Cache-Control": "no-store, max-age=0"',
    )
    for contract in required_operations_contracts:
        assert contract in operations_source, contract

    assert "request.body" not in operations_source
    assert "telegram_id" not in operations_source
    assert "prompt" not in operations_source
    assert "_check_telegram" in readiness_source
    assert 'status_code=200 if payload["status"] == "ready" else 503' in readiness_source
    assert "install_http_operations_routes()" in bot_source
    assert "operations_token: str | None = None" in config_source
    print("Operations readiness and low-cardinality metrics regression passed")


if __name__ == "__main__":
    asyncio.run(amain())
