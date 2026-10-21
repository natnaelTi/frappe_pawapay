# PawaPay Payments

Standalone Frappe app for integrating PawaPay Merchant API v2 with ERPNext Payment Requests and Payment Entries.

The default inbound flow uses PawaPay Checkout:

1. ERPNext creates a Payment Request and receives a stable `/pawapay/pay/<token>` URL.
2. The customer opens the Selfmade-hosted page and explicitly starts payment.
3. The app persists a UUIDv4 checkout transaction before calling PawaPay.
4. PawaPay redirects the customer to hosted mobile-money checkout.
5. Signed callbacks and scheduled reconciliation provide the authoritative state.
6. ERPNext accounting is posted exactly once, and only for verified `COMPLETED` transactions.

## Setup

1. Install the app on a bench that already has Frappe, ERPNext, and the official `payments` app.
2. Create a `PawaPay Settings` record for each company/currency/account configuration.
3. Select Sandbox or Production. Tokens are stored in a Password field and are never exported as fixtures.
4. Select the ERPNext clearing/bank account representing the PawaPay wallet.
5. Save the settings. The app creates `Payment Gateway`, `Payment Gateway Account`, and `Mode of Payment` records.
6. Configure the PawaPay Dashboard callback URL shown on the settings document.

## Production Checklist

- Site URL uses HTTPS with a trusted certificate.
- Production PawaPay token is separate from sandbox and rotated through the dashboard.
- Signed callbacks are enabled and required.
- Callback source IP allowlisting is enforced at the reverse proxy or firewall.
- The PawaPay wallet account and Mode of Payment are reviewed by Accounts.
- Reconciliation scheduler is enabled.
- No production financial tests run automatically.

See [Production Readiness Checklist](docs/production_readiness_checklist.md) for the full go-live checklist covering callbacks, signing keys, collection accounts, FX, reconciliation, and operator retry controls.

The checklist also documents the supported AMS subscription integration. AMS creates the ERPNext billing documents and grants entitlement only after this app has verified the provider result and posted accounting.

## Host-App Checkout Branding

The hosted payment route remains owned by `frappe_pawapay`, while an installed host app can supply its own presentation through a hook:

```python
pawapay_payment_page_context = ["my_app.payments.payment_page_context"]
```

The hook receives the `Payment Request` and optional `PawaPay Payment Link`. Return an empty dictionary for payment requests the host app does not own. A branded response can provide:

- `is_branded_checkout`, `checkout_standalone`, and `checkout_theme`
- Local `/assets/...` paths for `checkout_stylesheet`, `checkout_favicon`, `checkout_logo`, and `checkout_image`
- Brand, heading, description, status, button, and provider-note text
- `checkout_home_url` and `checkout_return_url`
- Label/value `checkout_details` and short `checkout_assurances`
- Merchant/reference visibility and labels

PawaPay normalizes the response, rejects unsafe navigation and asset URLs, and retains the neutral default page when no host app claims the request. Host-specific CSS and imagery therefore remain in the integrating app rather than becoming a dependency of this gateway.

## Operations

- `PawaPay Transaction` is the operational source of remote payment truth.
- `Payment Entry` is posted only after a verified `COMPLETED` lifecycle state.
- Failed, expired, cancelled, uncertain, processing, and in-reconciliation states do not post successful accounting.
- Completed remote transactions that cannot post accounting remain visible as accounting pending/failed and can be retried safely.

## Outbound Scope

This app includes approval-style Payout and Refund request DocTypes plus guarded service entry points. Production outbound money movement should be enabled only after sandbox certification and explicit workflow approval.
