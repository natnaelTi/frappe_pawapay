# PawaPay Production Readiness Checklist

Use this checklist before enabling live PawaPay payments, payouts, or refunds on a production ERPNext site.

## 1. Site and Network

- Confirm the ERPNext site has a public HTTPS URL with a trusted certificate.
- Confirm the callback endpoint is reachable from the public internet:
  - `https://<your-site>/api/method/frappe_pawapay.api.pawapay_callback`
- Configure the same callback endpoint in the PawaPay Dashboard for Checkouts, Deposits, and Refunds unless there is a deliberate reason to split them.
- Keep the callback endpoint centralized in this app. Other apps should start payments through this app, not register separate PawaPay callbacks.
- Enforce PawaPay callback source allowlisting at the reverse proxy or firewall when PawaPay provides production source ranges.
- Confirm background workers and scheduler are running.

## 2. PawaPay Settings

- New installs create a disabled placeholder `PawaPay Settings` record plus country/currency collection routes for the first available company.
- Review the auto-created wallet/clearing accounts before enabling the placeholder record.
- Create additional `PawaPay Settings` records if you need separate companies or operational wallet/account configurations.
- Set `Environment` to `Production` only when using a production API token.
- Store the PawaPay API token in the `API Token` password field. Do not commit it to code, fixtures, screenshots, or documentation.
- Set `Enabled` only after connection tests, signing, collection accounts, and accounting checks pass.
- Confirm `Callback URL` and `Statement Callback URL` show the production site URL, not a demo or local URL.
- Use `Test Connection` and confirm PawaPay returns active configuration successfully.

## 3. Signing Keys

- Enable signed callbacks in PawaPay and in `PawaPay Settings`.
- For production, keep `Require Signed Callbacks` enabled.
- Store outgoing request signing private keys outside DocTypes, ideally under the site private directory with restricted permissions.
- Put only the key identifier/path/reference in settings, not the full private key body.
- Upload/register the public key in PawaPay Dashboard if outgoing signed requests are required for the account.
- Rotate signing keys with a planned overlap window: add new key, confirm callbacks/requests, then retire the old key.

## 4. Countries, Currencies, and Collection Accounts

- Use the `Collection Accounts` child table for every live country/currency wallet route.
- Select `Country` from the linked ERPNext `Country` field. Do not type country names free-form.
- Confirm the generated PawaPay country codes map as expected:
  - Benin: `BEN`
  - Cameroon: `CMR`
  - Democratic Republic of the Congo: `COD`
  - Gabon: `GAB`
  - Ivory Coast: `CIV`
  - Kenya: `KEN`
  - Republic of the Congo: `COG`
  - Rwanda: `RWA`
  - Senegal: `SEN`
  - Sierra Leone: `SLE`
  - Uganda: `UGA`
  - Zambia: `ZMB`
- Confirm each collection account currency matches the row currency.
- Confirm the fallback `PawaPay Wallet Account`, `Currency`, and `Mode of Payment` are correct for the default route.
- Keep separate ERPNext clearing/bank accounts when finance needs country or wallet-level reconciliation.

## 5. FX and Multi-Currency

- Enable source currency conversion only when the business accepts payment in a local collection currency for a source document in another currency.
- Prefer `Reference Document` first, then `Currency Exchange`/Frankfurter fallback.
- Confirm Frankfurter can fetch the required currency pair before go-live.
- Review `PawaPay FX Rate Log` after test transactions. It should show source URL, date, rate, status, and linked `Currency Exchange`.
- Confirm rounding is acceptable for each local currency and PawaPay active configuration.
- Make sure customer-facing copy explains the collected local-currency amount before redirecting to PawaPay.

## 6. Inbound Checkout/Deposit Flow

- Create a small production test invoice or controlled live test document.
- Generate a Payment Request using the PawaPay gateway.
- Open the `/pawapay/pay/<token>` link in a private browser session.
- Confirm country/provider selection, amount, currency, and customer reference are correct.
- Complete payment using an approved test/live method.
- Confirm the `PawaPay Transaction` reaches `COMPLETED`.
- Confirm exactly one submitted `Payment Entry` is created.
- Confirm the Payment Request and source document are paid/allocated as expected.

## 7. Payouts and Refunds

- Keep payout/refund remote initiation permission-limited to Accounts Manager/System Manager.
- Require business approval before submitting a `PawaPay Payout Request`.
- Confirm each payout has:
  - party type and party,
  - source document,
  - wallet number,
  - provider,
  - authorization record,
  - correct country/currency collection account.
- Confirm each refund has:
  - completed original checkout/deposit transaction,
  - supported accounting reference,
  - refundable balance,
  - partial/full amount policy reviewed.
- Test payout/refund initiation, callback/reconciliation, and outbound Payment Entry posting in sandbox before live use.

## 8. Reconciliation Schedule

- Confirm scheduler events are enabled.
- The app polls pending transactions every 5 minutes through `reconcile_pending_transactions`.
- The app retries received webhook events every 5 minutes through `retry_received_webhooks`.
- Active configuration and wallet balances refresh hourly.
- Monitor stuck transactions where:
  - lifecycle is `PROCESSING`, `IN_RECONCILIATION`, `ACCEPTED`, or `ENQUEUED`,
  - accounting status is `FAILED` or `CONFLICT`,
  - review status is `NEEDS_ATTENTION`.

## 9. Operator Observability and Retry Controls

- Review `PawaPay Audit Event` daily during launch.
- Filter audit events by `Severity`, `Retryable`, `Operation Type`, `Transaction`, and `Error Log`.
- Use `Retry Processing` from `PawaPay Webhook Event` only for linked, non-invalid-signature callbacks.
- Use `Retry Accounting` from `PawaPay Transaction` only after resolving the cause of `FAILED` or `CONFLICT`.
- Use `Check Status` from `PawaPay Transaction` to force reconciliation for pending or uncertain remote states.
- Use `Resend Callback` for PawaPay payout/refund transactions only when a callback is missing and PawaPay supports resend for that operation.
- Use `Cancel Enqueued` only for payout/refund transactions that are still `ENQUEUED`.

## 10. AMS Subscription Billing

- Install both `payments` and `frappe_pawapay` on the AMS site before configuring billing.
- In `AMS Settings`, select `PawaPay` as the default billing provider and select the intended PawaPay Payment Gateway Account.
- Keep the AMS Pro price in its advertised source currency. If the company or PawaPay wallet collects another currency:
  - enable source-currency conversion in PawaPay Settings,
  - configure a leaf ERPNext Receivable account in the source invoice currency,
  - verify the exchange-rate source and customer-facing converted amount.
- Confirm the AMS flow creates one linked chain: `Billing Checkout Session` -> `Sales Invoice` -> `Payment Request` -> `PawaPay Payment Link`.
- Confirm abandoned links receive the billing-session expiry. The hourly AMS cleanup must expire untouched links, cancel their Payment Request and unpaid Sales Invoice, and restore a Free account from `Pending Payment` to `Active`.
- Do not expire or cancel a checkout while its PawaPay transaction is in flight or completed awaiting accounting reconciliation.
- Confirm Pro access remains disabled until all of the following are true:
  - the PawaPay transaction is `COMPLETED`,
  - accounting status is `POSTED`,
  - a submitted Payment Entry exists,
  - source amount, currency, Payment Request, and Sales Invoice match the AMS billing session.
- Treat PawaPay checkout as a one-time monthly collection. AMS grants one paid month and requests another checkout for renewal; it must not be represented as an automatic recurring mobile-money mandate.
- Complete one approved low-value payment end to end before production launch and verify entitlement activation, Payment Entry allocation, audit events, renewal date, and idempotent callback replay.

## 11. Go-Live Approval

- Unit and integration tests pass on the production app version.
- `bench --site <site> migrate` has run successfully.
- Gunicorn/workers have been restarted or reloaded after deployment.
- Finance has approved accounts, modes of payment, exchange-rate behavior, and reconciliation reports.
- Security has approved API token handling, signing key storage, callback verification, and network allowlisting.
- Operations has reviewed retry controls and the `PawaPay Audit Event` workflow.
