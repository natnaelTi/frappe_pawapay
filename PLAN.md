# PawaPay Frappe App Plan

## Local Version Findings

- Frappe: `version-16`, `v16.2.1`
- ERPNext: `version-16`, `v16.1.0-4-gbb2bada1fd`
- Payments: `develop`, `49b4f63`
- Installed `payments.utils.create_payment_gateway(gateway, settings=None, controller=None)` supports a gateway settings DocType and settings document name.
- Installed `Payment Request.get_payment_url()` uses the legacy gateway-controller lookup.
- Installed `Payment Request.create_payment_entry(submit=True)` supports `submit=False` and allocates `Payment Entry Reference.payment_request`.

## Architecture

- Standalone app: `frappe_pawapay`, display name `PawaPay Payments`.
- No modifications to Frappe, ERPNext, or payments.
- Default inbound flow is PawaPay Checkout through a stable opaque `/pawapay/pay/<token>` URL.
- PawaPay UUIDs are persisted locally before initiation.
- Browser return URLs never post accounting.
- Callback and reconciliation paths update operational truth and enqueue accounting only for `COMPLETED`.

## Implemented Files

- App metadata: `pyproject.toml`, `frappe_pawapay/hooks.py`, `frappe_pawapay/install.py`
- Gateway/controller: `PawaPay Settings`
- Operational DocTypes: Settings, Payment Link, Transaction, Status Event, Webhook Event, Payout Request, Refund Request, Wallet Snapshot
- Client/services: `pawapay/client.py`, `services/checkout.py`, `services/accounting.py`, `services/reconciliation.py`, `callbacks.py`
- Public UI: `/pawapay/pay/<token>` and `/pawapay/return`

## Security Model

- API token is a Frappe `Password` field.
- PawaPay base URL is derived from Sandbox/Production, never user supplied.
- Redirect URLs are allowlisted to PawaPay hosts.
- Callback payloads are stored redacted with raw-body hashes and duplicate fingerprints.
- Production settings require HTTPS and signed callbacks.
- Payout/refund remote initiation remains disabled unless an explicit checkbox is set and service implementation is completed.

## Accounting Rules

- Only `COMPLETED` inbound transactions call the finalizer.
- Finalizer calls installed `Payment Request.create_payment_entry(submit=False)`, annotates provider reference/date/remarks, then submits.
- Already-paid requests are marked accounting `CONFLICT`.
- Accounting errors leave the transaction completed but accounting `FAILED` with `NEEDS_ATTENTION`.

## Remaining Certification Work

- OpenAPI contract test suite against current `openapi_v2.yaml`.
- Sandbox certification scenarios with PawaPay test numbers.
- Production-ready payout and refund remote initiation.
- Concurrency tests around duplicate callbacks and Payment Entry finalization. The finalizer now locks the transaction and Payment Request rows, but an automated concurrency suite is still required.

## Closed In Hardening Pass

- Required-signature callbacks are recorded but do not mutate transaction state or retry unless verified.
- Invalid JSON callbacks are durably recorded without crashing the receiver.
- PawaPay Settings and other secret-bearing DocTypes include System Manager permlevel-1 permissions.
- Inbound accounting finalization locks transaction and Payment Request rows.
- Inbound accounting finalization checks exact amount and currency before posting.
- Checkout initiation recovers uncertain and duplicate responses through a status lookup using the persisted UUID.
- Checkout amount validation uses cached active configuration where available.
- Outgoing financial request signing uses `http-message-signatures` with PawaPay's required components.
- Signed callbacks verify `Content-Digest`, RFC 9421 signature, expected covered components, signature time window, and cached PawaPay public keys.

## Verification Commands

- `python -m json.tool <doctype json>`
- `PYTHONPATH=apps/frappe_pawapay:apps/frappe:apps/erpnext:apps/payments python -m compileall apps/frappe_pawapay/frappe_pawapay`
- `bench --site <site> install-app frappe_pawapay`
- `bench --site <site> migrate`
