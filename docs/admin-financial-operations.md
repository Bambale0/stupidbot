# Audited admin financial operations

## Required reason

Manual balance changes and manual payment actions require an explicit reason.

Examples:

- `20 | compensation for a failed generation`
- `339795159 20 | promotional credit grant`
- `stupidbot-12-a1b2c3 | transfer verified in the bank`

The operation is rejected when the reason is missing.

## Credit ledger

Every actual administrative balance delta creates a `CreditLedgerEntry` with:

- the administrator user and Telegram ID;
- target user;
- balance before and after;
- actual delta after the non-negative balance guard;
- human reason;
- operation key and reference.

Unlimited access and affiliate-rate changes are also recorded in the same immutable audit stream with before/after metadata.

## Payments

Manual payment confirmation, cancellation and reversal require a reason.

A payment stores an `admin_audit` event in its `raw_payload`. Manual grants and reversals also create credit and affiliate ledger entries.

Paid payments can be reversed from the payment card. Reversal uses `reverse_paid_payment`:

- granted credits are removed;
- already-spent credits become debt;
- timed unlimited access is reduced;
- affiliate commission is removed or becomes affiliate debt;
- repeated reversal is rejected.

## Package sale guard

An enabled package must:

- have a price greater than zero;
- grant at least one credit, or grant timed unlimited access with a positive duration;
- not be a technical regression package.

Invalid disabled packages may be edited, but they cannot be enabled until valid. An already-enabled package cannot be edited into an invalid sale state.
