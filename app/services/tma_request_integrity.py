from __future__ import annotations

import hashlib
import json
import logging
import re
from dataclasses import dataclass
from typing import Any, Awaitable, Callable

from fastapi import FastAPI

logger = logging.getLogger(__name__)

_PAYMENT_PATH = "/api/tma/app/payments"
_FEED_ACTION_PREFIX = "/api/tma/app/feed/"
_FEED_ACTION_SUFFIX = "/action"
_IDEMPOTENCY_PATTERN = re.compile(r"^[A-Za-z0-9_-]{16,128}$")
_INSTALL_MARKER = "_stupidbot_tma_request_integrity_installed"


@dataclass(frozen=True, slots=True)
class _JsonPolicy:
    max_bytes: int
    allowed_keys: frozenset[str]
    rate_limit: int


_PAYMENT_POLICY = _JsonPolicy(
    max_bytes=4096,
    allowed_keys=frozenset({"package_id"}),
    rate_limit=10,
)
_FEED_POLICY = _JsonPolicy(
    max_bytes=2048,
    allowed_keys=frozenset({"action"}),
    rate_limit=60,
)


def _headers(scope: dict[str, Any]) -> dict[str, str]:
    return {
        key.decode("latin-1").lower(): value.decode("latin-1")
        for key, value in scope.get("headers") or []
    }


def _json_response(status: int, detail: str, *, retry_after: int | None = None) -> list[dict[str, Any]]:
    body = json.dumps({"detail": detail}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers: list[tuple[bytes, bytes]] = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store, max-age=0"),
    ]
    if retry_after is not None:
        headers.append((b"retry-after", str(retry_after).encode("ascii")))
    return [
        {"type": "http.response.start", "status": status, "headers": headers},
        {"type": "http.response.body", "body": body, "more_body": False},
    ]


async def _send_messages(
    send: Callable[[dict[str, Any]], Awaitable[None]],
    messages: list[dict[str, Any]],
) -> None:
    for message in messages:
        await send(message)


async def _read_body(receive: Callable[[], Awaitable[dict[str, Any]]], max_bytes: int) -> bytes:
    chunks: list[bytes] = []
    total = 0
    while True:
        message = await receive()
        if message.get("type") == "http.disconnect":
            raise ConnectionError("client_disconnected")
        if message.get("type") != "http.request":
            continue
        chunk = bytes(message.get("body") or b"")
        total += len(chunk)
        if total > max_bytes:
            raise OverflowError("request_body_too_large")
        chunks.append(chunk)
        if not message.get("more_body", False):
            return b"".join(chunks)


def _replay_body(body: bytes) -> Callable[[], Awaitable[dict[str, Any]]]:
    delivered = False

    async def replay() -> dict[str, Any]:
        nonlocal delivered
        if delivered:
            return {"type": "http.request", "body": b"", "more_body": False}
        delivered = True
        return {"type": "http.request", "body": body, "more_body": False}

    return replay


def _request_identity(scope: dict[str, Any], headers: dict[str, str]) -> str:
    init_data = headers.get("x-telegram-init-data") or ""
    authorization = headers.get("authorization") or ""
    source = init_data or authorization
    if not source:
        client = scope.get("client") or ("unknown", 0)
        source = str(client[0])
    return hashlib.sha256(source.encode("utf-8", errors="ignore")).hexdigest()


def _validate_payload(path: str, payload: Any, policy: _JsonPolicy) -> str | None:
    if not isinstance(payload, dict):
        return "JSON object is required"
    unknown = set(payload) - set(policy.allowed_keys)
    missing = set(policy.allowed_keys) - set(payload)
    if unknown or missing:
        return "Unsupported request fields"
    if path == _PAYMENT_PATH:
        raw_package_id = payload.get("package_id")
        if isinstance(raw_package_id, bool):
            return "Invalid package id"
        try:
            package_id = int(raw_package_id)
        except (TypeError, ValueError):
            return "Invalid package id"
        if package_id <= 0 or str(raw_package_id).strip() != str(package_id):
            return "Invalid package id"
    else:
        action = str(payload.get("action") or "").strip().lower()
        if action not in {"like", "share", "publish", "remove"}:
            return "Unsupported feed action"
    return None


async def _rate_limited(redis: Any, *, identity: str, path: str, limit: int) -> bool:
    key_path = "payment" if path == _PAYMENT_PATH else "feed"
    key = f"tma:rate:{key_path}:{identity}".encode("utf-8")
    count = int(await redis.incr(key))
    if count == 1:
        await redis.expire(key, 60)
    return count > limit


def _cached_messages(payload: bytes) -> list[dict[str, Any]] | None:
    try:
        cached = json.loads(payload.decode("utf-8"))
        status = int(cached["status"])
        body = str(cached["body"]).encode("utf-8")
        content_type = str(cached.get("content_type") or "application/json; charset=utf-8")
    except (KeyError, TypeError, ValueError, json.JSONDecodeError, UnicodeDecodeError):
        return None
    return [
        {
            "type": "http.response.start",
            "status": status,
            "headers": [
                (b"content-type", content_type.encode("latin-1", errors="ignore")),
                (b"content-length", str(len(body)).encode("ascii")),
                (b"cache-control", b"no-store, max-age=0"),
                (b"x-idempotent-replay", b"true"),
            ],
        },
        {"type": "http.response.body", "body": body, "more_body": False},
    ]


class TmaRequestIntegrityMiddleware:
    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(
        self,
        scope: dict[str, Any],
        receive: Callable[[], Awaitable[dict[str, Any]]],
        send: Callable[[dict[str, Any]], Awaitable[None]],
    ) -> None:
        path = str(scope.get("path") or "")
        method = str(scope.get("method") or "").upper()
        is_payment = path == _PAYMENT_PATH
        is_feed_action = path.startswith(_FEED_ACTION_PREFIX) and path.endswith(_FEED_ACTION_SUFFIX)
        if scope.get("type") != "http" or method != "POST" or not (is_payment or is_feed_action):
            await self.app(scope, receive, send)
            return

        policy = _PAYMENT_POLICY if is_payment else _FEED_POLICY
        headers = _headers(scope)
        content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
        if content_type != "application/json":
            await _send_messages(send, _json_response(415, "application/json is required"))
            return

        try:
            body = await _read_body(receive, policy.max_bytes)
        except OverflowError:
            await _send_messages(send, _json_response(413, "Request body is too large"))
            return
        except ConnectionError:
            return

        try:
            payload = json.loads(body.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            await _send_messages(send, _json_response(400, "Invalid JSON"))
            return
        validation_error = _validate_payload(path, payload, policy)
        if validation_error:
            await _send_messages(send, _json_response(400, validation_error))
            return

        app = scope.get("app")
        redis = getattr(getattr(app, "state", None), "redis", None)
        if redis is None:
            await _send_messages(send, _json_response(503, "Request protection is unavailable"))
            return

        identity = _request_identity(scope, headers)
        if await _rate_limited(redis, identity=identity, path=path, limit=policy.rate_limit):
            logger.warning("tma_rate_limited path=%s identity=%s", path, identity[:12])
            await _send_messages(send, _json_response(429, "Too many requests", retry_after=60))
            return

        if not is_payment:
            await self.app(scope, _replay_body(body), send)
            return

        idempotency_key = headers.get("x-idempotency-key", "").strip()
        if not _IDEMPOTENCY_PATTERN.fullmatch(idempotency_key):
            await _send_messages(send, _json_response(400, "Valid X-Idempotency-Key is required"))
            return

        fingerprint = hashlib.sha256(f"{identity}:{idempotency_key}".encode("utf-8")).hexdigest()
        response_key = f"tma:payment:response:{fingerprint}".encode("utf-8")
        lock_key = f"tma:payment:lock:{fingerprint}".encode("utf-8")
        cached = await redis.get(response_key)
        if cached:
            messages = _cached_messages(bytes(cached))
            if messages:
                await _send_messages(send, messages)
                return

        lock_acquired = bool(await redis.set(lock_key, b"1", ex=120, nx=True))
        if not lock_acquired:
            await _send_messages(send, _json_response(409, "Payment request is already in progress", retry_after=2))
            return

        status = 500
        response_headers: list[tuple[bytes, bytes]] = []
        response_chunks: list[bytes] = []

        async def capture_send(message: dict[str, Any]) -> None:
            nonlocal status, response_headers
            if message.get("type") == "http.response.start":
                status = int(message.get("status") or 500)
                response_headers = list(message.get("headers") or [])
            elif message.get("type") == "http.response.body":
                response_chunks.append(bytes(message.get("body") or b""))
            await send(message)

        try:
            await self.app(scope, _replay_body(body), capture_send)
            if 200 <= status < 300:
                content_type_value = "application/json; charset=utf-8"
                for key, value in response_headers:
                    if key.lower() == b"content-type":
                        content_type_value = value.decode("latin-1")
                        break
                cache_payload = json.dumps(
                    {
                        "status": status,
                        "body": b"".join(response_chunks).decode("utf-8", errors="replace"),
                        "content_type": content_type_value,
                    },
                    ensure_ascii=False,
                    separators=(",", ":"),
                ).encode("utf-8")
                await redis.set(response_key, cache_payload, ex=24 * 60 * 60)
        finally:
            await redis.delete(lock_key)


def install_tma_request_integrity() -> None:
    if getattr(FastAPI, _INSTALL_MARKER, False):
        return
    original_init = FastAPI.__init__

    def init_with_integrity(self: FastAPI, *args: Any, **kwargs: Any) -> None:
        original_init(self, *args, **kwargs)
        self.add_middleware(TmaRequestIntegrityMiddleware)

    FastAPI.__init__ = init_with_integrity  # type: ignore[method-assign]
    setattr(FastAPI, _INSTALL_MARKER, True)
