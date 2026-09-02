# Runtime readiness and operational metrics

## Liveness

`GET /health` is a shallow process liveness endpoint. It does not prove that PostgreSQL, Redis, Telegram or the generation tracker are usable.

## Readiness

`GET /ready` checks, with an individual timeout:

- PostgreSQL using `SELECT 1`;
- Redis using `PING`;
- Telegram Bot API using `getMe`;
- the local `TaskTracker` background task.

A fully ready response uses HTTP 200:

```json
{
  "status": "ready",
  "checks": {
    "database": "ok",
    "redis": "ok",
    "telegram": "ok",
    "tracker": "ok"
  },
  "latency_ms": {
    "database": 4,
    "redis": 1,
    "telegram": 87,
    "tracker": 0
  }
}
```

Any required component failure produces HTTP 503 with `status=not_ready`. The endpoint never returns credentials or exception messages and is explicitly marked `no-store`.

## Operational metrics

`GET /ops/metrics` returns low-cardinality aggregate data only:

- package totals, enabled packages and currently sellable packages;
- generation task counts by status and active task total;
- payment counts by status;
- broadcast counts by status;
- TaskTracker running state;
- request totals, response status totals and latest latency for package catalog, payment creation, provider callbacks, payment callbacks and feed mutations.

It intentionally excludes Telegram IDs, prompts, request bodies, provider payloads, payment URLs and tokens.

### Authentication

Set `OPERATIONS_TOKEN` and send either:

```text
X-Operations-Token: <token>
```

or:

```text
Authorization: Bearer <token>
```

When `OPERATIONS_TOKEN` is not set, the endpoint uses `TELEGRAM_SECRET_TOKEN` as a compatibility fallback. Production refuses unauthenticated metrics access.

## Storage

HTTP counters are kept in Redis under a single aggregate hash and expire after 30 days. Business state remains authoritative in PostgreSQL and is read at request time.

## Monitoring guidance

- use `/health` only for process liveness;
- use `/ready` for load balancer and deployment gates;
- poll `/ops/metrics` from an authenticated monitoring job;
- alert when readiness is HTTP 503, `tracker_running` is false, payment callback failures rise, or active generation tasks grow without successful completions.
