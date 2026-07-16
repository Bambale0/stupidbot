from __future__ import annotations

from contextvars import ContextVar
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app import db as app_db
from app.models import GenerationTask, User

WELCOME_FREE_PHOTO_GENERATIONS = 2
WELCOME_FREE_PHOTO_SPEND_KEY = "welcome_free_photo_generation"
FEED_AUTHOR_REWARD_RATE_BPS = 500
FEED_AUTHOR_REWARD_PROCESSED_KEY = "feed_author_reward_processed"
FEED_AUTHOR_REWARD_AMOUNT_KEY = "feed_author_reward_kopecks"
FEED_AUTHOR_REWARD_USER_KEY = "feed_author_reward_user_id"

GROWTH_REWARDS_SCHEMA_SQL: tuple[str, ...] = (
    """
    ALTER TABLE users
    ADD COLUMN IF NOT EXISTS free_photo_generations_remaining integer
    """,
    """
    UPDATE users
    SET free_photo_generations_remaining = 0
    WHERE free_photo_generations_remaining IS NULL
    """,
    """
    ALTER TABLE users
    ALTER COLUMN free_photo_generations_remaining SET DEFAULT 2
    """,
    """
    ALTER TABLE users
    ALTER COLUMN free_photo_generations_remaining SET NOT NULL
    """,
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1
            FROM pg_constraint
            WHERE conname = 'ck_users_free_photo_generations_non_negative'
        ) THEN
            ALTER TABLE users
            ADD CONSTRAINT ck_users_free_photo_generations_non_negative
            CHECK (free_photo_generations_remaining >= 0);
        END IF;
    END
    $$
    """,
)

if not any("free_photo_generations_remaining" in statement for statement in app_db.SCHEMA_COMPAT_SQL):
    app_db.SCHEMA_COMPAT_SQL = (*app_db.SCHEMA_COMPAT_SQL, *GROWTH_REWARDS_SCHEMA_SQL)


_photo_generation_context: ContextVar[bool] = ContextVar(
    "welcome_photo_generation_context",
    default=False,
)
_free_photo_consumed: ContextVar[bool] = ContextVar(
    "welcome_photo_generation_consumed",
    default=False,
)


def free_photo_generations_remaining(user: User) -> int:
    return max(0, int(getattr(user, "free_photo_generations_remaining", 0) or 0))


def consume_free_photo_generation(user: User) -> bool:
    remaining = free_photo_generations_remaining(user)
    if remaining <= 0:
        return False
    user.free_photo_generations_remaining = remaining - 1
    return True


def _free_photo_spend_from_task(task: GenerationTask) -> int:
    payload = dict(task.input_payload or {})
    raw_spend = payload.get("credit_spend")
    if not isinstance(raw_spend, dict):
        return 0
    try:
        return max(0, int(raw_spend.get(WELCOME_FREE_PHOTO_SPEND_KEY) or 0))
    except (TypeError, ValueError):
        return 0


async def apply_feed_author_reward(
    session: AsyncSession,
    *,
    task: GenerationTask,
) -> int:
    payload = dict(task.input_payload or {})
    if payload.get(FEED_AUTHOR_REWARD_PROCESSED_KEY):
        try:
            return max(0, int(payload.get(FEED_AUTHOR_REWARD_AMOUNT_KEY) or 0))
        except (TypeError, ValueError):
            return 0
    if task.status != "success" or not task.source_feed_task_id:
        return 0

    source = await session.get(GenerationTask, int(task.source_feed_task_id))
    if not source or source.user_id == task.user_id:
        return 0

    revenue_kopecks = max(0, int(task.estimated_revenue_kopecks or 0))
    reward_kopecks = revenue_kopecks * FEED_AUTHOR_REWARD_RATE_BPS // 10_000
    if reward_kopecks <= 0:
        return 0

    author = await session.get(User, source.user_id, with_for_update=True)
    if not author or author.is_blocked:
        return 0

    from app.services.financial_credits import grant_affiliate_balance, positive_int

    author.affiliate_earned_kopecks = (
        positive_int(author.affiliate_earned_kopecks) + reward_kopecks
    )
    grant_affiliate_balance(author, reward_kopecks)
    task.input_payload = {
        **payload,
        FEED_AUTHOR_REWARD_PROCESSED_KEY: True,
        FEED_AUTHOR_REWARD_AMOUNT_KEY: reward_kopecks,
        FEED_AUTHOR_REWARD_USER_KEY: author.id,
        "feed_author_reward_rate_bps": FEED_AUTHOR_REWARD_RATE_BPS,
    }
    await session.flush()
    return reward_kopecks


def install_growth_rewards_patch() -> None:
    """Install welcome photo generations and feed-author rewards."""

    from app import repositories
    from app.plugins.generation import plugin as generation
    from app.services import financial_credits, financial_integrity, financial_tasks
    from app.services import financial_tracker_patch

    if getattr(generation, "_growth_rewards_patch_installed", False):
        return

    original_user_generates_for_free = generation.user_generates_for_free
    original_spend_user_credits = generation.spend_user_credits
    original_create_image_task = generation._create_comet_image_task
    original_generation_limits_payload = generation._generation_limits_payload
    original_charge_details_text = generation._charge_details_text
    original_refund_locked_task = financial_credits.refund_locked_task
    original_record_task_financials = financial_tasks.record_task_financials

    def user_generates_for_free(user: User) -> bool:
        if original_user_generates_for_free(user):
            return True
        return (
            _photo_generation_context.get()
            and free_photo_generations_remaining(user) > 0
        )

    def spend_user_credits(
        user: User,
        *,
        credit_type: str | None,
        amount: int,
    ) -> dict[str, int] | None:
        if (
            _photo_generation_context.get()
            and int(amount or 0) <= 0
            and not original_user_generates_for_free(user)
            and consume_free_photo_generation(user)
        ):
            _free_photo_consumed.set(True)
            return {WELCOME_FREE_PHOTO_SPEND_KEY: 1}
        return original_spend_user_credits(
            user,
            credit_type=credit_type,
            amount=amount,
        )

    async def create_image_task(*args: Any, **kwargs: Any) -> None:
        context_token = _photo_generation_context.set(True)
        consumed_token = _free_photo_consumed.set(False)
        try:
            await original_create_image_task(*args, **kwargs)
        finally:
            _free_photo_consumed.reset(consumed_token)
            _photo_generation_context.reset(context_token)

    def generation_limits_payload(user: User, model: Any) -> dict[str, Any]:
        payload = original_generation_limits_payload(user, model)
        remaining = free_photo_generations_remaining(user)
        if (
            str(getattr(model, "category", "") or "") == "image"
            and remaining > 0
            and not original_user_generates_for_free(user)
        ):
            paid_available = str(payload.get("available_generations") or "0")
            payload["available_generations"] = (
                f"{remaining} бесплатно + {paid_available} по балансу"
            )
            payload["free_photo_generations_remaining"] = remaining
        return payload

    def charge_details_text(
        user: User,
        charged_credits: int,
        has_unlimited: bool,
        credit_type: str,
    ) -> str:
        if _free_photo_consumed.get():
            remaining = free_photo_generations_remaining(user)
            return (
                "Подарочная фото-генерация: кредиты не списаны. "
                f"Осталось бесплатных: {remaining}."
            )
        return original_charge_details_text(
            user,
            charged_credits,
            has_unlimited,
            credit_type,
        )

    async def refund_locked_task(
        session: AsyncSession,
        task: GenerationTask,
    ) -> bool:
        free_spend = _free_photo_spend_from_task(task)
        if free_spend <= 0:
            return await original_refund_locked_task(session, task)
        if task.refunded_at:
            return False

        user = await session.get(User, task.user_id, with_for_update=True)
        if not user:
            return False
        user.free_photo_generations_remaining = (
            free_photo_generations_remaining(user) + free_spend
        )
        task.refunded_at = financial_credits.now_utc()
        return True

    async def refund_task_credits(
        session: AsyncSession,
        *,
        task: GenerationTask,
    ) -> None:
        if task.id:
            locked = await session.get(GenerationTask, task.id, with_for_update=True)
            if locked:
                await refund_locked_task(session, locked)

    async def record_task_financials(
        session: AsyncSession,
        *,
        task_id: int,
        settings: Any,
        provider_payload: dict[str, Any] | None = None,
    ) -> GenerationTask | None:
        task = await original_record_task_financials(
            session,
            task_id=task_id,
            settings=settings,
            provider_payload=provider_payload,
        )
        if task:
            await apply_feed_author_reward(session, task=task)
        return task

    generation.user_generates_for_free = user_generates_for_free
    generation.spend_user_credits = spend_user_credits
    generation._create_comet_image_task = create_image_task
    generation._generation_limits_payload = generation_limits_payload
    generation._charge_details_text = charge_details_text
    generation.refund_task_credits = refund_task_credits

    financial_credits.refund_locked_task = refund_locked_task
    financial_credits.refund_task_credits = refund_task_credits
    financial_tasks.refund_locked_task = refund_locked_task
    financial_tasks.record_task_financials = record_task_financials
    financial_integrity.refund_task_credits = refund_task_credits
    financial_integrity.record_task_financials = record_task_financials
    financial_tracker_patch.record_task_financials = record_task_financials
    repositories.refund_task_credits = refund_task_credits

    generation._growth_rewards_patch_installed = True
