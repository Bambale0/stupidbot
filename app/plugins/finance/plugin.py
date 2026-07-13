from __future__ import annotations

from aiogram import Dispatcher, F, Router
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.types import CallbackQuery, Message

from app.context import AppContext
from app.db import session_scope
from app.plugins.common import ensure_user_for_callback, ensure_user_for_message, is_admin_user
from app.services.financial_integrity import financial_summary
from app.ui import navigation_keyboard

router = Router(name="finance")


def _rub(kopecks: int) -> str:
    return f"{int(kopecks or 0) / 100:,.2f} ₽".replace(",", " ")


def _summary_text(summary: dict) -> str:
    lines = [
        "<b>Финансовая целостность</b>",
        "",
        f"Оплаченная выручка: <b>{_rub(summary['paid_revenue_kopecks'])}</b>",
        f"Сторнировано: <b>{_rub(summary['reversed_revenue_kopecks'])}</b>",
        f"Расходы провайдеров: <b>{_rub(summary['provider_cost_kopecks'])}</b>",
        f"Оценочная выручка генераций: <b>{_rub(summary['estimated_revenue_kopecks'])}</b>",
        f"Оценочная маржа: <b>{_rub(summary['estimated_margin_kopecks'])}</b>",
        "",
        f"К выплате партнёрам: <b>{_rub(summary['affiliate_payable_kopecks'])}</b>",
        f"Партнёрский долг после reversal: <b>{_rub(summary['affiliate_debt_kopecks'])}</b>",
        f"Кредитный долг пользователей: <b>{summary['credit_debt']}</b>",
        f"Осиротевшие задачи: <b>{summary['orphan_tasks']}</b>",
        "",
        f"Credit ledger: <b>{summary['credit_ledger_entries']}</b>",
        f"Affiliate ledger: <b>{summary['affiliate_ledger_entries']}</b>",
    ]
    by_model = summary.get("by_model") or []
    if by_model:
        lines.extend(["", "<b>По моделям</b>"])
        for row in by_model[:12]:
            lines.append(
                f"• {row['model_code']}: {row['tasks']} задач · "
                f"cost {_rub(row['provider_cost_kopecks'])} · "
                f"margin {_rub(row['estimated_margin_kopecks'])}"
            )
    else:
        lines.extend(
            [
                "",
                "Расходы провайдеров пока не рассчитаны. Настройте стоимость моделей и "
                "ценность фото/видео-кредитов.",
            ]
        )
    return "\n".join(lines)


@router.message(Command("finance"))
async def finance_command(message: Message, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    user = await ensure_user_for_message(message, context)
    if not is_admin_user(user, context):
        await message.answer("Нет доступа.")
        return
    async with session_scope(context.session_factory) as session:
        summary = await financial_summary(session)
    await message.answer(
        _summary_text(summary),
        reply_markup=navigation_keyboard(back_callback="admin:ux:overview"),
    )


@router.callback_query(F.data == "admin:finance")
async def finance_callback(callback: CallbackQuery, context: AppContext, state: FSMContext) -> None:
    await state.clear()
    user = await ensure_user_for_callback(callback, context)
    if not is_admin_user(user, context):
        await callback.answer("Нет доступа", show_alert=True)
        return
    async with session_scope(context.session_factory) as session:
        summary = await financial_summary(session)
    if callback.message:
        await callback.message.answer(
            _summary_text(summary),
            reply_markup=navigation_keyboard(back_callback="admin:ux:overview"),
        )
    await callback.answer()


def setup(dispatcher: Dispatcher, context: AppContext) -> None:
    dispatcher.include_router(router)
