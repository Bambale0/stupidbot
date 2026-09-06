# BANANA AI Platform

> **Production Telegram AI product** · FastAPI · aiogram · PostgreSQL · Redis · T-Bank billing · hybrid subscriptions/credits · staging/rollback automation
>
> Repository codename: `stupidbot`.

BANANA is a production Telegram AI platform for image and video generation. Its strongest portfolio angle is not the model catalog itself, but the operational and financial layer around it: payments, subscriptions, credit packages, idempotent accounting, background broadcasts, database migrations, staging rollout, backup/restore checks and rollback safety.

## Engineering highlights

- FastAPI webhook application with aiogram.
- PostgreSQL for durable product and financial state.
- Redis for FSM/runtime coordination.
- Image/video generation through Comet and KIE provider adapters.
- Telegram Mini App and public gallery/feed flows.
- Saved reference sets and repeat-generation workflows.
- T-Bank payment integration.
- Hybrid economy: time-based subscription + independent credit balances.
- Partner/referral accounting and withdrawals.
- Idempotent payment confirmation and reversal logic.
- Non-blocking batch broadcasts with persisted progress.
- CI financial-integrity gate on PostgreSQL + Redis.
- Staging rollout with backup, restore verification, health checks and rollback.

## Product architecture

```text
Telegram / Mini App
        |
        v
 FastAPI + aiogram
        |
        +--> generation plugins ---> Comet / KIE
        +--> payment services ------> T-Bank
        +--> admin operations
        +--> partner/referral flows
        |
        +--> PostgreSQL
        +--> Redis
```

The application is organized as plugins for core UX, generation, references, feed/gallery, payments, partners, admin and finance rather than one large handler module.

## Billing model

BANANA deliberately keeps two value systems independent:

1. **Subscription** — access for a limited period.
2. **Credits** — separate image/video/universal balances.

Buying a subscription does not erase credits, and buying credits does not change subscription expiry.

Financial mutation paths are designed to be idempotent:

- duplicate provider/payment callbacks do not credit twice;
- repeated manual confirmation does not extend a subscription twice;
- refunds/reversals are represented as explicit ledger operations;
- negative prices and balances are protected by constraints and validation.

## Reliable broadcasts

Admin broadcasts run outside the Telegram webhook request path. Recipients are read in bounded batches, blocked users are excluded, and progress counters are persisted after each batch. Interrupted runs are not blindly restarted, avoiding duplicate sends to part of the audience.

## Staging and release safety

The staging rollout performs a guarded sequence:

```text
candidate SHA
    |
    +--> immutable archive + checksum
    +--> code/database backup
    +--> isolated restore verification
    +--> compile/contracts
    +--> migrations + regressions
    +--> PostgreSQL/Redis readiness
    +--> service restart
    +--> local/public health checks
    |
    +--> success: release candidate accepted
    |
    +--> failure: rollback path remains available
```

Paid provider and payment smoke workflows are manual-only so CI cannot accidentally spend provider credits or charge real cards.

## Stack

| Area | Technology |
| --- | --- |
| Backend | Python 3.11, FastAPI, aiogram |
| Data | PostgreSQL, Redis |
| AI providers | Comet API, KIE.AI |
| Payments | T-Bank |
| Runtime | systemd, Nginx |
| Tests | compile/contracts + financial regression suites |
| Delivery | GitHub Actions, staging rollout, rollback checks |

## Local development

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip setuptools wheel
python -m pip install -e '.[dev]'
cp .env.example .env
python -m scripts.init_db
bash scripts/ci.sh
```

## Security principles

- environment secrets are not committed;
- provider/payment callbacks are verified before financial mutation;
- users do not receive internal tracebacks or provider secrets;
- saved references remain owner-scoped;
- payment/finalization/refund handlers are idempotent;
- database backup/restore is part of release verification.

## Portfolio note

BANANA is the portfolio case for **billing integrity and production operations**: it shows how a Telegram AI product behaves when real money, migrations, background jobs, provider failures and deployment rollback matter.
