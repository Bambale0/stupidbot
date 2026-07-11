from __future__ import annotations

import logging
import mimetypes
import asyncio
import hashlib
import hmac
import json
import time
from html import escape
from io import BytesIO
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl

import uvicorn
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest, TelegramForbiddenError
from aiogram.types import Update
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from redis.asyncio import Redis

from app.bot import create_bot, create_dispatcher, register_bot_commands
from app.config import Settings, get_settings
from app.context import AppContext
from app.db import build_engine, build_session_factory, init_db, session_scope
from app.repositories import (
    ensure_defaults,
    credit_package_snapshot,
    get_feed_tasks,
    get_user_generation_tasks,
    increment_feed_share,
    like_feed_task,
    list_packages,
    remove_task_from_feed,
    serialize_feed_task,
    serialize_user_generation_task,
    share_task_to_feed,
)
from app.models import GenerationTask, User
from sqlalchemy import select
from app.services.comet import CometClient
from app.services.kie import KieClient
from app.services.payments import (
    CUSTOM_CREDIT_MAX_AMOUNT,
    CUSTOM_CREDIT_MIN_AMOUNT,
    CUSTOM_CREDIT_PRICE_RUB,
    PaymentCreditAmountInvalid,
    PaymentPackageUnavailable,
    PaymentProviderError,
    create_custom_credit_payment,
    create_package_payment,
    handle_tbank_notification,
)
from app.services.task_tracker import TaskTracker
from app.services.tbank import TBankClient
from app.ui import package_credits_text

logger = logging.getLogger(__name__)
MINI_APP_DIR = Path(__file__).resolve().parent / "static" / "miniapp"
mimetypes.add_type("image/webp", ".webp")
TMA_AUTH_MAX_AGE_SECONDS = 24 * 60 * 60
TMA_MEDIA_TOKEN_TTL_SECONDS = 60 * 60
TMA_MEDIA_TOKEN_REFRESH_SECONDS = 15 * 60


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    logging.basicConfig(level=getattr(logging, settings.log_level.upper(), logging.INFO))

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if not settings.telegram_bot_token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is required")

        engine = build_engine(settings)
        session_factory = build_session_factory(engine)
        redis = Redis.from_url(settings.redis_url, decode_responses=False)

        if settings.auto_create_db:
            await init_db(engine)
            async with session_scope(session_factory) as session:
                await ensure_defaults(session, settings.admin_ids)

        comet = CometClient(
            api_key=settings.comet_api_key,
            base_url=settings.comet_base_url,
        )
        kie = KieClient(
            api_key=settings.kie_api_key,
            base_url=settings.kie_base_url,
            upload_base_url=settings.kie_upload_base_url,
        )
        tbank = TBankClient(
            terminal_key=settings.tbank_terminal_key,
            password=settings.tbank_password,
            success_url=settings.tbank_success_url
            or f"{settings.public_base_url.rstrip('/')}/payments/success",
            fail_url=settings.tbank_fail_url
            or f"{settings.public_base_url.rstrip('/')}/payments/fail",
        )
        context = AppContext(
            settings=settings,
            session_factory=session_factory,
            redis=redis,
            comet=comet,
            kie=kie,
            tbank=tbank,
        )
        bot = create_bot(settings)
        dispatcher = create_dispatcher(context, redis)
        context.bot = bot
        context.dispatcher = dispatcher

        tracker = TaskTracker(context, bot)
        tracker.start()
        logger.info("app_startup public_base_url=%s webhook_enabled=%s", settings.public_base_url, settings.telegram_set_webhook)

        await register_bot_commands(bot, settings)

        if settings.telegram_set_webhook:
            await bot.set_webhook(
                settings.telegram_webhook_url,
                secret_token=settings.telegram_secret_token,
                drop_pending_updates=False,
            )

        app.state.context = context
        app.state.bot = bot
        app.state.dispatcher = dispatcher
        app.state.engine = engine
        app.state.redis = redis
        app.state.tracker = tracker

        try:
            yield
        finally:
            logger.info("app_shutdown")
            await tracker.stop()
            await bot.session.close()
            await redis.aclose()
            await engine.dispose()

    app = FastAPI(title="StupidBot Telegram Webhook", version="0.1.0", lifespan=lifespan)

    if MINI_APP_DIR.exists():
        mini_app_route = settings.mini_app_route
        app.mount(
            f"{mini_app_route}/assets",
            StaticFiles(directory=MINI_APP_DIR / "assets"),
            name="miniapp-assets",
        )

        @app.get(f"{mini_app_route}/", include_in_schema=False)
        async def mini_app_index() -> FileResponse:
            return FileResponse(
                MINI_APP_DIR / "index.html",
                headers={"Cache-Control": "no-store, max-age=0"},
            )

        @app.get(mini_app_route, include_in_schema=False)
        async def mini_app_redirect() -> RedirectResponse:
            return RedirectResponse(f"{mini_app_route}/", status_code=308)

    @app.get("/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/api/tma/app/feed")
    async def tma_feed(request: Request, limit: int = 40) -> dict[str, Any]:
        async with session_scope(request.app.state.context.session_factory) as session:
            tasks = await get_feed_tasks(session, limit=limit)
            rows = [await serialize_feed_task(session, task) for task in tasks]
        return {"items": rows}

    @app.get("/api/tma/app/packages")
    async def tma_packages(request: Request) -> dict[str, Any]:
        async with session_scope(request.app.state.context.session_factory) as session:
            packages = await list_packages(session, only_enabled=True)
            rows = []
            for package in packages:
                snapshot = credit_package_snapshot(package)
                rows.append(
                    {
                        **snapshot,
                        "id": package.id,
                        "amount_text": package_credits_text(package),
                        "price_rub": float(package.price_rub),
                    }
                )
        return {
            "items": rows,
            "custom_credit_price_rub": float(CUSTOM_CREDIT_PRICE_RUB),
            "custom_credit_min": CUSTOM_CREDIT_MIN_AMOUNT,
            "custom_credit_max": CUSTOM_CREDIT_MAX_AMOUNT,
        }

    @app.post("/api/tma/app/payments")
    async def tma_create_payment(request: Request) -> dict[str, Any]:
        user = await _tma_request_user(request)
        if not user:
            raise HTTPException(status_code=401, detail="Telegram user is required")
        payload: dict[str, Any] = await request.json()
        raw_credits = payload.get("credits")
        if raw_credits is not None:
            try:
                credits = int(raw_credits)
            except (TypeError, ValueError):
                raise HTTPException(status_code=400, detail="Invalid credit amount") from None
            try:
                result = await create_custom_credit_payment(
                    request.app.state.context,
                    user_id=user.id,
                    credits=credits,
                    customer_key=str(user.telegram_id),
                    source="miniapp",
                )
            except PaymentCreditAmountInvalid:
                raise HTTPException(status_code=400, detail="Invalid credit amount") from None
            except PaymentPackageUnavailable:
                raise HTTPException(status_code=404, detail="User not found") from None
            except PaymentProviderError:
                logger.exception("TMA custom credit payment creation failed")
                raise HTTPException(status_code=502, detail="Payment provider error") from None

            return {
                "ok": True,
                "payment_id": result.payment_id,
                "order_id": result.order_id,
                "status": result.status,
                "payment_url": result.payment_url,
            }

        try:
            package_id = int(payload.get("package_id") or payload.get("id") or 0)
        except (TypeError, ValueError):
            raise HTTPException(status_code=400, detail="Invalid package id") from None
        if package_id <= 0:
            raise HTTPException(status_code=400, detail="Invalid package id")

        try:
            result = await create_package_payment(
                request.app.state.context,
                user_id=user.id,
                package_id=package_id,
                customer_key=str(user.telegram_id),
                source="miniapp",
            )
        except PaymentPackageUnavailable:
            raise HTTPException(status_code=404, detail="Package not found") from None
        except PaymentProviderError:
            logger.exception("TMA payment creation failed")
            raise HTTPException(status_code=502, detail="Payment provider error") from None

        return {
            "ok": True,
            "payment_id": result.payment_id,
            "order_id": result.order_id,
            "status": result.status,
            "payment_url": result.payment_url,
        }

    @app.get("/api/tma/app/tasks")
    async def tma_tasks(request: Request, limit: int = 30) -> dict[str, Any]:
        user = await _tma_request_user(request)
        if not user:
            raise HTTPException(status_code=401, detail="Telegram user is required")
        async with session_scope(request.app.state.context.session_factory) as session:
            tasks = await get_user_generation_tasks(session, user_id=user.id, limit=limit)
            rows = [_serialize_tma_task(task, request, user) for task in tasks]
        return {"items": rows}

    @app.get("/api/tma/app/tasks/stream")
    async def tma_tasks_stream(request: Request, limit: int = 30) -> StreamingResponse:
        user = await _tma_request_user(request)
        if not user:
            raise HTTPException(status_code=401, detail="Telegram user is required")

        async def events():
            last_signature = ""
            while not await request.is_disconnected():
                async with session_scope(request.app.state.context.session_factory) as session:
                    tasks = await get_user_generation_tasks(session, user_id=user.id, limit=limit)
                    rows = [_serialize_tma_task(task, request, user) for task in tasks]
                signature = json.dumps(
                    [
                        (row["id"], row["status"], row.get("updated_at"), row.get("media_url"))
                        for row in rows
                    ],
                    sort_keys=True,
                    ensure_ascii=True,
                )
                if signature != last_signature:
                    last_signature = signature
                    yield f"event: tasks\ndata: {json.dumps({'items': rows}, ensure_ascii=False)}\n\n"
                await asyncio.sleep(3)

        return StreamingResponse(
            events(),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-store",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/tma/app/tasks/{task_id}/media/{index}")
    async def tma_task_media(task_id: int, index: int, request: Request) -> StreamingResponse:
        user = await _tma_request_user(request)
        if not user:
            user = await _tma_media_request_user(request, task_id=task_id, index=index)
        if not user:
            raise HTTPException(status_code=401, detail="Telegram user is required")
        async with session_scope(request.app.state.context.session_factory) as session:
            task = await session.get(GenerationTask, task_id)
            if not task or task.user_id != user.id:
                raise HTTPException(status_code=404, detail="Generation task not found")
            if index < 0 or index >= len(task.result_urls or []):
                raise HTTPException(status_code=404, detail="Result media not found")
            media_ref = str(task.result_urls[index])
            media_type = "video/mp4" if "video" in task.model_code else "image/jpeg"
        if media_ref.startswith(("http://", "https://")):
            return RedirectResponse(media_ref)
        file = await request.app.state.bot.get_file(media_ref)
        buffer = BytesIO()
        await request.app.state.bot.download_file(file.file_path, destination=buffer)
        buffer.seek(0)
        return StreamingResponse(buffer, media_type=media_type)

    @app.post("/api/tma/app/feed/{task_id}/action")
    async def tma_feed_action(task_id: int, request: Request) -> dict[str, Any]:
        payload: dict[str, Any] = await request.json()
        action = str(payload.get("action") or "").lower()
        user = await _tma_request_user(request)
        if not user:
            raise HTTPException(status_code=401, detail="Telegram user is required")
        async with session_scope(request.app.state.context.session_factory) as session:
            if action == "like":
                likes, is_new = await like_feed_task(session, task_id=task_id, user_id=user.id)
                if likes is None:
                    raise HTTPException(status_code=404, detail="Feed task not found")
                return {"ok": True, "likes": likes, "new": is_new}
            if action == "share":
                shares = await increment_feed_share(session, task_id)
                if shares is None:
                    raise HTTPException(status_code=404, detail="Feed task not found")
                return {"ok": True, "shares": shares}
            if action == "publish":
                ok, reason = await share_task_to_feed(session, task_id=task_id, user_id=user.id)
                if not ok:
                    raise HTTPException(status_code=400, detail=reason)
                return {"ok": True, "status": reason}
            if action == "remove":
                ok = await remove_task_from_feed(session, task_id=task_id, user_id=user.id)
                if not ok:
                    raise HTTPException(status_code=400, detail="not_owner")
                return {"ok": True}
        raise HTTPException(status_code=400, detail="Unsupported feed action")

    @app.get("/payments/success")
    async def payment_success() -> HTMLResponse:
        return HTMLResponse(
            _payment_return_page(settings, success=True),
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    @app.get("/payments/fail")
    async def payment_fail() -> HTMLResponse:
        return HTMLResponse(
            _payment_return_page(settings, success=False),
            headers={"Cache-Control": "no-store, max-age=0"},
        )

    async def telegram_webhook(request: Request) -> dict[str, bool]:
        if settings.telegram_secret_token:
            secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
            if secret != settings.telegram_secret_token:
                raise HTTPException(status_code=403, detail="Invalid Telegram secret token")

        payload = await request.json()
        update = Update.model_validate(payload, context={"bot": request.app.state.bot})
        try:
            await request.app.state.dispatcher.feed_update(
                request.app.state.bot,
                update,
                context=request.app.state.context,
            )
        except TelegramBadRequest as exc:
            message = str(exc)
            stale_callback = (
                "query is too old" in message
                or "response timeout expired" in message
                or "query ID is invalid" in message
            )
            if not stale_callback:
                logger.exception("Telegram API error while processing update")
                await _notify_update_failed(request.app.state.bot, update)
                return {"ok": True}
            logger.warning("Ignoring stale Telegram callback answer: %s", message)
        except Exception:
            logger.exception("Unhandled error while processing Telegram update")
            await _notify_update_failed(request.app.state.bot, update)
        return {"ok": True}

    async def _notify_update_failed(bot, update: Update) -> None:
        """Never expose tracebacks/technical text to Telegram users; keep details in logs."""
        text = "Не удалось выполнить операцию. Попробуйте ещё раз через несколько минут."
        callback = getattr(update, "callback_query", None)
        if callback:
            with suppress(TelegramBadRequest, TelegramForbiddenError, TelegramAPIError, Exception):
                await callback.answer(text, show_alert=True)
                return
            message = getattr(callback, "message", None)
            chat = getattr(message, "chat", None)
            chat_id = getattr(chat, "id", None)
            if chat_id:
                with suppress(TelegramBadRequest, TelegramForbiddenError, TelegramAPIError, Exception):
                    await bot.send_message(chat_id, text)
            return
        message = getattr(update, "message", None) or getattr(update, "edited_message", None)
        chat = getattr(message, "chat", None)
        chat_id = getattr(chat, "id", None)
        if chat_id:
            with suppress(TelegramBadRequest, TelegramForbiddenError, TelegramAPIError, Exception):
                await bot.send_message(chat_id, text)

    async def _tma_request_user(request: Request) -> User | None:
        init_data = _extract_tma_init_data(request)
        if not init_data:
            return None
        parsed = _verify_tma_init_data(init_data)
        if not parsed:
            return None
        raw_user = parsed.get("user")
        if not raw_user:
            return None
        with suppress(ValueError, TypeError, json.JSONDecodeError):
            payload = json.loads(raw_user)
            telegram_id = int(payload.get("id"))
            async with session_scope(request.app.state.context.session_factory) as session:
                return await session.scalar(select(User).where(User.telegram_id == telegram_id))
        return None

    def _serialize_tma_task(task: GenerationTask, request: Request, user: User) -> dict[str, Any]:
        row = serialize_user_generation_task(task)
        media_url = str(row.get("media_url") or "")
        if media_url and not media_url.startswith(("http://", "https://")):
            expires = _tma_media_token_expiry()
            token = _sign_tma_media_token(
                telegram_id=user.telegram_id,
                task_id=task.id,
                index=0,
                expires=expires,
            )
            row["media_url"] = (
                f"/api/tma/app/tasks/{task.id}/media/0"
                f"?uid={user.telegram_id}&expires={expires}&token={token}"
            )
        return row

    async def _tma_media_request_user(request: Request, *, task_id: int, index: int) -> User | None:
        try:
            telegram_id = int(request.query_params.get("uid") or "")
            expires = int(request.query_params.get("expires") or "")
        except ValueError:
            return None
        if expires < int(time.time()):
            return None
        token = str(request.query_params.get("token") or "")
        expected = _sign_tma_media_token(
            telegram_id=telegram_id,
            task_id=task_id,
            index=index,
            expires=expires,
        )
        if not token or not hmac.compare_digest(token, expected):
            return None
        async with session_scope(request.app.state.context.session_factory) as session:
            return await session.scalar(select(User).where(User.telegram_id == telegram_id))

    def _extract_tma_init_data(request: Request) -> str:
        header = request.headers.get("x-telegram-init-data")
        if header:
            return header
        authorization = request.headers.get("authorization") or ""
        if authorization.lower().startswith("tma "):
            return authorization[4:].strip()
        return str(request.query_params.get("init_data") or "")

    def _verify_tma_init_data(init_data: str) -> dict[str, str] | None:
        pairs = dict(parse_qsl(init_data, keep_blank_values=True))
        received_hash = pairs.pop("hash", "")
        if not received_hash:
            return None
        data_check_string = "\n".join(f"{key}={value}" for key, value in sorted(pairs.items()))
        secret_key = hmac.new(
            b"WebAppData",
            settings.telegram_bot_token.encode(),
            hashlib.sha256,
        ).digest()
        expected_hash = hmac.new(
            secret_key,
            data_check_string.encode(),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(received_hash, expected_hash):
            return None
        with suppress(ValueError):
            auth_date = int(pairs.get("auth_date") or "0")
            now = int(time.time())
            if auth_date <= 0 or auth_date > now + 60 or now - auth_date > TMA_AUTH_MAX_AGE_SECONDS:
                return None
            return pairs
        return None

    def _tma_media_token_expiry() -> int:
        now = int(time.time())
        refresh_left = TMA_MEDIA_TOKEN_REFRESH_SECONDS - (now % TMA_MEDIA_TOKEN_REFRESH_SECONDS)
        return now + TMA_MEDIA_TOKEN_TTL_SECONDS + refresh_left

    def _sign_tma_media_token(
        *,
        telegram_id: int,
        task_id: int,
        index: int,
        expires: int,
    ) -> str:
        payload = f"{telegram_id}:{task_id}:{index}:{expires}"
        return hmac.new(
            settings.telegram_bot_token.encode(),
            payload.encode(),
            hashlib.sha256,
        ).hexdigest()

    async def comet_callback(request: Request) -> dict[str, bool]:
        if settings.comet_callback_secret:
            supplied = request.query_params.get("token") or request.headers.get(
                "x-comet-callback-token"
            )
            if not supplied or not hmac.compare_digest(supplied, settings.comet_callback_secret):
                raise HTTPException(status_code=403, detail="Invalid callback token")
        payload: dict[str, Any] = await request.json()
        handled = await request.app.state.tracker.apply_callback_payload(payload)
        return {"ok": handled}

    async def tbank_callback(request: Request) -> PlainTextResponse:
        payload: dict[str, Any] = await request.json()
        handled = await handle_tbank_notification(request.app.state.context, payload)
        if not handled:
            raise HTTPException(status_code=400, detail="Payment notification rejected")
        return PlainTextResponse("OK")

    app.add_api_route(settings.telegram_webhook_path, telegram_webhook, methods=["POST"])
    app.add_api_route("/comet/callback", comet_callback, methods=["POST"])
    app.add_api_route("/payments/tbank/callback", tbank_callback, methods=["POST"])
    return app


def _payment_return_page(settings: Settings, *, success: bool) -> str:
    bot_username = settings.telegram_bot_username.strip().lstrip("@") or "eva_nana_bot"
    telegram_url = f"https://t.me/{bot_username}"
    mini_app_url = settings.mini_app_url
    if success:
        title = "Оплата принята"
        eyebrow = "PAYMENT COMPLETE"
        headline = "Кредиты уже в пути"
        body = (
            "Вернитесь в Telegram. Бот пришлет уведомление и обновит баланс, "
            "как только банк подтвердит платеж."
        )
        note = "Обычно это занимает несколько секунд."
        status_class = "is-success"
        primary_label = "Открыть Telegram"
        secondary_label = "Открыть BANANA"
    else:
        title = "Оплата не завершена"
        eyebrow = "PAYMENT STOPPED"
        headline = "Платеж не прошел"
        body = "Деньги не списаны. Вернитесь в BANANA и попробуйте оплатить еще раз."
        note = "Если банк уже показал списание, дождитесь финального уведомления в Telegram."
        status_class = "is-fail"
        primary_label = "Вернуться в Telegram"
        secondary_label = "Попробовать снова"

    return f"""<!doctype html>
<html lang="ru">
  <head>
    <meta charset="utf-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover" />
    <meta name="theme-color" content="#08070b" />
    <title>{escape(title)}</title>
    <link rel="icon" href="/miniapp/assets/favicon.svg?v=20260611-riot1" type="image/svg+xml" />
    <style>
      :root {{
        color-scheme: dark;
        --bg: #08070b;
        --panel: rgba(23, 19, 34, 0.92);
        --ink: #fff7fb;
        --muted: rgba(255, 232, 244, 0.72);
        --line: rgba(255, 232, 244, 0.18);
        --pink: #ff3f9f;
        --orange: #ff9b62;
        --blue: #63cff6;
        --green: #35e681;
        --red: #ff5868;
      }}

      * {{
        box-sizing: border-box;
      }}

      html,
      body {{
        min-height: 100%;
        margin: 0;
      }}

      body {{
        display: grid;
        min-height: 100vh;
        place-items: center;
        padding: max(18px, env(safe-area-inset-top)) max(14px, env(safe-area-inset-right))
          max(18px, env(safe-area-inset-bottom)) max(14px, env(safe-area-inset-left));
        overflow-x: hidden;
        background:
          linear-gradient(180deg, rgba(7, 17, 33, 0.34), rgba(5, 8, 21, 0.98)),
          repeating-linear-gradient(135deg, transparent 0 24px, rgba(113, 202, 237, 0.08) 25px 27px, transparent 28px 46px),
          #071121;
        color: var(--ink);
        font-family:
          Inter, ui-sans-serif, system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      }}

      main {{
        width: min(100%, 520px);
      }}

      .brand {{
        display: flex;
        align-items: center;
        justify-content: space-between;
        gap: 12px;
        margin-bottom: 14px;
        color: rgba(255, 232, 244, 0.76);
        font-size: 0.78rem;
        font-weight: 1000;
        letter-spacing: 0.08em;
        text-transform: uppercase;
      }}

      .brand strong {{
        color: var(--ink);
      }}

      .card {{
        padding: clamp(22px, 6vw, 34px);
        border: 1px solid var(--line);
        border-radius: 24px;
        background:
          linear-gradient(135deg, rgba(255, 63, 159, 0.16), rgba(99, 207, 246, 0.08)),
          var(--panel);
        box-shadow: 0 24px 70px rgba(0, 0, 0, 0.42);
      }}

      .mark {{
        display: grid;
        width: 74px;
        height: 74px;
        margin-bottom: 18px;
        place-items: center;
        border-radius: 20px;
        border: 1px solid rgba(255, 232, 244, 0.2);
        background: rgba(255, 255, 255, 0.08);
        font-size: 2rem;
        font-weight: 1000;
      }}

      .mark.is-success {{
        color: var(--green);
        box-shadow: 0 0 34px rgba(53, 230, 129, 0.16);
      }}

      .mark.is-fail {{
        color: var(--red);
        box-shadow: 0 0 34px rgba(255, 88, 104, 0.16);
      }}

      .eyebrow {{
        margin: 0 0 8px;
        color: var(--pink);
        font-size: 0.74rem;
        font-weight: 1000;
        letter-spacing: 0.1em;
        text-transform: uppercase;
      }}

      h1 {{
        margin: 0;
        color: var(--ink);
        font-size: clamp(2rem, 8vw, 3.4rem);
        line-height: 0.96;
        font-weight: 1000;
        letter-spacing: 0;
      }}

      p {{
        margin: 14px 0 0;
        color: var(--muted);
        font-size: 1rem;
        line-height: 1.48;
        font-weight: 750;
      }}

      .note {{
        padding: 12px 14px;
        border: 1px solid rgba(255, 232, 244, 0.14);
        border-radius: 14px;
        background: rgba(255, 255, 255, 0.06);
        color: rgba(255, 232, 244, 0.8);
        font-size: 0.9rem;
      }}

      .actions {{
        display: grid;
        gap: 10px;
        margin-top: 24px;
      }}

      a,
      button {{
        display: inline-flex;
        min-height: 54px;
        align-items: center;
        justify-content: center;
        border: 0;
        border-radius: 16px;
        padding: 0 18px;
        color: #190817;
        font: inherit;
        font-size: 0.96rem;
        font-weight: 1000;
        letter-spacing: 0;
        text-decoration: none;
        cursor: pointer;
      }}

      .primary {{
        background: linear-gradient(180deg, var(--pink), var(--orange));
        box-shadow: 0 12px 28px rgba(255, 63, 159, 0.24);
      }}

      .secondary {{
        border: 1px solid rgba(255, 232, 244, 0.22);
        background: rgba(255, 255, 255, 0.08);
        color: var(--ink);
      }}

      .ghost {{
        min-height: 42px;
        background: transparent;
        color: rgba(255, 232, 244, 0.72);
        font-size: 0.86rem;
      }}

      @media (min-width: 520px) {{
        .actions {{
          grid-template-columns: 1fr 1fr;
        }}

        .ghost {{
          grid-column: 1 / -1;
        }}
      }}
    </style>
  </head>
  <body>
    <main>
      <div class="brand">
        <strong>BANANA</strong>
        <span>{escape(eyebrow)}</span>
      </div>
      <section class="card" aria-labelledby="paymentTitle">
        <div class="mark {status_class}" aria-hidden="true">{"OK" if success else "!"}</div>
        <p class="eyebrow">{escape(title)}</p>
        <h1 id="paymentTitle">{escape(headline)}</h1>
        <p>{escape(body)}</p>
        <p class="note">{escape(note)}</p>
        <div class="actions">
          <a class="primary" href="{escape(telegram_url)}">{escape(primary_label)}</a>
          <a class="secondary" href="{escape(mini_app_url)}">{escape(secondary_label)}</a>
          <button class="ghost" type="button" onclick="history.length > 1 ? history.back() : window.close()">Закрыть страницу</button>
        </div>
      </section>
    </main>
  </body>
</html>"""


app = create_app()


def main() -> None:
    settings = get_settings()
    uvicorn.run(
        "app.main:app",
        host=settings.host,
        port=settings.port,
        log_level=settings.log_level.lower(),
        reload=False,
        timeout_graceful_shutdown=10,
    )


if __name__ == "__main__":
    main()
