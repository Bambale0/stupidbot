from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import GenerationModel, GenerationTask, User
from app.repositories import refund_task_credits
from app.services.financial_integrity import record_task_financials
from app.services.growth_rewards import (
    FEED_AUTHOR_REWARD_AMOUNT_KEY,
    FEED_AUTHOR_REWARD_PROCESSED_KEY,
    FEED_AUTHOR_REWARD_RATE_BPS,
    WELCOME_FREE_PHOTO_GENERATIONS,
    WELCOME_FREE_PHOTO_SPEND_KEY,
    consume_free_photo_generation,
    free_photo_generations_remaining,
)
from app.plugins.generation import plugin as generation


async def run_growth_rewards_regression(session: AsyncSession, suffix: str) -> None:
    newcomer = User(telegram_id=int(f"91{suffix}", 16))
    session.add(newcomer)
    await session.flush()

    assert WELCOME_FREE_PHOTO_GENERATIONS == 2
    assert free_photo_generations_remaining(newcomer) == 2
    assert not generation.user_generates_for_free(newcomer), (
        "free photo allowance must not make video generations free"
    )

    assert consume_free_photo_generation(newcomer)
    assert free_photo_generations_remaining(newcomer) == 1

    free_task = GenerationTask(
        user_id=newcomer.id,
        model_code=f"welcome-photo-{suffix}",
        status="generating",
        input_payload={
            "credit_type": "photo",
            "credit_spend": {WELCOME_FREE_PHOTO_SPEND_KEY: 1},
        },
        cost_credits=0,
    )
    session.add(free_task)
    await session.flush()

    await refund_task_credits(session, task=free_task)
    await refund_task_credits(session, task=free_task)
    assert free_photo_generations_remaining(newcomer) == 2
    assert free_task.refunded_at is not None

    author = User(
        telegram_id=int(f"92{suffix}", 16),
        free_photo_generations_remaining=0,
    )
    repeater = User(
        telegram_id=int(f"93{suffix}", 16),
        free_photo_generations_remaining=0,
    )
    session.add_all([author, repeater])
    await session.flush()

    model = GenerationModel(
        code=f"growth-photo-{suffix}",
        title="Growth photo",
        category="image",
        price_credits=2,
        config={"provider_cost_kopecks": 100},
    )
    session.add(model)
    await session.flush()

    source = GenerationTask(
        user_id=author.id,
        model_code=model.code,
        status="success",
        result_urls=["source-file-id"],
        input_payload={"provider": "comet"},
        cost_credits=2,
        is_public_feed=True,
        feed_status="approved",
    )
    session.add(source)
    await session.flush()

    paid_repeat = GenerationTask(
        user_id=repeater.id,
        model_code=model.code,
        status="success",
        result_urls=["repeat-file-id"],
        input_payload={
            "provider": "comet",
            "credit_type": "photo",
            "credit_spend": {"photo": 2},
        },
        cost_credits=2,
        source_feed_task_id=source.id,
    )
    session.add(paid_repeat)
    await session.flush()

    settings = SimpleNamespace(
        photo_credit_value_kopecks=3900,
        video_credit_value_kopecks=10000,
    )
    await record_task_financials(
        session,
        task_id=paid_repeat.id,
        settings=settings,
    )
    expected_reward = 2 * 3900 * FEED_AUTHOR_REWARD_RATE_BPS // 10_000
    assert expected_reward == 390
    assert author.affiliate_balance_kopecks == expected_reward
    assert author.affiliate_earned_kopecks == expected_reward
    assert paid_repeat.input_payload[FEED_AUTHOR_REWARD_PROCESSED_KEY] is True
    assert paid_repeat.input_payload[FEED_AUTHOR_REWARD_AMOUNT_KEY] == expected_reward

    await record_task_financials(
        session,
        task_id=paid_repeat.id,
        settings=settings,
    )
    assert author.affiliate_balance_kopecks == expected_reward
    assert author.affiliate_earned_kopecks == expected_reward

    free_repeat = GenerationTask(
        user_id=repeater.id,
        model_code=model.code,
        status="success",
        result_urls=["free-repeat-file-id"],
        input_payload={
            "provider": "comet",
            "credit_type": "photo",
            "credit_spend": {WELCOME_FREE_PHOTO_SPEND_KEY: 1},
        },
        cost_credits=0,
        source_feed_task_id=source.id,
    )
    session.add(free_repeat)
    await session.flush()
    await record_task_financials(
        session,
        task_id=free_repeat.id,
        settings=settings,
    )
    assert author.affiliate_balance_kopecks == expected_reward
    assert FEED_AUTHOR_REWARD_PROCESSED_KEY not in free_repeat.input_payload

    self_repeat = GenerationTask(
        user_id=author.id,
        model_code=model.code,
        status="success",
        result_urls=["self-repeat-file-id"],
        input_payload={
            "provider": "comet",
            "credit_type": "photo",
            "credit_spend": {"photo": 2},
        },
        cost_credits=2,
        source_feed_task_id=source.id,
    )
    session.add(self_repeat)
    await session.flush()
    await record_task_financials(
        session,
        task_id=self_repeat.id,
        settings=settings,
    )
    assert author.affiliate_balance_kopecks == expected_reward
