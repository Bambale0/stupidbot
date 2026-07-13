# Financial integrity and release safeguards

This change disables unsafe sales paths and adds database-backed financial controls for credits, referrals, payments, and generation costs.

## Release gates

Production startup requires Telegram and callback secrets plus an HTTPS public base URL. Custom universal-credit sales and new unlimited sales are disabled.

## Ledger and reversals

PostgreSQL triggers append immutable credit and affiliate ledger rows for balance and debt changes. Payment reversals remove unused credits; already-spent grants become debt settled by future grants. Withdrawn affiliate commissions become affiliate debt settled by future commissions.

## Generation lifecycle

Generation tasks receive unique idempotency keys. Polling and callbacks finalize tasks through one row-locked transition, so only one worker may refund, publish, and notify. A reconciliation pass closes orphan submissions after the configured timeout, with a wider safety window for synchronous generation.

## Cost and margin

Final tasks store provider cost, estimated revenue, and estimated margin. Model configuration may define flat or per-second provider cost fields, including fallback provider fields. Administrators can use `/finance` or **Админка → Финансы**.

## Deployment

Back up PostgreSQL, deploy, enable the `feed` and `finance` plugins, set production secrets and approved accounting values, run `python3 -m scripts.init_db`, then run `bash scripts/ci.sh` before restarting the service.
