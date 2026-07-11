from __future__ import annotations

from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import (
    BotSetting,
    CreditPackage,
    FeedLike,
    GalleryItem,
    GenerationModel,
    GenerationTask,
    PartnerLink,
    Payment,
    User,
)
from app.services.generation_catalog import ALLOWED_MODEL_CODES, DEFAULT_MODELS


DEFAULT_PACKAGES: list[dict[str, Any]] = [
    {
        "code": "starter",
        "title": "Стартовый пакет",
        "description": "10 фото-кредитов для быстрых генераций.",
        "terms": "Фото-кредиты зачисляются сразу после подтверждения оплаты.",
        "credits": 0,
        "photo_credits": 10,
        "video_credits": 0,
        "price_rub": Decimal("390.00"),
        "position": 10,
    },
    {
        "code": "creator",
        "title": "Пакет автора",
        "description": "50 фото-кредитов и 10 видео-кредитов для серии изображений и видео.",
        "terms": "Кредиты зачисляются сразу после подтверждения оплаты.",
        "credits": 0,
        "photo_credits": 50,
        "video_credits": 10,
        "price_rub": Decimal("1490.00"),
        "position": 20,
    },
    {
        "code": "unlimited_30",
        "title": "Безлимит на 30 дней",
        "description": "Отдельный безлимитный доступ на месяц.",
        "terms": "Безлимит действует 30 дней с момента подтверждения оплаты.",
        "credits": 0,
        "price_rub": Decimal("4990.00"),
        "is_unlimited": True,
        "duration_days": 30,
        "position": 30,
    },
]

DEFAULT_AFFILIATE_COMMISSION_RATE_BPS = 3000
MAX_AFFILIATE_COMMISSION_RATE_BPS = 10000
COMMON_CREDIT_TYPE = "common"
PHOTO_CREDIT_TYPE = "photo"
VIDEO_CREDIT_TYPE = "video"
CREDIT_TYPES = {COMMON_CREDIT_TYPE, PHOTO_CREDIT_TYPE, VIDEO_CREDIT_TYPE}
TECHNICAL_PACKAGE_CODE_PREFIXES = ("scenario-package-",)
TECHNICAL_PACKAGE_TITLE_PREFIXES = ("Scenario Package",)
CREDIT_BALANCE_FIELDS = {
    COMMON_CREDIT_TYPE: "credits_balance",
    PHOTO_CREDIT_TYPE: "photo_credits_balance",
    VIDEO_CREDIT_TYPE: "video_credits_balance",
}


async def ensure_defaults(session: AsyncSession, admin_ids: list[int]) -> None:
    for item in DEFAULT_MODELS:
        existing = await session.scalar(
            select(GenerationModel).where(GenerationModel.code == item["code"])
        )
        if not existing:
            session.add(GenerationModel(**item))
        else:
            _sync_default_model_config(existing, item)
            existing.title = item["title"]
            existing.category = item["category"]
            existing.description = item.get("description") or existing.description
            existing.position = int(item.get("position") or existing.position)
            existing.is_enabled = True

    extra_models = list(
        await session.scalars(
            select(GenerationModel).where(GenerationModel.code.not_in(ALLOWED_MODEL_CODES))
        )
    )
    for model in extra_models:
        model.is_enabled = False

    for item in DEFAULT_PACKAGES:
        existing = await session.scalar(
            select(CreditPackage).where(CreditPackage.code == item["code"])
        )
        if not existing:
            payload = {
                "is_unlimited": False,
                "duration_days": None,
                **item,
            }
            session.add(CreditPackage(**payload))
        else:
            if _should_sync_default_package_split(existing, item):
                existing.credits = int(item.get("credits") or 0)
                existing.photo_credits = int(item.get("photo_credits") or 0)
                existing.video_credits = int(item.get("video_credits") or 0)
                existing.description = item.get("description") or existing.description
                existing.terms = item.get("terms") or existing.terms
            elif _should_sync_default_package_text(existing):
                existing.description = item.get("description") or existing.description
                existing.terms = item.get("terms") or existing.terms
            if not existing.terms and item.get("terms"):
                existing.terms = item["terms"]

    for package in await _enabled_technical_packages(session):
        package.is_enabled = False

    for telegram_id in admin_ids:
        user = await session.scalar(select(User).where(User.telegram_id == telegram_id))
        if user:
            user.is_admin = True
            await ensure_partner_code(session, user)
        else:
            user = User(telegram_id=telegram_id, is_admin=True, credits_balance=0)
            session.add(user)
            await session.flush()
            await ensure_partner_code(session, user)

    users_without_code = list(
        await session.scalars(select(User).where(User.partner_code.is_(None)))
    )
    for user in users_without_code:
        await ensure_partner_code(session, user)

    setting = await session.get(BotSetting, "welcome_text")
    if not setting:
        session.add(
            BotSetting(
                key="welcome_text",
                value={
                    "text": (
                        "Привет. Я помогу сделать изображение по фото и промпту или оживить "
                        "персонажа по видео. Выберите раздел в меню."
                    )
                },
                description="Текст приветствия для /start.",
            )
        )


def _sync_default_model_config(model: GenerationModel, defaults: dict[str, Any]) -> None:
    default_config = defaults.get("config")
    if not isinstance(default_config, dict):
        return

    current_config = dict(model.config or {})
    for key in (
        "provider",
        "provider_family",
        "provider_model",
        "fallback_provider",
        "fallback_model",
        "price_unit",
        "motion_control_mode",
        "character_orientation",
        "background_source",
        "min_duration_seconds",
        "max_duration_seconds",
        "aspect_ratios",
        "resolutions",
        "default_aspect_ratio",
        "default_resolution",
        "output_formats",
        "modes",
        "durations",
        "max_images",
    ):
        if key in default_config:
            current_config[key] = default_config[key]
    model.config = current_config

    if model.code in ALLOWED_MODEL_CODES:
        model.description = defaults.get("description") or model.description


def _should_sync_default_package_split(package: CreditPackage, defaults: dict[str, Any]) -> bool:
    legacy_common_credits = {
        "starter": 10,
        "creator": 50,
    }.get(package.code)
    if legacy_common_credits is None:
        return False
    return (
        int(package.credits or 0) == legacy_common_credits
        and int(package.photo_credits or 0) == 0
        and int(package.video_credits or 0) == 0
        and int(defaults.get("photo_credits") or 0) > 0
    )


def _should_sync_default_package_text(package: CreditPackage) -> bool:
    legacy_descriptions = {
        "10 кредитов для быстрых генераций.",
        "50 кредитов для серии изображений и видео.",
    }
    legacy_terms = {
        "Бананы зачисляются сразу после подтверждения оплаты.",
    }
    return package.code in {"starter", "creator"} and (
        str(package.description or "").strip() in legacy_descriptions
        or str(package.terms or "").strip() in legacy_terms
    )


async def _enabled_technical_packages(session: AsyncSession) -> list[CreditPackage]:
    packages = list(
        await session.scalars(select(CreditPackage).where(CreditPackage.is_enabled.is_(True)))
    )
    return [package for package in packages if package_is_technical(package)]


def package_is_technical(package: CreditPackage) -> bool:
    code = str(package.code or "")
    title = str(package.title or "")
    return code.startswith(TECHNICAL_PACKAGE_CODE_PREFIXES) or title.startswith(
        TECHNICAL_PACKAGE_TITLE_PREFIXES
    )


def package_grants_value(package: CreditPackage) -> bool:
    credits = (
        int(package.credits or 0)
        + int(package.photo_credits or 0)
        + int(package.video_credits or 0)
    )
    duration_days = int(package.duration_days or 0)
    return credits > 0 or (bool(package.is_unlimited) and duration_days > 0)


def package_is_user_visible(package: CreditPackage) -> bool:
    return (
        bool(package.is_enabled)
        and package_grants_value(package)
        and not package_is_technical(package)
    )


async def get_or_create_user(session: AsyncSession, tg_user: Any, admin_ids: list[int]) -> User:
    user = await session.scalar(select(User).where(User.telegram_id == int(tg_user.id)))
    if not user:
        user = User(
            telegram_id=int(tg_user.id),
            username=tg_user.username,
            first_name=tg_user.first_name,
            last_name=tg_user.last_name,
            language_code=tg_user.language_code,
            is_admin=int(tg_user.id) in admin_ids,
        )
        session.add(user)
        await session.flush()
        await ensure_partner_code(session, user)
    else:
        user.username = tg_user.username
        user.first_name = tg_user.first_name
        user.last_name = tg_user.last_name
        user.language_code = tg_user.language_code
        if int(tg_user.id) in admin_ids:
            user.is_admin = True
        await ensure_partner_code(session, user)
    return user


def _base36(value: int) -> str:
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value <= 0:
        return "0"
    result = ""
    while value:
        value, rest = divmod(value, 36)
        result = alphabet[rest] + result
    return result


async def ensure_partner_code(session: AsyncSession, user: User) -> str:
    if user.partner_code:
        return user.partner_code
    if not user.id:
        await session.flush()
    user.partner_code = f"u{_base36(user.id)}"
    return user.partner_code


def normalize_ref_code(value: str | None) -> str:
    normalized = str(value or "").strip()
    for prefix in ("ref_", "ref-"):
        if normalized.lower().startswith(prefix):
            normalized = normalized[len(prefix) :]
            break
    return normalized.strip().lower()


async def bind_referral(session: AsyncSession, *, user: User, ref_code: str | None) -> User | None:
    if not ref_code or user.referred_by_user_id:
        return None
    normalized = normalize_ref_code(ref_code)
    if not normalized:
        return None
    if not user.id:
        await session.flush()
    referrer = await session.scalar(select(User).where(func.lower(User.partner_code) == normalized))
    if not referrer or referrer.id == user.id or referrer.is_blocked:
        return None
    if await _referral_would_create_cycle(session, user_id=user.id, referrer_id=referrer.id):
        return None
    user.referred_by_user_id = referrer.id
    return referrer


async def _referral_would_create_cycle(
    session: AsyncSession,
    *,
    user_id: int,
    referrer_id: int,
) -> bool:
    current_id: int | None = referrer_id
    visited: set[int] = set()
    while current_id:
        if current_id == user_id or current_id in visited:
            return True
        visited.add(current_id)
        current = await session.get(User, current_id)
        current_id = current.referred_by_user_id if current else None
    return False


async def apply_affiliate_commission(
    session: AsyncSession,
    *,
    payment: Payment,
    buyer: User,
) -> int:
    if (
        payment.affiliate_commission_user_id is not None
        or payment.affiliate_commission_kopecks > 0
        or not buyer.referred_by_user_id
    ):
        return 0
    referrer = await session.get(User, buyer.referred_by_user_id, with_for_update=True)
    if not referrer or referrer.is_blocked:
        return 0
    rate_bps = (
        DEFAULT_AFFILIATE_COMMISSION_RATE_BPS
        if referrer.affiliate_commission_rate_bps is None
        else int(referrer.affiliate_commission_rate_bps)
    )
    rate_bps = max(0, min(MAX_AFFILIATE_COMMISSION_RATE_BPS, rate_bps))
    payment.affiliate_commission_user_id = referrer.id
    commission = max(0, payment.amount_kopecks * rate_bps // 10000)
    if commission <= 0:
        return 0
    referrer.affiliate_balance_kopecks += commission
    referrer.affiliate_earned_kopecks += commission
    payment.affiliate_commission_kopecks = commission
    return commission


def user_has_unlimited(user: User) -> bool:
    if not user.unlimited_until:
        return False
    return user.unlimited_until > datetime.now(timezone.utc)


def user_generates_for_free(user: User) -> bool:
    return bool(user.is_admin) or user_has_unlimited(user)


def normalize_credit_type(value: str | None) -> str:
    credit_type = str(value or COMMON_CREDIT_TYPE).strip().lower()
    if credit_type in {"image", "photo", "photos"}:
        return PHOTO_CREDIT_TYPE
    if credit_type in {"video", "motion", "videos"}:
        return VIDEO_CREDIT_TYPE
    if credit_type in {"common", "credit", "credits", "universal"}:
        return COMMON_CREDIT_TYPE
    return COMMON_CREDIT_TYPE


def model_credit_type(model: GenerationModel) -> str:
    if model.category == "video":
        return VIDEO_CREDIT_TYPE
    if model.category == "image":
        return PHOTO_CREDIT_TYPE
    return COMMON_CREDIT_TYPE


def user_credit_balance(user: User, credit_type: str | None = None) -> int:
    normalized = normalize_credit_type(credit_type)
    common = int(user.credits_balance or 0)
    if normalized == PHOTO_CREDIT_TYPE:
        return int(user.photo_credits_balance or 0) + common
    if normalized == VIDEO_CREDIT_TYPE:
        return int(user.video_credits_balance or 0) + common
    return common


def spend_user_credits(
    user: User,
    *,
    credit_type: str | None,
    amount: int,
) -> dict[str, int] | None:
    amount = int(amount or 0)
    if amount <= 0:
        return {}
    normalized = normalize_credit_type(credit_type)
    if user_credit_balance(user, normalized) < amount:
        return None

    spent: dict[str, int] = {COMMON_CREDIT_TYPE: 0, PHOTO_CREDIT_TYPE: 0, VIDEO_CREDIT_TYPE: 0}
    remaining = amount
    if normalized in {PHOTO_CREDIT_TYPE, VIDEO_CREDIT_TYPE}:
        field = CREDIT_BALANCE_FIELDS[normalized]
        specific_balance = int(getattr(user, field) or 0)
        specific_spend = min(specific_balance, remaining)
        if specific_spend:
            setattr(user, field, specific_balance - specific_spend)
            spent[normalized] = specific_spend
            remaining -= specific_spend

    if remaining:
        common_balance = int(user.credits_balance or 0)
        user.credits_balance = common_balance - remaining
        spent[COMMON_CREDIT_TYPE] = remaining

    return {key: value for key, value in spent.items() if value > 0}


def credit_spend_from_payload(payload: dict[str, Any] | None) -> dict[str, int]:
    if not isinstance(payload, dict):
        return {}
    raw_spend = payload.get("credit_spend")
    if not isinstance(raw_spend, dict):
        return {}
    result: dict[str, int] = {}
    for key, value in raw_spend.items():
        credit_type = normalize_credit_type(str(key))
        try:
            amount = int(value)
        except (TypeError, ValueError):
            continue
        if amount > 0:
            result[credit_type] = result.get(credit_type, 0) + amount
    return result


async def list_models(session: AsyncSession, category: str | None = None) -> list[GenerationModel]:
    stmt = select(GenerationModel).where(GenerationModel.code.in_(ALLOWED_MODEL_CODES))
    stmt = stmt.order_by(GenerationModel.position, GenerationModel.id)
    if category:
        stmt = stmt.where(GenerationModel.category == category)
    return list(await session.scalars(stmt))


async def list_enabled_models(session: AsyncSession, category: str) -> list[GenerationModel]:
    stmt = (
        select(GenerationModel)
        .where(
            GenerationModel.category == category,
            GenerationModel.is_enabled.is_(True),
            GenerationModel.code.in_(ALLOWED_MODEL_CODES),
        )
        .order_by(GenerationModel.position, GenerationModel.id)
    )
    return list(await session.scalars(stmt))


async def get_model(session: AsyncSession, code: str) -> GenerationModel | None:
    if code not in ALLOWED_MODEL_CODES:
        return None
    return await session.scalar(select(GenerationModel).where(GenerationModel.code == code))


async def charge_user_for_model(
    session: AsyncSession,
    *,
    user: User,
    model: GenerationModel,
) -> bool:
    if model.price_credits <= 0 or user_generates_for_free(user):
        return True
    return (
        spend_user_credits(
            user,
            credit_type=model_credit_type(model),
            amount=int(model.price_credits or 0),
        )
        is not None
    )


async def refund_credits(
    session: AsyncSession,
    *,
    user_id: int,
    credits: int,
    credit_type: str | None = None,
    allocation: dict[str, int] | None = None,
) -> None:
    if credits <= 0:
        return
    user = await session.get(User, user_id, with_for_update=True)
    if not user:
        return
    if allocation:
        refunded = 0
        for raw_type, amount in allocation.items():
            try:
                value = int(amount)
            except (TypeError, ValueError):
                continue
            if value <= 0:
                continue
            normalized = normalize_credit_type(raw_type)
            field = CREDIT_BALANCE_FIELDS[normalized]
            setattr(user, field, int(getattr(user, field) or 0) + value)
            refunded += value
        remaining = int(credits or 0) - refunded
        if remaining <= 0:
            return
        credits = remaining

    normalized = normalize_credit_type(credit_type)
    field = CREDIT_BALANCE_FIELDS[normalized]
    setattr(user, field, int(getattr(user, field) or 0) + int(credits or 0))


async def refund_task_credits(session: AsyncSession, *, task: GenerationTask) -> None:
    payload = dict(task.input_payload or {})
    await refund_credits(
        session,
        user_id=task.user_id,
        credits=int(task.cost_credits or 0),
        credit_type=str(payload.get("credit_type") or COMMON_CREDIT_TYPE),
        allocation=credit_spend_from_payload(payload),
    )


async def list_packages(session: AsyncSession, only_enabled: bool = True) -> list[CreditPackage]:
    stmt = select(CreditPackage).order_by(CreditPackage.position, CreditPackage.id)
    if only_enabled:
        stmt = stmt.where(CreditPackage.is_enabled.is_(True))
    packages = list(await session.scalars(stmt))
    if only_enabled:
        return [package for package in packages if package_is_user_visible(package)]
    return packages


def credit_package_snapshot(package: CreditPackage) -> dict[str, Any]:
    duration_days = package.duration_days
    return {
        "package_id": package.id,
        "code": package.code,
        "title": package.title,
        "description": package.description or "",
        "terms": package.terms or "",
        "credits": int(package.credits or 0),
        "photo_credits": int(package.photo_credits or 0),
        "video_credits": int(package.video_credits or 0),
        "price_rub": str(package.price_rub),
        "is_unlimited": bool(package.is_unlimited),
        "duration_days": int(duration_days) if duration_days is not None else None,
    }


def payment_package_snapshot(payment: Payment) -> dict[str, Any] | None:
    raw_payload = payment.raw_payload if isinstance(payment.raw_payload, dict) else {}
    snapshot = raw_payload.get("package_snapshot")
    if not isinstance(snapshot, dict):
        return None
    return snapshot


async def apply_package_snapshot_to_user(
    session: AsyncSession,
    *,
    user: User,
    snapshot: dict[str, Any],
) -> None:
    if bool(snapshot.get("is_unlimited")):
        duration_days = _snapshot_int(snapshot.get("duration_days"))
        if duration_days > 0:
            now = datetime.now(timezone.utc)
            base = (
                user.unlimited_until if user.unlimited_until and user.unlimited_until > now else now
            )
            user.unlimited_until = base + timedelta(days=duration_days)
    user.credits_balance += _snapshot_int(snapshot.get("credits"))
    user.photo_credits_balance += _snapshot_int(snapshot.get("photo_credits"))
    user.video_credits_balance += _snapshot_int(snapshot.get("video_credits"))
    await session.flush()


def _snapshot_int(value: Any) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


async def apply_package_to_user(
    session: AsyncSession,
    *,
    user: User,
    package: CreditPackage,
) -> None:
    await apply_package_snapshot_to_user(
        session, user=user, snapshot=credit_package_snapshot(package)
    )


async def get_public_gallery(session: AsyncSession, limit: int = 10) -> list[GalleryItem]:
    stmt = (
        select(GalleryItem)
        .where(GalleryItem.is_public.is_(True))
        .order_by(GalleryItem.is_featured.desc(), GalleryItem.created_at.desc())
        .limit(limit)
    )
    return list(await session.scalars(stmt))


def generation_media_type(task: GenerationTask) -> str:
    provider = str((task.input_payload or {}).get("provider") or "")
    if task.model_code.startswith("kling") or "video" in task.model_code or provider == "kie-video":
        return "video"
    return "image"


def public_user_name(user: User | None) -> str:
    if not user:
        return "BANANA user"
    if user.username:
        return f"@{user.username}"
    name = " ".join(part for part in [user.first_name, user.last_name] if part)
    return name or "BANANA user"


async def share_task_to_feed(
    session: AsyncSession,
    *,
    task_id: int,
    user_id: int,
) -> tuple[bool, str]:
    task = await session.get(GenerationTask, task_id)
    if not task:
        return False, "not_found"
    if task.user_id != user_id:
        return False, "not_owner"
    if task.status != "success":
        return False, "not_completed"
    if not task.result_urls:
        return False, "no_result"
    if task.source_feed_task_id:
        source = await session.get(GenerationTask, task.source_feed_task_id)
        if source and source.user_id != user_id:
            return False, "foreign_source"
    task.is_public_feed = True
    task.feed_status = "approved"
    task.published_at = datetime.now(timezone.utc)
    await session.flush()
    return True, "published"


async def remove_task_from_feed(
    session: AsyncSession,
    *,
    task_id: int,
    user_id: int,
) -> bool:
    task = await session.get(GenerationTask, task_id)
    if not task or task.user_id != user_id:
        return False
    task.is_public_feed = False
    task.feed_status = "hidden"
    await session.flush()
    return True


async def get_feed_tasks(session: AsyncSession, limit: int = 30) -> list[GenerationTask]:
    score = GenerationTask.likes_count + GenerationTask.shares_count * 3
    stmt = (
        select(GenerationTask)
        .where(
            GenerationTask.is_public_feed.is_(True),
            GenerationTask.feed_status == "approved",
            GenerationTask.status == "success",
            func.jsonb_array_length(GenerationTask.result_urls) > 0,
        )
        .order_by(
            score.desc(),
            GenerationTask.published_at.desc().nullslast(),
            GenerationTask.created_at.desc(),
        )
        .limit(max(1, min(int(limit), 100)))
    )
    return list(await session.scalars(stmt))


async def get_user_generation_tasks(
    session: AsyncSession,
    *,
    user_id: int,
    limit: int = 30,
) -> list[GenerationTask]:
    stmt = (
        select(GenerationTask)
        .where(GenerationTask.user_id == user_id)
        .order_by(GenerationTask.created_at.desc())
        .limit(max(1, min(int(limit), 100)))
    )
    return list(await session.scalars(stmt))


async def get_public_feed_task(session: AsyncSession, task_id: int) -> GenerationTask | None:
    return await session.scalar(
        select(GenerationTask).where(
            GenerationTask.id == task_id,
            GenerationTask.is_public_feed.is_(True),
            GenerationTask.feed_status == "approved",
            GenerationTask.status == "success",
        )
    )


async def like_feed_task(
    session: AsyncSession,
    *,
    task_id: int,
    user_id: int,
) -> tuple[int | None, bool]:
    task = await get_public_feed_task(session, task_id)
    if not task:
        return None, False
    existing = await session.scalar(
        select(FeedLike).where(FeedLike.user_id == user_id, FeedLike.task_id == task_id)
    )
    if existing:
        return task.likes_count, False
    session.add(FeedLike(user_id=user_id, task_id=task_id))
    task.likes_count += 1
    await session.flush()
    return task.likes_count, True


async def increment_feed_share(session: AsyncSession, task_id: int) -> int | None:
    task = await get_public_feed_task(session, task_id)
    if not task:
        return None
    task.shares_count += 1
    await session.flush()
    return task.shares_count


async def serialize_feed_task(session: AsyncSession, task: GenerationTask) -> dict[str, Any]:
    user = await session.get(User, task.user_id)
    media_url = str(task.result_urls[0]) if task.result_urls else ""
    payload = task.input_payload or {}
    return {
        "id": task.id,
        "media_url": media_url,
        "media_type": generation_media_type(task),
        "prompt": task.prompt or "",
        "model_code": task.model_code,
        "author": public_user_name(user),
        "likes": int(task.likes_count or 0),
        "shares": int(task.shares_count or 0),
        "published_at": task.published_at.isoformat() if task.published_at else None,
        "aspect_ratio": payload.get("aspect_ratio"),
        "duration": payload.get("duration"),
    }


def serialize_user_generation_task(task: GenerationTask) -> dict[str, Any]:
    media_url = str(task.result_urls[0]) if task.result_urls else ""
    payload = task.input_payload or {}
    return {
        "id": task.id,
        "media_url": media_url,
        "media_type": generation_media_type(task),
        "status": task.status,
        "prompt": task.prompt or "",
        "model_code": task.model_code,
        "cost_credits": int(task.cost_credits or 0),
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "aspect_ratio": payload.get("aspect_ratio"),
        "duration": payload.get("duration"),
        "is_public_feed": bool(task.is_public_feed),
    }


async def get_enabled_partner_links(session: AsyncSession) -> list[PartnerLink]:
    stmt = (
        select(PartnerLink)
        .where(PartnerLink.is_enabled.is_(True))
        .order_by(PartnerLink.position, PartnerLink.id)
    )
    return list(await session.scalars(stmt))


async def stats_snapshot(session: AsyncSession) -> dict[str, int]:
    users = await session.scalar(select(func.count()).select_from(User))
    tasks = await session.scalar(select(func.count()).select_from(GenerationTask))
    tasks_success = await session.scalar(
        select(func.count()).select_from(GenerationTask).where(GenerationTask.status == "success")
    )
    payments_paid = await session.scalar(
        select(func.count()).select_from(Payment).where(Payment.status == "paid")
    )
    return {
        "users": int(users or 0),
        "tasks": int(tasks or 0),
        "tasks_success": int(tasks_success or 0),
        "payments_paid": int(payments_paid or 0),
    }
