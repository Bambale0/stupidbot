from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from fastapi import FastAPI

if __package__ in {None, ""}:
    from _bootstrap import add_project_root_to_path

    add_project_root_to_path()

import app.bot  # noqa: F401,E402
from app.readiness import (  # noqa: E402
    install_http_readiness_route,
    readiness_payload,
    readiness_response,
    tracker_is_running,
)


class _Result:
    def __init__(self, value: int) -> None:
        self.value = value

    def scalar_one(self) -> int:
        return self.value


class _Connection:
    def __init__(self, value: int = 1, *, fail: bool = False) -> None:
        self.value = value
        self.fail = fail

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        return None

    async def execute(self, statement):
        del statement
        if self.fail:
            raise RuntimeError("database unavailable")
        return _Result(self.value)


class _Engine:
    def __init__(self, value: int = 1, *, fail: bool = False) -> None:
        self.value = value
        self.fail = fail

    def connect(self) -> _Connection:
        return _Connection(self.value, fail=self.fail)


class _Redis:
    def __init__(self, value: bool = True, *, fail: bool = False) -> None:
        self.value = value
        self.fail = fail

    async def ping(self) -> bool:
        if self.fail:
            raise RuntimeError("redis unavailable")
        return self.value


class _Bot:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    async def get_me(self):
        if self.fail:
            raise RuntimeError("telegram unavailable")
        return SimpleNamespace(id=42, username="stupidbot_test")


def _response_payload(response) -> dict:
    return json.loads(bytes(response.body).decode("utf-8"))


async def amain() -> None:
    install_http_readiness_route()
    install_http_readiness_route()
    app = FastAPI()
    assert [route.path for route in app.routes].count("/ready") == 1

    stop_event = asyncio.Event()
    background = asyncio.create_task(stop_event.wait())
    tracker = SimpleNamespace(_task=background, _stop=stop_event)
    assert tracker_is_running(tracker)

    ready = await readiness_payload(
        engine=_Engine(),
        redis=_Redis(),
        bot=_Bot(),
        tracker=tracker,
    )
    assert ready["status"] == "ready"
    assert ready["checks"] == {
        "database": "ok",
        "redis": "ok",
        "telegram": "ok",
        "tracker": "ok",
    }
    assert set(ready["latency_ms"]) == {"database", "redis", "telegram", "tracker"}

    request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                engine=_Engine(),
                redis=_Redis(),
                bot=_Bot(),
                tracker=tracker,
            )
        )
    )
    ready_response = await readiness_response(request)
    assert ready_response.status_code == 200
    assert _response_payload(ready_response)["checks"] == ready["checks"]

    failed = await readiness_payload(
        engine=_Engine(fail=True),
        redis=_Redis(fail=True),
        bot=_Bot(fail=True),
        tracker=SimpleNamespace(_task=None, _stop=asyncio.Event()),
    )
    assert failed["status"] == "not_ready"
    assert failed["checks"] == {
        "database": "error",
        "redis": "error",
        "telegram": "error",
        "tracker": "error",
    }

    failed_request = SimpleNamespace(
        app=SimpleNamespace(
            state=SimpleNamespace(
                engine=_Engine(fail=True),
                redis=_Redis(),
                bot=_Bot(),
                tracker=tracker,
            )
        )
    )
    failed_response = await readiness_response(failed_request)
    assert failed_response.status_code == 503
    assert _response_payload(failed_response)["status"] == "not_ready"

    stop_event.set()
    await background
    assert not tracker_is_running(tracker)
    print("HTTP readiness regression passed")


if __name__ == "__main__":
    asyncio.run(amain())
