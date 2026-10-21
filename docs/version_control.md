# Repository and Review Workflow

Repository: https://github.com/natnaelTi/frappe_pawapay

The initial import preserves the existing app as a stack of feature-scoped draft
pull requests. These are new import commits, not a reconstruction of historical
development dates.

| Order | Branch | Scope | Base |
| --- | --- | --- | --- |
| 1 | `feat/payment-platform` | Package scaffold, provider security, accounting/FX/outbound operations, checkout and audit controls | `main` |
| 2 | `feat/installer-onboarding` | Wallet/account seeding and first-run settings dialog | `feat/payment-platform` |
| 3 | `test/payment-integration-ci` | Unit tests, controlled accounting fixtures, ingestion/retry tests and source CI | `feat/installer-onboarding` |
| 4 | `docs/production-readiness` | Setup, host-app branding, architecture notes and production checklist | `test/payment-integration-ci` |

Review and merge in order. After each merge, retarget the next PR to `main` and
confirm its diff. Preserve the scoped commits when merging; squash merging a
prerequisite requires rebasing its dependent branches to avoid repeated diffs.
The final documentation branch contains the complete app snapshot.

## Validation

The initial import was checked with Python compilation, JavaScript syntax checks,
JSON/TOML parsing, 15 unit tests and 17 integration tests on the development site.
Provider calls are mocked in automated tests. This does not certify live provider
behavior, concurrent financial execution or a production deployment.

GitHub Actions performs source checks only. ERPNext integration tests need a
prepared bench, ERPNext/payments dependencies and an isolated test database; they
are not run by the source-check workflow.

## Ongoing Changes

Create a feature branch from the latest `main`, use scoped commits, and open a PR
with the concrete behavior change, checks actually run and deployment steps.
Keep API tokens, private keys, site configuration, generated assets and bytecode
outside version control. Deploy a reviewed commit only after the production
checklist and the appropriate provider certification have been completed.
