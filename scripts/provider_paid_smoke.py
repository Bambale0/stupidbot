from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from types import SimpleNamespace
from typing import Any
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import async_sessionmaker

if __package__ in {None, ""}:
    from _bootstrap import add_project_root_to_path

    add_project_root_to_path()

from app.config import get_settings
from app.db import build_engine, session_scope
from app.models import (
    CreditLedgerEntry,
    GalleryItem,
    GenerationModel,
    GenerationTask,
    ProviderCostEntry,
    User,
)
from app.services.comet import CometClient
from app.services.financial_tasks import finalize_generation_task, record_task_financials
from app.services.kie import KieClient
from app.services.referrals import install_repository_patches

CONFIRMATION = "RUN_PAID_SMOKE"
ACTIVE_STATES = {"submitted", "waiting", "queuing", "generating"}


def _result_urls(data: dict[str, Any]) -> list[str]:
    result_json: Any = data.get("resultJson") or data.get("result")
    if isinstance(result_json, str):
        try:
            result_json = json.loads(result_json)
        except json.JSONDecodeError:
            result_json = None
    sources = [result_json, data]
    for source in sources:
        if not isinstance(source, dict):
            continue
        for key in ("resultUrls", "urls", "images", "videos"):
            value = source.get(key)
            if isinstance(value, str) and value:
                return [value]
            if isinstance(value, list):
                urls: list[str] = []
                for item in value:
                    if isinstance(item, str) and item:
                        urls.append(item)
                    elif isinstance(item, dict):
                        for nested in (
                            "url",
                            "image_url",
                            "video_url",
                            "download_url",
                            "result_url",
                            "output_url",
                        ):
                            nested_value = item.get(nested)
                            if isinstance(nested_value, str) and nested_value:
                                urls.append(nested_value)
                                break
                if urls:
                    return urls
        for key in (
            "url",
            "image_url",
            "video_url",
            "download_url",
            "result_url",
            "output_url",
        ):
            value = source.get(key)
            if isinstance(value, str) and value:
                return [value]
    return []


async def _run_comet(settings: Any, prompt: str) -> tuple[str, dict[str, Any], list[str]]:
    if not settings.comet_api_key:
        raise RuntimeError("COMET_API_KEY is not configured")
    client = CometClient(
        api_key=settings.comet_api_key,
        base_url=settings.comet_base_url,
        timeout=240.0,
    )
    model = settings.comet_image_simple_model
    result = await client.generate_image(
        model=model,
        prompt=prompt,
        aspect_ratio="1:1",
        image_size="1K",
        output_mime_type="image/png",
    )
    if not result.images or not result.images[0].content:
        raise RuntimeError("Comet returned no image bytes")
    digest = hashlib.sha256(result.images[0].content).hexdigest()
    payload = {
        **dict(result.metadata or {}),
        "smoke_sha256": digest,
        "smoke_bytes": len(result.images[0].content),
        "smoke_mime_type": result.images[0].mime_type,
    }
    # Comet image generation is synchronous and returns inline bytes. The marker
    # is used only inside a rolled-back transaction to verify gallery finalization.
    return model, payload, [f"inline://comet/{digest}"]


async def _run_kie(
    settings: Any,
    prompt: str,
    *,
    timeout_seconds: int,
) -> tuple[str, dict[str, Any], list[str]]:
    if not settings.kie_api_key:
        raise RuntimeError("KIE_API_KEY is not configured")
    client = KieClient(
        api_key=settings.kie_api_key,
        base_url=settings.kie_base_url,
        upload_base_url=settings.kie_upload_base_url,
        timeout=90.0,
    )
    model = settings.kie_image_simple_model
    task_id = await client.create_image_task(
        model=model,
        prompt=prompt,
        aspect_ratio="1:1",
        output_format="png",
    )
    deadline = asyncio.get_running_loop().time() + max(60, timeout_seconds)
    last: dict[str, Any] = {"state": "submitted", "taskId": task_id}
    while asyncio.get_running_loop().time() < deadline:
        last = await client.query_task(task_id)
        state = str(last.get("state") or "").lower()
        if state == "success":
            urls = _result_urls(last)
            if not urls:
                raise RuntimeError(f"KIE task {task_id} succeeded without result URL")
            return model, {**last, "taskId": task_id}, urls
        if state == "fail":
            reason = last.get("failMsg") or last.get("error") or "unknown provider error"
            raise RuntimeError(f"KIE task {task_id} failed: {reason}")
        if state not in ACTIVE_STATES:
            raise RuntimeError(f"KIE task {task_id} returned unexpected state: {state}")
        await asyncio.sleep(10)
    raise TimeoutError(f"KIE task {task_id} did not finish in {timeout_seconds}s; last state={last.get('state')}")


async def _verify_financial_lifecycle(
    settings: Any,
    *,
    provider: str,
    provider_model: str,
    prompt: str,
    result_payload: dict[str, Any],
    result_urls: list[str],
) -> None:
    install_repository_patches()
    engine = build_engine(settings)
    if engine.dialect.name != "postgresql":
        await engine.dispose()
        raise RuntimeError("paid smoke lifecycle requires PostgreSQL")
    suffix = uuid4().hex
    try:
        async with engine.connect() as connection:
            outer = await connection.begin()
            factory = async_sessionmaker(
                bind=connection,
                expire_on_commit=False,
                autoflush=False,
                join_transaction_mode="create_savepoint",
            )
            try:
                async with session_scope(factory) as session:
                    user = User(telegram_id=8_600_000_000 + int(suffix[:7], 16))
                    model_code = f"paid-smoke-{provider}-{suffix[:12]}"
                    model = GenerationModel(
                        code=model_code,
                        title=f"Paid smoke {provider}",
                        category="image",
                        price_credits=1,
                        config={"provider_cost_kopecks": 0},
                    )
                    session.add_all([user, model])
                    await session.flush()
                    user.photo_credits_balance = 1
                    await session.flush()
                    user.photo_credits_balance = 0
                    task = GenerationTask(
                        user_id=user.id,
                        model_code=model_code,
                        status="submitted",
                        prompt=prompt,
                        input_payload={
                            "provider": provider,
                            "provider_model": provider_model,
                            "credit_type": "photo",
                            "credit_spend": {"photo": 1},
                            "paid_smoke": True,
                        },
                        cost_credits=1,
                        idempotency_key=f"paid-smoke:{provider}:{suffix}",
                    )
                    session.add(task)
                    await session.flush()
                    task_id = task.id
                    user_id = user.id

                async with session_scope(factory) as session:
                    first, changed_first = await finalize_generation_task(
                        session,
                        task_id=task_id,
                        status="success",
                        result_payload=result_payload,
                        result_urls=result_urls,
                    )
                    second, changed_second = await finalize_generation_task(
                        session,
                        task_id=task_id,
                        status="success",
                        result_payload=result_payload,
                        result_urls=result_urls,
                    )
                    assert first and second
                    assert changed_first is True
                    assert changed_second is False
                    await record_task_financials(
                        session,
                        task_id=task_id,
                        settings=settings,
                        provider_payload=result_payload,
                    )
                    await record_task_financials(
                        session,
                        task_id=task_id,
                        settings=settings,
                        provider_payload=result_payload,
                    )

                async with session_scope(factory) as session:
                    gallery_count = int(
                        await session.scalar(
                            select(func.count()).select_from(GalleryItem).where(
                                GalleryItem.generation_task_id == task_id
                            )
                        )
                        or 0
                    )
                    cost_count = int(
                        await session.scalar(
                            select(func.count()).select_from(ProviderCostEntry).where(
                                ProviderCostEntry.generation_task_id == task_id
                            )
                        )
                        or 0
                    )
                    spend_count = int(
                        await session.scalar(
                            select(func.count()).select_from(CreditLedgerEntry).where(
                                CreditLedgerEntry.user_id == user_id,
                                CreditLedgerEntry.credit_type == "photo",
                                CreditLedgerEntry.balance_delta == -1,
                            )
                        )
                        or 0
                    )
                    task = await session.get(GenerationTask, task_id)
                    assert task and task.status == "success" and task.finalized_at
                    assert gallery_count == 1
                    assert cost_count == 1
                    assert spend_count == 1
            finally:
                await outer.rollback()
    finally:
        await engine.dispose()


async def amain(args: argparse.Namespace) -> None:
    if args.confirm != CONFIRMATION:
        raise RuntimeError(f"paid smoke requires --confirm {CONFIRMATION}")
    settings = get_settings()
    prompt = "A plain blue ceramic mug on a white studio background, no text, product photo."
    providers = [args.provider] if args.provider != "both" else ["comet", "kie"]
    for provider in providers:
        print(f"Starting paid smoke for {provider}")
        if provider == "comet":
            model, payload, urls = await _run_comet(settings, prompt)
        else:
            model, payload, urls = await _run_kie(
                settings,
                prompt,
                timeout_seconds=args.timeout_seconds,
            )
        await _verify_financial_lifecycle(
            settings,
            provider=provider,
            provider_model=model,
            prompt=prompt,
            result_payload=payload,
            result_urls=urls,
        )
        print(
            f"Paid smoke passed: provider={provider} model={model} "
            f"results={len(urls)} transactional_lifecycle=passed"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Run explicitly approved paid provider smoke checks")
    parser.add_argument("--provider", choices=("comet", "kie", "both"), required=True)
    parser.add_argument("--confirm", required=True)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    asyncio.run(amain(parser.parse_args()))
