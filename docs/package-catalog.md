# Package catalog and Mini App payments

## Source of truth

Sellable packages are stored in `CreditPackage`. The admin database controls:

- title and description;
- photo, video and universal credit grants;
- timed unlimited access;
- RUB price;
- enabled state and display position.

Mini App and Telegram must not maintain separate package prices.

## Public visibility

A package is public only when all conditions are true:

- `is_enabled` is true;
- it grants at least one credit or a valid timed unlimited entitlement;
- it is not a technical regression package;
- `price_rub` is greater than zero.

## Mini App refresh contract

`GET /api/tma/app/packages` is served with:

- `Cache-Control: no-store, max-age=0`;
- `Pragma: no-cache`;
- `Expires: 0`.

The Mini App reloads the catalog:

- every time the package tab opens;
- when the browser/Mini App returns to the foreground;
- immediately before payment creation;
- when the user presses the refresh button.

If the selected package changed before payment, the first payment attempt is stopped. The user sees the new price and composition and confirms again.

## Payment integrity

The client submits only `package_id`. It never submits an amount.

The backend reloads the current package from the database, calculates the provider amount and stores an immutable package snapshot in the payment. Later admin edits do not change an already-created payment.

Arbitrary universal-credit purchases are disabled. A direct request containing `credits` is rejected even when it bypasses the Mini App UI.
