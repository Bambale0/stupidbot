from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base


JsonDict = dict[str, Any]
JsonList = list[Any]


def _generation_idempotency_key(context: Any) -> str:
    params = context.get_current_parameters()
    chat_id = params.get("chat_id")
    message_id = params.get("message_id")
    model_code = str(params.get("model_code") or "model")
    if chat_id is not None and message_id is not None:
        return f"telegram:{chat_id}:{message_id}:{model_code}"
    return f"generation:{uuid4().hex}"


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


class User(TimestampMixin, Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint(
            "credits_balance >= 0 AND photo_credits_balance >= 0 "
            "AND video_credits_balance >= 0",
            name="ck_users_credit_balances_non_negative",
        ),
        CheckConstraint(
            "common_credit_debt >= 0 AND photo_credit_debt >= 0 "
            "AND video_credit_debt >= 0",
            name="ck_users_credit_debts_non_negative",
        ),
        CheckConstraint(
            "affiliate_balance_kopecks >= 0 AND affiliate_earned_kopecks >= 0 "
            "AND affiliate_debt_kopecks >= 0",
            name="ck_users_affiliate_amounts_non_negative",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(255))
    first_name: Mapped[str | None] = mapped_column(String(255))
    last_name: Mapped[str | None] = mapped_column(String(255))
    language_code: Mapped[str | None] = mapped_column(String(16))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    is_blocked: Mapped[bool] = mapped_column(Boolean, default=False)
    credits_balance: Mapped[int] = mapped_column(Integer, default=0)
    photo_credits_balance: Mapped[int] = mapped_column(Integer, default=0)
    video_credits_balance: Mapped[int] = mapped_column(Integer, default=0)
    common_credit_debt: Mapped[int] = mapped_column(Integer, default=0)
    photo_credit_debt: Mapped[int] = mapped_column(Integer, default=0)
    video_credit_debt: Mapped[int] = mapped_column(Integer, default=0)
    affiliate_balance_kopecks: Mapped[int] = mapped_column(Integer, default=0)
    affiliate_earned_kopecks: Mapped[int] = mapped_column(Integer, default=0)
    affiliate_debt_kopecks: Mapped[int] = mapped_column(Integer, default=0)
    affiliate_commission_rate_bps: Mapped[int] = mapped_column(Integer, default=3000)
    unlimited_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    partner_code: Mapped[str | None] = mapped_column(String(64), unique=True)
    referred_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))

    tasks: Mapped[list["GenerationTask"]] = relationship(back_populates="user")


class ReferralCodeAlias(TimestampMixin, Base):
    __tablename__ = "referral_code_aliases"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    code: Mapped[str] = mapped_column(String(64), unique=True, index=True)


class BotSetting(TimestampMixin, Base):
    __tablename__ = "bot_settings"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[JsonDict] = mapped_column(JSONB, default=dict)
    description: Mapped[str | None] = mapped_column(Text)


class GenerationModel(TimestampMixin, Base):
    __tablename__ = "generation_models"
    __table_args__ = (
        CheckConstraint("price_credits >= 0", name="ck_generation_models_price_non_negative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255))
    category: Mapped[str] = mapped_column(String(32), index=True)
    description: Mapped[str | None] = mapped_column(Text)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    position: Mapped[int] = mapped_column(Integer, default=100)
    price_credits: Mapped[int] = mapped_column(Integer, default=1)
    config: Mapped[JsonDict] = mapped_column(JSONB, default=dict)


class UploadedFile(TimestampMixin, Base):
    __tablename__ = "uploaded_files"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    file_type: Mapped[str] = mapped_column(String(32), index=True)
    telegram_file_id: Mapped[str | None] = mapped_column(String(255))
    original_name: Mapped[str | None] = mapped_column(String(255))
    mime_type: Mapped[str | None] = mapped_column(String(255))
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    kie_file_url: Mapped[str] = mapped_column(Text)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class GenerationTask(TimestampMixin, Base):
    __tablename__ = "generation_tasks"
    __table_args__ = (
        Index("ix_generation_tasks_public_feed", "is_public_feed", "published_at"),
        CheckConstraint("cost_credits >= 0", name="ck_generation_tasks_cost_non_negative"),
        CheckConstraint(
            "provider_cost_kopecks >= 0",
            name="ck_generation_tasks_provider_cost_non_negative",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    model_code: Mapped[str] = mapped_column(String(128), index=True)
    provider_task_id: Mapped[str | None] = mapped_column(String(255), index=True)
    idempotency_key: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        index=True,
        default=_generation_idempotency_key,
    )
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    prompt: Mapped[str | None] = mapped_column(Text)
    input_payload: Mapped[JsonDict] = mapped_column(JSONB, default=dict)
    result_payload: Mapped[JsonDict] = mapped_column(JSONB, default=dict)
    result_urls: Mapped[JsonList] = mapped_column(JSONB, default=list)
    error_message: Mapped[str | None] = mapped_column(Text)
    cost_credits: Mapped[int] = mapped_column(Integer, default=0)
    provider_cost_kopecks: Mapped[int] = mapped_column(Integer, default=0)
    estimated_revenue_kopecks: Mapped[int] = mapped_column(Integer, default=0)
    estimated_margin_kopecks: Mapped[int] = mapped_column(Integer, default=0)
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refunded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    financials_calculated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    chat_id: Mapped[int | None] = mapped_column(BigInteger)
    message_id: Mapped[int | None] = mapped_column(Integer)
    is_public_feed: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    feed_status: Mapped[str] = mapped_column(String(32), default="hidden", index=True)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    likes_count: Mapped[int] = mapped_column(Integer, default=0)
    shares_count: Mapped[int] = mapped_column(Integer, default=0)
    source_feed_task_id: Mapped[int | None] = mapped_column(ForeignKey("generation_tasks.id"))

    user: Mapped[User] = relationship(back_populates="tasks")


class FeedLike(TimestampMixin, Base):
    __tablename__ = "feed_likes"
    __table_args__ = (
        UniqueConstraint("user_id", "task_id", name="uq_feed_likes_user_task"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    task_id: Mapped[int] = mapped_column(ForeignKey("generation_tasks.id"), index=True)


class CreditPackage(TimestampMixin, Base):
    __tablename__ = "credit_packages"
    __table_args__ = (
        CheckConstraint("price_rub >= 0", name="ck_credit_packages_price_non_negative"),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255))
    description: Mapped[str | None] = mapped_column(Text)
    terms: Mapped[str | None] = mapped_column(Text)
    credits: Mapped[int] = mapped_column(Integer, default=0)
    photo_credits: Mapped[int] = mapped_column(Integer, default=0)
    video_credits: Mapped[int] = mapped_column(Integer, default=0)
    price_rub: Mapped[float] = mapped_column(Numeric(10, 2), default=0)
    is_unlimited: Mapped[bool] = mapped_column(Boolean, default=False)
    duration_days: Mapped[int | None] = mapped_column(Integer)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    position: Mapped[int] = mapped_column(Integer, default=100)


class Payment(TimestampMixin, Base):
    __tablename__ = "payments"
    __table_args__ = (
        CheckConstraint("amount_kopecks >= 0", name="ck_payments_amount_non_negative"),
        CheckConstraint(
            "affiliate_commission_kopecks >= 0 "
            "AND affiliate_commission_reversed_kopecks >= 0",
            name="ck_payments_affiliate_amounts_non_negative",
        ),
    )

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    package_id: Mapped[int | None] = mapped_column(ForeignKey("credit_packages.id"))
    provider: Mapped[str] = mapped_column(String(64), default="tbank")
    provider_payment_id: Mapped[str | None] = mapped_column(String(255), index=True)
    order_id: Mapped[str] = mapped_column(String(255), unique=True, index=True)
    amount_kopecks: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="created", index=True)
    payment_url: Mapped[str | None] = mapped_column(Text)
    affiliate_commission_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    affiliate_commission_kopecks: Mapped[int] = mapped_column(Integer, default=0)
    affiliate_commission_reversed_kopecks: Mapped[int] = mapped_column(Integer, default=0)
    reversed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reversal_reason: Mapped[str | None] = mapped_column(String(255))
    raw_payload: Mapped[JsonDict] = mapped_column(JSONB, default=dict)


class AffiliateWithdrawal(TimestampMixin, Base):
    __tablename__ = "affiliate_withdrawals"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    amount_kopecks: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(32), default="pending", index=True)
    details: Mapped[str] = mapped_column(Text)
    admin_comment: Mapped[str | None] = mapped_column(Text)


class CreditLedgerEntry(Base):
    __tablename__ = "credit_ledger_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    credit_type: Mapped[str] = mapped_column(String(32), index=True)
    balance_delta: Mapped[int] = mapped_column(Integer, default=0)
    debt_delta: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(String(128), default="balance_update")
    reference_type: Mapped[str | None] = mapped_column(String(64))
    reference_id: Mapped[str | None] = mapped_column(String(255))
    operation_key: Mapped[str | None] = mapped_column(String(255), index=True)
    metadata_json: Mapped[JsonDict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class AffiliateLedgerEntry(Base):
    __tablename__ = "affiliate_ledger_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), index=True)
    balance_delta_kopecks: Mapped[int] = mapped_column(Integer, default=0)
    earned_delta_kopecks: Mapped[int] = mapped_column(Integer, default=0)
    debt_delta_kopecks: Mapped[int] = mapped_column(Integer, default=0)
    reason: Mapped[str] = mapped_column(String(128), default="affiliate_update")
    reference_type: Mapped[str | None] = mapped_column(String(64))
    reference_id: Mapped[str | None] = mapped_column(String(255))
    operation_key: Mapped[str | None] = mapped_column(String(255), index=True)
    metadata_json: Mapped[JsonDict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ProviderCostEntry(Base):
    __tablename__ = "provider_cost_entries"

    id: Mapped[int] = mapped_column(primary_key=True)
    generation_task_id: Mapped[int] = mapped_column(
        ForeignKey("generation_tasks.id"),
        unique=True,
        index=True,
    )
    provider: Mapped[str] = mapped_column(String(64), default="unknown")
    provider_model: Mapped[str | None] = mapped_column(String(255))
    cost_kopecks: Mapped[int] = mapped_column(Integer, default=0)
    estimated_revenue_kopecks: Mapped[int] = mapped_column(Integer, default=0)
    estimated_margin_kopecks: Mapped[int] = mapped_column(Integer, default=0)
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    units: Mapped[int] = mapped_column(Integer, default=1)
    metadata_json: Mapped[JsonDict] = mapped_column(JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class GalleryItem(TimestampMixin, Base):
    __tablename__ = "gallery_items"

    id: Mapped[int] = mapped_column(primary_key=True)
    generation_task_id: Mapped[int | None] = mapped_column(ForeignKey("generation_tasks.id"))
    user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"), index=True)
    title: Mapped[str | None] = mapped_column(String(255))
    prompt: Mapped[str | None] = mapped_column(Text)
    media_url: Mapped[str] = mapped_column(Text)
    media_type: Mapped[str] = mapped_column(String(32), default="image")
    model_code: Mapped[str | None] = mapped_column(String(128))
    is_public: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    is_featured: Mapped[bool] = mapped_column(Boolean, default=False)


class PartnerLink(TimestampMixin, Base):
    __tablename__ = "partner_links"

    id: Mapped[int] = mapped_column(primary_key=True)
    code: Mapped[str] = mapped_column(String(128), unique=True, index=True)
    title: Mapped[str] = mapped_column(String(255))
    url: Mapped[str] = mapped_column(Text)
    description: Mapped[str | None] = mapped_column(Text)
    clicks: Mapped[int] = mapped_column(Integer, default=0)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, index=True)
    position: Mapped[int] = mapped_column(Integer, default=100)


class Broadcast(TimestampMixin, Base):
    __tablename__ = "broadcasts"

    id: Mapped[int] = mapped_column(primary_key=True)
    created_by_user_id: Mapped[int | None] = mapped_column(ForeignKey("users.id"))
    title: Mapped[str] = mapped_column(String(255))
    text: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(32), default="draft", index=True)
    sent_count: Mapped[int] = mapped_column(Integer, default=0)
    fail_count: Mapped[int] = mapped_column(Integer, default=0)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
