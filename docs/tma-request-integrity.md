# Mini App request integrity

## Payment contract

`POST /api/tma/app/payments` accepts only:

```json
{"package_id": 123}
```

Unknown fields, client-provided prices and arbitrary credit quantities are rejected before the payment handler runs.

Every request must include:

- valid Telegram Mini App identity (`X-Telegram-Init-Data` or the existing TMA authorization form);
- `Content-Type: application/json`;
- an `X-Idempotency-Key` containing 16–128 URL-safe characters.

The Mini App creates one idempotency key for the selected package and current package signature. It reuses that key for a retry of the same payment attempt and resets it when the package or its price changes.

Successful payment-init responses are replayable for 24 hours. A concurrent duplicate receives HTTP 409 instead of creating a second payment.

## Rate limits

- payment creation: 10 requests per identity per minute;
- feed mutations: 60 requests per identity per minute.

Rate-limit responses use HTTP 429 and include `Retry-After: 60`.

## Feed mutation contract

Feed action requests accept only:

```json
{"action": "like"}
```

Allowed actions are `like`, `share`, `publish` and `remove`. Extra fields, including prompt text, are rejected.

## Privacy

Public feed serialization must never include the author's prompt. The regression scans public serializers and frontend request contracts to prevent prompt leakage or accidental reintroduction of client-controlled payment amounts.
