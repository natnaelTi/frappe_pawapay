from __future__ import annotations

from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

import frappe
from frappe import _

from frappe_pawapay.pawapay.countries import normalize_pawapay_country_code
from frappe_pawapay.pawapay.models import CHECKOUT_REDIRECT_HOSTS


def decimal_from(value) -> Decimal:
	try:
		return Decimal(str(value))
	except (InvalidOperation, TypeError):
		frappe.throw(_("Invalid amount for PawaPay transaction."))


def validate_amount_format(amount: Decimal, decimals_in_amount: str | None = None):
	if decimals_in_amount == "NONE" and amount != amount.to_integral_value():
		frappe.throw(_("This PawaPay provider does not support decimal amounts."))
	if decimals_in_amount == "TWO_PLACES" and amount.quantize(Decimal("0.01")) != amount:
		frappe.throw(_("This PawaPay provider supports at most two decimal places."))
	if amount <= 0:
		frappe.throw(_("PawaPay amount must be greater than zero."))


def validate_amount_against_active_configuration(settings, amount: Decimal, currency: str, country: str, operation_type: str = "DEPOSIT"):
	config = _load_active_configuration(settings)
	if not config:
		validate_amount_format(amount)
		return

	matches = list(_iter_country_currency_configs(config, country, currency, operation_type))
	if not matches:
		supported = ", ".join(_get_supported_country_currencies(config, country)) or _("no currencies")
		frappe.throw(
			_("PawaPay active configuration does not support {0} {1} in {2}. Supported: {3}.").format(
				operation_type, currency, country, supported
			)
		)

	for match in matches:
		decimals = match.get("decimalsInAmount") or match.get("decimals_in_amount")
		minimum = match.get("minimumAmount") or match.get("minAmount")
		maximum = match.get("maximumAmount") or match.get("maxAmount")
		try:
			validate_amount_format(amount, decimals)
			if minimum is not None and amount < Decimal(str(minimum)):
				continue
			if maximum is not None and amount > Decimal(str(maximum)):
				continue
			return
		except Exception:
			continue

	frappe.throw(_("PawaPay amount is outside the configured provider limits."))


def assert_allowed_redirect(url: str) -> str:
	parsed = urlparse(url or "")
	if parsed.scheme != "https" or parsed.hostname not in CHECKOUT_REDIRECT_HOSTS:
		frappe.throw(_("PawaPay returned an unsafe redirect URL."))
	return url


def mask_msisdn(msisdn: str | None) -> str:
	if not msisdn:
		return ""
	digits = "".join(ch for ch in str(msisdn) if ch.isdigit())
	if len(digits) <= 6:
		return "*" * len(digits)
	return f"{digits[:3]}{'*' * (len(digits) - 6)}{digits[-3:]}"


def _load_active_configuration(settings):
	if not settings.get("active_configuration_cache"):
		return None
	try:
		return frappe.parse_json(settings.active_configuration_cache)
	except Exception:
		return None


def get_deposit_amount_constraints(settings, country: str, currency: str) -> list[dict]:
	return get_operation_amount_constraints(settings, country, currency, "DEPOSIT")


def get_operation_amount_constraints(settings, country: str, currency: str, operation_type: str) -> list[dict]:
	config = _load_active_configuration(settings)
	if not config:
		return []
	return list(_iter_country_currency_configs(config, country, currency, operation_type))


def get_deposit_country_options(settings) -> list[dict]:
	config = _load_active_configuration(settings)
	if not config:
		return [
			{
				"country": settings.default_country,
				"country_name": settings.default_country,
				"currencies": [{"currency": settings.currency, "display_name": settings.currency, "providers": []}],
			}
		]

	options = []
	for country_config in config.get("countries", []):
		currencies = {}
		for provider in country_config.get("providers", []):
			for currency_config in provider.get("currencies", []):
				deposit_config = (currency_config.get("operationTypes") or {}).get("DEPOSIT") or {}
				if deposit_config.get("status") != "OPERATIONAL":
					continue
				currency = currency_config.get("currency")
				if not currency:
					continue
				currency_option = currencies.setdefault(
					currency,
					{
						"currency": currency,
						"display_name": currency_config.get("displayName") or currency,
						"providers": [],
						"decimals_in_amount": deposit_config.get("decimalsInAmount"),
						"minimum_amount": deposit_config.get("minAmount"),
						"maximum_amount": deposit_config.get("maxAmount"),
					},
				)
				currency_option["providers"].append(
					{
						"provider": provider.get("provider"),
						"display_name": provider.get("displayName") or provider.get("provider"),
					}
				)

		if currencies:
			display_name = country_config.get("displayName") or {}
			options.append(
				{
					"country": country_config.get("country"),
					"country_name": display_name.get("en") or country_config.get("country"),
					"prefix": country_config.get("prefix"),
					"flag": country_config.get("flag"),
					"currencies": sorted(currencies.values(), key=lambda row: row["currency"]),
				}
			)
	return sorted(options, key=lambda row: row["country_name"])


def get_first_deposit_currency(settings, country: str) -> str | None:
	for option in get_deposit_country_options(settings):
		if option.get("country") == country and option.get("currencies"):
			return option["currencies"][0].get("currency")
	return None


def validate_completed_provider_payload(transaction, payload: dict, source: str, mark_conflict: bool = True):
	if not payload:
		_raise_payload_mismatch(transaction, _("PawaPay {0} payload is empty.").format(source), mark_conflict)

	operation = _extract_payload_operation(payload)
	if operation and operation != transaction.operation_type:
		_raise_payload_mismatch(
			transaction,
			_("PawaPay {0} operation mismatch. Expected {1}, received {2}.").format(
				source, transaction.operation_type, operation
			),
			mark_conflict,
		)

	reference = _extract_payload_reference(payload)
	if reference and reference != transaction.client_reference_id:
		_raise_payload_mismatch(
			transaction,
			_("PawaPay {0} reference mismatch. Expected {1}, received {2}.").format(
				source, transaction.client_reference_id, reference
			),
			mark_conflict,
		)

	amount = _extract_payload_amount(payload)
	if amount is None:
		_raise_payload_mismatch(transaction, _("PawaPay {0} payload did not include a completed amount.").format(source), mark_conflict)
	if decimal_from(amount) != decimal_from(transaction.amount):
		_raise_payload_mismatch(
			transaction,
			_("PawaPay {0} amount mismatch. Expected {1}, received {2}.").format(
				source, transaction.amount, amount
			),
			mark_conflict,
		)

	currency = _extract_payload_currency(payload)
	if not currency:
		_raise_payload_mismatch(transaction, _("PawaPay {0} payload did not include a currency.").format(source), mark_conflict)
	if currency.upper() != (transaction.currency or "").upper():
		_raise_payload_mismatch(
			transaction,
			_("PawaPay {0} currency mismatch. Expected {1}, received {2}.").format(
				source, transaction.currency, currency
			),
			mark_conflict,
		)

	country = normalize_pawapay_country_code(_extract_payload_country(payload))
	if not country:
		_raise_payload_mismatch(transaction, _("PawaPay {0} payload did not include a supported country.").format(source), mark_conflict)
	if country != (transaction.country or "").upper():
		_raise_payload_mismatch(
			transaction,
			_("PawaPay {0} country mismatch. Expected {1}, received {2}.").format(
				source, transaction.country, country
			),
			mark_conflict,
		)


def mark_transaction_payload_conflict(transaction, message: str):
	transaction.db_set(
		{
			"review_status": "NEEDS_ATTENTION",
			"accounting_status": "CONFLICT",
			"last_error": message[:1000],
		}
	)


def _raise_payload_mismatch(transaction, message: str, mark_conflict: bool):
	if mark_conflict:
		mark_transaction_payload_conflict(transaction, message)
	frappe.throw(message)


def _extract_payload_operation(payload: dict) -> str | None:
	for candidate in _payload_candidates(payload):
		if candidate.get("checkoutId"):
			return "Checkout"
		if candidate.get("depositId"):
			return "Deposit"
		if candidate.get("payoutId"):
			return "Payout"
		if candidate.get("refundId"):
			return "Refund"
	return None


def _extract_payload_reference(payload: dict) -> str | None:
	for candidate in _payload_candidates(payload):
		if candidate.get("clientReferenceId"):
			return candidate.get("clientReferenceId")
		metadata = candidate.get("metadata") or []
		if isinstance(metadata, dict):
			metadata = [metadata]
		for row in metadata:
			if isinstance(row, dict) and row.get("paymentRequest"):
				return row.get("paymentRequest")
	return None


def _extract_payload_amount(payload: dict):
	for candidate in _payload_candidates(payload):
		for key in ("amount", "requestedAmount", "depositedAmount", "collectedAmount"):
			value = candidate.get(key)
			if isinstance(value, dict):
				if value.get("amount") is not None:
					return value.get("amount")
			elif value is not None:
				return value
		amount_row = _matching_amount_row(candidate)
		if amount_row:
			return amount_row.get("amount")
	return None


def _extract_payload_currency(payload: dict) -> str | None:
	for candidate in _payload_candidates(payload):
		for key in ("currency", "requestedCurrency", "depositedCurrency", "collectedCurrency"):
			if candidate.get(key):
				return candidate.get(key)
		for key in ("amount", "requestedAmount", "depositedAmount", "collectedAmount"):
			value = candidate.get(key)
			if isinstance(value, dict) and value.get("currency"):
				return value.get("currency")
		amount_row = _matching_amount_row(candidate)
		if amount_row and amount_row.get("currency"):
			return amount_row.get("currency")
	return None


def _extract_payload_country(payload: dict) -> str | None:
	for candidate in _payload_candidates(payload):
		for key in ("country", "countryCode"):
			if candidate.get(key):
				return candidate.get(key)
		for key in ("amount", "requestedAmount", "depositedAmount", "collectedAmount"):
			value = candidate.get(key)
			if isinstance(value, dict) and value.get("country"):
				return value.get("country")
		amount_row = _matching_amount_row(candidate)
		if amount_row and amount_row.get("country"):
			return amount_row.get("country")
	return None


def _matching_amount_row(payload: dict) -> dict | None:
	amounts = payload.get("amounts") or []
	if not amounts:
		return None
	if len(amounts) == 1:
		return amounts[0]
	return None


def _payload_candidates(payload: dict):
	yield payload
	for key in ("checkout", "deposit", "payout", "refund", "data"):
		if isinstance(payload.get(key), dict):
			yield payload.get(key)


def _iter_country_currency_configs(config, country: str, currency: str, operation_type: str = "DEPOSIT"):
	for country_config in config.get("countries", []):
		if country_config.get("country") != country:
			continue
		for provider in country_config.get("providers", []):
				for currency_config in provider.get("currencies", []):
					if currency_config.get("currency") != currency:
						continue
					operation_config = (currency_config.get("operationTypes") or {}).get(operation_type)
					if operation_config and operation_config.get("status") in {"OPERATIONAL", "DELAYED"}:
						yield operation_config


def _get_supported_country_currencies(config, country: str) -> list[str]:
	currencies = set()
	for country_config in config.get("countries", []):
		if country_config.get("country") != country:
			continue
		for provider in country_config.get("providers", []):
			for currency_config in provider.get("currencies", []):
				currency = currency_config.get("currency")
				if currency:
					currencies.add(currency)
	return sorted(currencies)
