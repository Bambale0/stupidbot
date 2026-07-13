# Financial integrity and release safeguards

This change disables unsafe sales paths and adds database-backed financial controls for credits, referrals, payments, and generation costs.

## Release gates

Production startup requires Telegram and callback secrets plus an HTTPS public base URL. Custom universal-credit sales and new unlimited sales are disabled.

## Ledger and reversals

PostgreSQL triggers append immutable credit and affiliate ledger rows for balance and debt changes. Payment reversals remove unused credits; already-spent grants become debt settled by future grants. Withdrawn affiliate commissions become affiliate debt settled by future commissions.

## Partial-refund accounting policy

`PARTIAL_REFUNDED` must be calculated from a **cumulative refunded amount**, never by independently rounding each refund event. For every credit bucket and the affiliate commission, the cumulative reversal target is:

```text
floor(original_grant * cumulative_refunded_kopecks / original_payment_kopecks)
```

Each callback applies only the difference between that target and the amount already reversed. A confirmed full refund reverses the complete remaining tail so integer rounding cannot leave one credit or commission kopeck behind. Credits that were already spent become debt; balances remain non-negative.

Partial refunds for unlimited packages require manual review because fractional access duration is not a well-defined accounting unit.

The pure calculation is implemented in `app/services/partial_refunds.py` and covered by the PostgreSQL financial regression. Webhook activation for `PARTIAL_REFUNDED` remains gated until a staging terminal confirms whether the notification `Amount` represents the individual refund, the cumulative refunded amount, or the original payment amount. Ambiguous payment payloads must not mutate balances automatically.

## Generation lifecycle

Generation tasks receive unique idempotency keys. Polling and callbacks finalize tasks through one row-locked transition, so only one worker may refund, publish, and notify. A reconciliation pass closes orphan submissions after the configured timeout, with a wider safety window for synchronous generation.

## Cost and margin

Final tasks store provider cost, estimated revenue, and estimated margin. Model configuration may define flat or per-second provider cost fields, including fallback provider fields. Administrators can use `/finance` or **Админка → Финансы**.

## Deployment

Back up PostgreSQL, deploy, enable the `feed` and `finance` plugins, set production secrets and approved accounting values, run `python3 -m scripts.init_db`, then run `bash scripts/ci.sh` before restarting the service.

The tracked staging checklist is GitHub issue #3. Do not mark PR #2 ready for review until backup/restore, real T-Bank payment and reversal, referral commission, Comet/KIE generation, and orphan-task reconciliation have been completed on staging.
