# Nano Banana 2 Lite and billing policy

## Image model

The product code `nano-banana` is retained for callback and history compatibility, but it now routes directly to KIE model `nano-banana-2-lite`.

Provider requests use:

- `POST /api/v1/jobs/createTask`;
- top-level `model: nano-banana-2-lite`;
- `input.prompt`;
- `input.image_urls`, up to 10 references;
- `input.aspect_ratio`;
- optional `callBackUrl`.

The Lite request does not send the legacy `image_input`, `resolution`, or `output_format` fields. Product UI shows fixed 1K output for this model.

## Payments and tariffs

StupidBot sells one-time credit packages. It does not currently implement recurring card charges, renewals, or a subscription lifecycle.

- custom universal-credit purchases remain disabled;
- legacy unlimited packages remain disabled and hidden;
- Starter is a one-time photo package;
- Creator is a one-time hybrid package with 50 photo credits and 20 video credits;
- video-specific credits are consumed before universal credits;
- universal credits can cover the remaining amount for old balances and compatibility;
- package grants first settle matching credit debt created by payment reversals.

## Referrals

- referral binding is one-time and rejects self-referrals and cycles;
- commission is recorded once per paid order;
- payment reversals remove commission or create affiliate debt when funds were already reserved/withdrawn;
- rejected withdrawals restore only the amount remaining after affiliate debt is settled.
