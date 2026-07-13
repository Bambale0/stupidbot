from __future__ import annotations

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import AffiliateLedgerEntry, CreditLedgerEntry, GenerationTask, Payment, User
from app.services.financial_tasks import ACTIVE_ORPHAN_STATES


async def financial_summary(session: AsyncSession) -> dict[str, Any]:
    async def total(column: Any, condition: Any | None = None) -> int:
        query = select(func.coalesce(func.sum(column), 0))
        if condition is not None:
            query = query.where(condition)
        return int(await session.scalar(query) or 0)

    rows = list(
        await session.execute(
            select(
                GenerationTask.model_code,
                func.count(GenerationTask.id),
                func.coalesce(func.sum(GenerationTask.provider_cost_kopecks), 0),
                func.coalesce(func.sum(GenerationTask.estimated_margin_kopecks), 0),
            )
            .where(GenerationTask.financials_calculated_at.is_not(None))
            .group_by(GenerationTask.model_code)
        )
    )
    return {
        "paid_revenue_kopecks": await total(Payment.amount_kopecks, Payment.status == "paid"),
        "reversed_revenue_kopecks": await total(Payment.amount_kopecks, Payment.status == "reversed"),
        "provider_cost_kopecks": await total(GenerationTask.provider_cost_kopecks),
        "estimated_revenue_kopecks": await total(GenerationTask.estimated_revenue_kopecks),
        "estimated_margin_kopecks": await total(GenerationTask.estimated_margin_kopecks),
        "affiliate_payable_kopecks": await total(User.affiliate_balance_kopecks),
        "affiliate_debt_kopecks": await total(User.affiliate_debt_kopecks),
        "credit_debt": await total(User.common_credit_debt + User.photo_credit_debt + User.video_credit_debt),
        "orphan_tasks": int(await session.scalar(select(func.count()).select_from(GenerationTask).where(GenerationTask.status.in_(ACTIVE_ORPHAN_STATES), GenerationTask.provider_task_id.is_(None))) or 0),
        "credit_ledger_entries": int(await session.scalar(select(func.count()).select_from(CreditLedgerEntry)) or 0),
        "affiliate_ledger_entries": int(await session.scalar(select(func.count()).select_from(AffiliateLedgerEntry)) or 0),
        "by_model": [
            {"model_code": row[0], "tasks": int(row[1]), "provider_cost_kopecks": int(row[2]), "estimated_margin_kopecks": int(row[3])}
            for row in rows
        ],
    }
