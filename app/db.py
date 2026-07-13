from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncAttrs, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase

from app.config import Settings


class Base(AsyncAttrs, DeclarativeBase):
    pass


SCHEMA_COMPAT_SQL: tuple[str, ...] = (
    """
    ALTER TABLE users
    ADD COLUMN IF NOT EXISTS is_blocked boolean NOT NULL DEFAULT false
    """,
    """
    ALTER TABLE users
    ADD COLUMN IF NOT EXISTS partner_code varchar(64)
    """,
    """
    ALTER TABLE users
    ADD COLUMN IF NOT EXISTS referred_by_user_id integer REFERENCES users(id)
    """,
    """
    CREATE OR REPLACE FUNCTION stupidbot_base36(input_value bigint)
    RETURNS text AS $$
    DECLARE
        alphabet text := '0123456789abcdefghijklmnopqrstuvwxyz';
        value bigint := abs(input_value);
        result text := '';
        remainder integer;
    BEGIN
        IF value = 0 THEN
            RETURN '0';
        END IF;
        WHILE value > 0 LOOP
            remainder := (value % 36)::integer;
            result := substr(alphabet, remainder + 1, 1) || result;
            value := value / 36;
        END LOOP;
        RETURN result;
    END;
    $$ LANGUAGE plpgsql IMMUTABLE STRICT
    """,
    """
    INSERT INTO referral_code_aliases (user_id, code, created_at, updated_at)
    SELECT id, lower(partner_code), now(), now()
    FROM users
    WHERE partner_code IS NOT NULL
      AND lower(partner_code) <> ('u' || stupidbot_base36(telegram_id))
    ON CONFLICT (code) DO NOTHING
    """,
    """
    UPDATE users
    SET partner_code = 'migration:' || id::text
    WHERE partner_code IS DISTINCT FROM ('u' || stupidbot_base36(telegram_id))
    """,
    """
    UPDATE users
    SET partner_code = 'u' || stupidbot_base36(telegram_id)
    WHERE partner_code IS DISTINCT FROM ('u' || stupidbot_base36(telegram_id))
    """,
    """
    CREATE OR REPLACE FUNCTION stupidbot_normalize_partner_code()
    RETURNS trigger AS $$
    BEGIN
        IF NEW.telegram_id IS NOT NULL THEN
            NEW.partner_code := 'u' || stupidbot_base36(NEW.telegram_id);
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    DROP TRIGGER IF EXISTS trg_stupidbot_normalize_partner_code ON users
    """,
    """
    CREATE TRIGGER trg_stupidbot_normalize_partner_code
    BEFORE INSERT OR UPDATE ON users
    FOR EACH ROW EXECUTE FUNCTION stupidbot_normalize_partner_code()
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ix_users_partner_code_unique
    ON users (partner_code)
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_users_referred_by_user_id
    ON users (referred_by_user_id)
    """,
    """
    ALTER TABLE users
    ADD COLUMN IF NOT EXISTS affiliate_balance_kopecks integer NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE users
    ADD COLUMN IF NOT EXISTS photo_credits_balance integer NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE users
    ADD COLUMN IF NOT EXISTS video_credits_balance integer NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE users
    ADD COLUMN IF NOT EXISTS affiliate_earned_kopecks integer NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE users
    ADD COLUMN IF NOT EXISTS affiliate_commission_rate_bps integer NOT NULL DEFAULT 3000
    """,
    """
    ALTER TABLE users
    ADD COLUMN IF NOT EXISTS common_credit_debt integer NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE users
    ADD COLUMN IF NOT EXISTS photo_credit_debt integer NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE users
    ADD COLUMN IF NOT EXISTS video_credit_debt integer NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE users
    ADD COLUMN IF NOT EXISTS affiliate_debt_kopecks integer NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE payments
    ADD COLUMN IF NOT EXISTS affiliate_commission_user_id integer REFERENCES users(id)
    """,
    """
    ALTER TABLE payments
    ADD COLUMN IF NOT EXISTS affiliate_commission_kopecks integer NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE payments
    ADD COLUMN IF NOT EXISTS affiliate_commission_reversed_kopecks integer NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE payments
    ADD COLUMN IF NOT EXISTS reversed_at timestamp with time zone
    """,
    """
    ALTER TABLE payments
    ADD COLUMN IF NOT EXISTS reversal_reason varchar(255)
    """,
    """
    ALTER TABLE credit_packages
    ADD COLUMN IF NOT EXISTS terms text
    """,
    """
    ALTER TABLE credit_packages
    ADD COLUMN IF NOT EXISTS photo_credits integer NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE credit_packages
    ADD COLUMN IF NOT EXISTS video_credits integer NOT NULL DEFAULT 0
    """,
    """
    UPDATE credit_packages
    SET is_enabled = false
    WHERE is_unlimited = true
    """,
    """
    ALTER TABLE generation_tasks
    ADD COLUMN IF NOT EXISTS is_public_feed boolean NOT NULL DEFAULT false
    """,
    """
    ALTER TABLE generation_tasks
    ADD COLUMN IF NOT EXISTS feed_status varchar(32) NOT NULL DEFAULT 'hidden'
    """,
    """
    ALTER TABLE generation_tasks
    ADD COLUMN IF NOT EXISTS published_at timestamp with time zone
    """,
    """
    ALTER TABLE generation_tasks
    ADD COLUMN IF NOT EXISTS likes_count integer NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE generation_tasks
    ADD COLUMN IF NOT EXISTS shares_count integer NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE generation_tasks
    ADD COLUMN IF NOT EXISTS source_feed_task_id integer REFERENCES generation_tasks(id)
    """,
    """
    ALTER TABLE generation_tasks
    ADD COLUMN IF NOT EXISTS idempotency_key varchar(255)
    """,
    """
    UPDATE generation_tasks
    SET idempotency_key = 'legacy:' || id::text
    WHERE idempotency_key IS NULL OR idempotency_key = ''
    """,
    """
    CREATE UNIQUE INDEX IF NOT EXISTS ix_generation_tasks_idempotency_key
    ON generation_tasks (idempotency_key)
    """,
    """
    ALTER TABLE generation_tasks
    ALTER COLUMN idempotency_key SET NOT NULL
    """,
    """
    ALTER TABLE generation_tasks
    ADD COLUMN IF NOT EXISTS finalized_at timestamp with time zone
    """,
    """
    ALTER TABLE generation_tasks
    ADD COLUMN IF NOT EXISTS refunded_at timestamp with time zone
    """,
    """
    ALTER TABLE generation_tasks
    ADD COLUMN IF NOT EXISTS provider_cost_kopecks integer NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE generation_tasks
    ADD COLUMN IF NOT EXISTS estimated_revenue_kopecks integer NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE generation_tasks
    ADD COLUMN IF NOT EXISTS estimated_margin_kopecks integer NOT NULL DEFAULT 0
    """,
    """
    ALTER TABLE generation_tasks
    ADD COLUMN IF NOT EXISTS financials_calculated_at timestamp with time zone
    """,
    """
    CREATE INDEX IF NOT EXISTS ix_generation_tasks_public_feed
    ON generation_tasks (is_public_feed, published_at)
    """,
    """
    UPDATE generation_models
    SET price_credits = 0
    WHERE price_credits < 0
    """,
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_generation_models_price_non_negative'
        ) THEN
            ALTER TABLE generation_models
            ADD CONSTRAINT ck_generation_models_price_non_negative CHECK (price_credits >= 0);
        END IF;
    END
    $$
    """,
    """
    UPDATE generation_tasks
    SET cost_credits = 0
    WHERE cost_credits < 0
    """,
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_generation_tasks_cost_non_negative'
        ) THEN
            ALTER TABLE generation_tasks
            ADD CONSTRAINT ck_generation_tasks_cost_non_negative CHECK (cost_credits >= 0);
        END IF;
    END
    $$
    """,
    """
    UPDATE users SET
        credits_balance = greatest(credits_balance, 0),
        photo_credits_balance = greatest(photo_credits_balance, 0),
        video_credits_balance = greatest(video_credits_balance, 0),
        common_credit_debt = greatest(common_credit_debt, 0),
        photo_credit_debt = greatest(photo_credit_debt, 0),
        video_credit_debt = greatest(video_credit_debt, 0),
        affiliate_balance_kopecks = greatest(affiliate_balance_kopecks, 0),
        affiliate_earned_kopecks = greatest(affiliate_earned_kopecks, 0),
        affiliate_debt_kopecks = greatest(affiliate_debt_kopecks, 0)
    """,
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_users_credit_balances_non_negative'
        ) THEN
            ALTER TABLE users ADD CONSTRAINT ck_users_credit_balances_non_negative CHECK (
                credits_balance >= 0 AND photo_credits_balance >= 0 AND video_credits_balance >= 0
            );
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_users_credit_debts_non_negative'
        ) THEN
            ALTER TABLE users ADD CONSTRAINT ck_users_credit_debts_non_negative CHECK (
                common_credit_debt >= 0 AND photo_credit_debt >= 0 AND video_credit_debt >= 0
            );
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_users_affiliate_amounts_non_negative'
        ) THEN
            ALTER TABLE users ADD CONSTRAINT ck_users_affiliate_amounts_non_negative CHECK (
                affiliate_balance_kopecks >= 0 AND affiliate_earned_kopecks >= 0
                AND affiliate_debt_kopecks >= 0
            );
        END IF;
    END
    $$
    """,
    """
    UPDATE generation_tasks
    SET provider_cost_kopecks = greatest(provider_cost_kopecks, 0)
    WHERE provider_cost_kopecks < 0
    """,
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_generation_tasks_provider_cost_non_negative'
        ) THEN
            ALTER TABLE generation_tasks
            ADD CONSTRAINT ck_generation_tasks_provider_cost_non_negative
            CHECK (provider_cost_kopecks >= 0);
        END IF;
    END
    $$
    """,
    """
    UPDATE credit_packages SET price_rub = 0 WHERE price_rub < 0
    """,
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_credit_packages_price_non_negative'
        ) THEN
            ALTER TABLE credit_packages
            ADD CONSTRAINT ck_credit_packages_price_non_negative CHECK (price_rub >= 0);
        END IF;
    END
    $$
    """,
    """
    UPDATE payments SET
        amount_kopecks = greatest(amount_kopecks, 0),
        affiliate_commission_kopecks = greatest(affiliate_commission_kopecks, 0),
        affiliate_commission_reversed_kopecks = greatest(
            affiliate_commission_reversed_kopecks, 0
        )
    """,
    """
    DO $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_payments_amount_non_negative'
        ) THEN
            ALTER TABLE payments ADD CONSTRAINT ck_payments_amount_non_negative
            CHECK (amount_kopecks >= 0);
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM pg_constraint WHERE conname = 'ck_payments_affiliate_amounts_non_negative'
        ) THEN
            ALTER TABLE payments ADD CONSTRAINT ck_payments_affiliate_amounts_non_negative CHECK (
                affiliate_commission_kopecks >= 0
                AND affiliate_commission_reversed_kopecks >= 0
            );
        END IF;
    END
    $$
    """,
    """
    CREATE OR REPLACE FUNCTION stupidbot_credit_ledger_append()
    RETURNS trigger AS $$
    BEGIN
        IF OLD.credits_balance IS DISTINCT FROM NEW.credits_balance
           OR OLD.common_credit_debt IS DISTINCT FROM NEW.common_credit_debt THEN
            INSERT INTO credit_ledger_entries (
                user_id, credit_type, balance_delta, debt_delta, reason, metadata_json
            ) VALUES (
                NEW.id,
                'common',
                NEW.credits_balance - OLD.credits_balance,
                NEW.common_credit_debt - OLD.common_credit_debt,
                'user_balance_update',
                jsonb_build_object(
                    'old_balance', OLD.credits_balance,
                    'new_balance', NEW.credits_balance,
                    'old_debt', OLD.common_credit_debt,
                    'new_debt', NEW.common_credit_debt
                )
            );
        END IF;

        IF OLD.photo_credits_balance IS DISTINCT FROM NEW.photo_credits_balance
           OR OLD.photo_credit_debt IS DISTINCT FROM NEW.photo_credit_debt THEN
            INSERT INTO credit_ledger_entries (
                user_id, credit_type, balance_delta, debt_delta, reason, metadata_json
            ) VALUES (
                NEW.id,
                'photo',
                NEW.photo_credits_balance - OLD.photo_credits_balance,
                NEW.photo_credit_debt - OLD.photo_credit_debt,
                'user_balance_update',
                jsonb_build_object(
                    'old_balance', OLD.photo_credits_balance,
                    'new_balance', NEW.photo_credits_balance,
                    'old_debt', OLD.photo_credit_debt,
                    'new_debt', NEW.photo_credit_debt
                )
            );
        END IF;

        IF OLD.video_credits_balance IS DISTINCT FROM NEW.video_credits_balance
           OR OLD.video_credit_debt IS DISTINCT FROM NEW.video_credit_debt THEN
            INSERT INTO credit_ledger_entries (
                user_id, credit_type, balance_delta, debt_delta, reason, metadata_json
            ) VALUES (
                NEW.id,
                'video',
                NEW.video_credits_balance - OLD.video_credits_balance,
                NEW.video_credit_debt - OLD.video_credit_debt,
                'user_balance_update',
                jsonb_build_object(
                    'old_balance', OLD.video_credits_balance,
                    'new_balance', NEW.video_credits_balance,
                    'old_debt', OLD.video_credit_debt,
                    'new_debt', NEW.video_credit_debt
                )
            );
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    DROP TRIGGER IF EXISTS trg_stupidbot_credit_ledger ON users
    """,
    """
    CREATE TRIGGER trg_stupidbot_credit_ledger
    AFTER UPDATE OF credits_balance, photo_credits_balance, video_credits_balance,
                    common_credit_debt, photo_credit_debt, video_credit_debt
    ON users
    FOR EACH ROW EXECUTE FUNCTION stupidbot_credit_ledger_append()
    """,
    """
    CREATE OR REPLACE FUNCTION stupidbot_affiliate_ledger_append()
    RETURNS trigger AS $$
    BEGIN
        IF OLD.affiliate_balance_kopecks IS DISTINCT FROM NEW.affiliate_balance_kopecks
           OR OLD.affiliate_earned_kopecks IS DISTINCT FROM NEW.affiliate_earned_kopecks
           OR OLD.affiliate_debt_kopecks IS DISTINCT FROM NEW.affiliate_debt_kopecks THEN
            INSERT INTO affiliate_ledger_entries (
                user_id,
                balance_delta_kopecks,
                earned_delta_kopecks,
                debt_delta_kopecks,
                reason,
                metadata_json
            ) VALUES (
                NEW.id,
                NEW.affiliate_balance_kopecks - OLD.affiliate_balance_kopecks,
                NEW.affiliate_earned_kopecks - OLD.affiliate_earned_kopecks,
                NEW.affiliate_debt_kopecks - OLD.affiliate_debt_kopecks,
                'affiliate_balance_update',
                jsonb_build_object(
                    'old_balance', OLD.affiliate_balance_kopecks,
                    'new_balance', NEW.affiliate_balance_kopecks,
                    'old_earned', OLD.affiliate_earned_kopecks,
                    'new_earned', NEW.affiliate_earned_kopecks,
                    'old_debt', OLD.affiliate_debt_kopecks,
                    'new_debt', NEW.affiliate_debt_kopecks
                )
            );
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    DROP TRIGGER IF EXISTS trg_stupidbot_affiliate_ledger ON users
    """,
    """
    CREATE TRIGGER trg_stupidbot_affiliate_ledger
    AFTER UPDATE OF affiliate_balance_kopecks, affiliate_earned_kopecks, affiliate_debt_kopecks
    ON users
    FOR EACH ROW EXECUTE FUNCTION stupidbot_affiliate_ledger_append()
    """,
    """
    CREATE OR REPLACE FUNCTION stupidbot_reject_ledger_mutation()
    RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'financial ledger rows are immutable';
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    DROP TRIGGER IF EXISTS trg_credit_ledger_immutable ON credit_ledger_entries
    """,
    """
    CREATE TRIGGER trg_credit_ledger_immutable
    BEFORE UPDATE OR DELETE ON credit_ledger_entries
    FOR EACH ROW EXECUTE FUNCTION stupidbot_reject_ledger_mutation()
    """,
    """
    DROP TRIGGER IF EXISTS trg_affiliate_ledger_immutable ON affiliate_ledger_entries
    """,
    """
    CREATE TRIGGER trg_affiliate_ledger_immutable
    BEFORE UPDATE OR DELETE ON affiliate_ledger_entries
    FOR EACH ROW EXECUTE FUNCTION stupidbot_reject_ledger_mutation()
    """,
    """
    DROP TRIGGER IF EXISTS trg_provider_cost_immutable ON provider_cost_entries
    """,
    """
    CREATE TRIGGER trg_provider_cost_immutable
    BEFORE UPDATE OR DELETE ON provider_cost_entries
    FOR EACH ROW EXECUTE FUNCTION stupidbot_reject_ledger_mutation()
    """,
)


def build_engine(settings: Settings):
    return create_async_engine(settings.database_url, pool_pre_ping=True, echo=False)


def build_session_factory(engine) -> async_sessionmaker[AsyncSession]:
    return async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


async def init_db(engine) -> None:
    from app import models  # noqa: F401

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        if conn.dialect.name == "postgresql":
            for statement in SCHEMA_COMPAT_SQL:
                await conn.execute(text(statement))


@asynccontextmanager
async def session_scope(
    session_factory: async_sessionmaker[AsyncSession],
) -> AsyncIterator[AsyncSession]:
    async with session_factory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise
