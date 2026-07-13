from __future__ import annotations


class PartialRefundPolicyError(ValueError):
    """Raised when a partial-refund calculation cannot be performed safely."""


def cumulative_reversal_target(
    original_units: int,
    cumulative_refunded_kopecks: int,
    original_payment_kopecks: int,
    *,
    force_full: bool = False,
) -> int:
    """Return the cumulative number of units that must be reversed.

    Rounding is always down for intermediate partial refunds. A confirmed full
    refund reverses the complete remaining grant so integer rounding cannot
    leave one credit or commission kopeck behind.
    """

    units = max(0, int(original_units or 0))
    payment = int(original_payment_kopecks or 0)
    if payment <= 0:
        raise PartialRefundPolicyError("original payment amount must be positive")

    refunded = max(0, min(int(cumulative_refunded_kopecks or 0), payment))
    if force_full or refunded >= payment:
        return units
    return units * refunded // payment


def incremental_reversal_delta(
    original_units: int,
    already_reversed_units: int,
    cumulative_refunded_kopecks: int,
    original_payment_kopecks: int,
    *,
    force_full: bool = False,
) -> int:
    """Return only the new reversal delta for an idempotent callback."""

    target = cumulative_reversal_target(
        original_units,
        cumulative_refunded_kopecks,
        original_payment_kopecks,
        force_full=force_full,
    )
    already_reversed = max(0, min(int(already_reversed_units or 0), target))
    return target - already_reversed
