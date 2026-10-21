from __future__ import annotations

import frappe
from frappe import _

from frappe_pawapay.compatibility import (
	create_payment_gateway,
	ensure_collection_gateway_accounts,
	ensure_mode_of_payment,
)
from frappe_pawapay.pawapay.countries import get_country_from_pawapay_code

PLACEHOLDER_SETTINGS_NAME = "PawaPay Auto Setup"
PLACEHOLDER_API_TOKEN = "PAWAPAY_API_TOKEN_PLACEHOLDER"

PAWAPAY_COLLECTION_ROUTES = [
	{"country_code": "BEN", "currency": "XOF", "label": "Benin"},
	{"country_code": "CMR", "currency": "XAF", "label": "Cameroon"},
	{"country_code": "COD", "currency": "CDF", "label": "Democratic Republic of the Congo"},
	{"country_code": "GAB", "currency": "XAF", "label": "Gabon"},
	{"country_code": "CIV", "currency": "XOF", "label": "Ivory Coast"},
	{"country_code": "KEN", "currency": "KES", "label": "Kenya"},
	{"country_code": "COG", "currency": "XAF", "label": "Republic of the Congo"},
	{"country_code": "RWA", "currency": "RWF", "label": "Rwanda"},
	{"country_code": "SEN", "currency": "XOF", "label": "Senegal"},
	{"country_code": "SLE", "currency": "SLE", "label": "Sierra Leone"},
	{"country_code": "UGA", "currency": "UGX", "label": "Uganda"},
	{"country_code": "ZMB", "currency": "ZMW", "label": "Zambia"},
]


def bootstrap_default_configuration():
	company = _get_default_company()
	if not company:
		return None

	_ensure_currencies()
	accounts = _ensure_wallet_accounts(company)
	settings = _ensure_placeholder_settings(company, accounts)
	create_payment_gateway(settings.gateway, "PawaPay Settings", settings.name)
	ensure_collection_gateway_accounts(settings)
	return settings.name


@frappe.whitelist()
def get_onboarding_context():
	frappe.only_for(("System Manager",))
	settings_name = frappe.db.exists("PawaPay Settings", PLACEHOLDER_SETTINGS_NAME)
	if not settings_name:
		return {"show": False}

	settings = frappe.get_doc("PawaPay Settings", settings_name)
	if settings.enabled or settings.get_password("api_token") != PLACEHOLDER_API_TOKEN:
		return {"show": False}

	return {
		"show": True,
		"settings": settings.name,
		"company": settings.company,
		"environment": settings.environment,
		"default_country": settings.default_country,
		"default_language": settings.default_language,
		"allow_source_currency_conversion": settings.allow_source_currency_conversion,
		"fx_rate_source": settings.fx_rate_source,
		"signing_enabled": settings.signing_enabled,
		"signing_key_id": settings.signing_key_id,
		"signing_algorithm": settings.signing_algorithm,
		"private_key_reference": settings.private_key_reference,
		"require_signed_callbacks": settings.require_signed_callbacks,
		"callback_url": settings.callback_url,
		"collection_account_count": len(settings.get("collection_accounts") or []),
	}


@frappe.whitelist()
def configure_placeholder_settings(settings_name: str, values: str | dict):
	frappe.only_for(("System Manager",))
	if settings_name != PLACEHOLDER_SETTINGS_NAME:
		frappe.throw(_("Only the auto-created placeholder PawaPay Settings record can be configured here."))

	values = frappe.parse_json(values)
	settings = frappe.get_doc("PawaPay Settings", settings_name)
	allowed_fields = {
		"environment",
		"api_token",
		"default_country",
		"default_language",
		"allow_source_currency_conversion",
		"fx_rate_source",
		"manual_exchange_rate",
		"signing_enabled",
		"signing_key_id",
		"signing_algorithm",
		"private_key_reference",
		"require_signed_callbacks",
		"enabled",
	}
	for fieldname in allowed_fields:
		if fieldname in values:
			settings.set(fieldname, values[fieldname])

	settings.save(ignore_permissions=True)
	return {"ok": True, "settings": settings.name}


def _get_default_company():
	return (
		frappe.defaults.get_global_default("company")
		or frappe.db.get_single_value("Global Defaults", "default_company")
		or frappe.db.get_value("Company", {}, "name", order_by="creation asc")
	)


def _ensure_currencies():
	for route in PAWAPAY_COLLECTION_ROUTES:
		currency = route["currency"]
		if not frappe.db.exists("Currency", currency):
			frappe.get_doc({"doctype": "Currency", "currency_name": currency, "enabled": 1}).insert(
				ignore_permissions=True
			)


def _ensure_wallet_accounts(company: str):
	abbr = frappe.get_cached_value("Company", company, "abbr")
	parent_account = _get_wallet_parent_account(company)
	accounts = {}
	for route in PAWAPAY_COLLECTION_ROUTES:
		account_name = f"PawaPay Wallet {route['country_code']} {route['currency']}"
		existing = frappe.db.get_value("Account", {"company": company, "account_name": account_name}, "name")
		if existing:
			accounts[route["country_code"]] = existing
			continue

		doc = frappe.get_doc(
			{
				"doctype": "Account",
				"account_name": account_name,
				"company": company,
				"parent_account": parent_account,
				"is_group": 0,
				"account_type": "Bank",
				"account_currency": route["currency"],
			}
		)
		doc.insert(ignore_permissions=True)
		accounts[route["country_code"]] = doc.name or f"{account_name} - {abbr}"
	return accounts


def _get_wallet_parent_account(company: str):
	parent = frappe.db.get_value("Account", {"company": company, "account_type": "Bank", "is_group": 1}, "name")
	if parent:
		return parent
	parent = frappe.db.get_value(
		"Account",
		{"company": company, "root_type": "Asset", "is_group": 1},
		"name",
		order_by="lft desc",
	)
	if parent:
		return parent
	frappe.throw(_("No asset group account exists for company {0}.").format(company))


def _ensure_placeholder_settings(company: str, accounts: dict[str, str]):
	if frappe.db.exists("PawaPay Settings", PLACEHOLDER_SETTINGS_NAME):
		settings = frappe.get_doc("PawaPay Settings", PLACEHOLDER_SETTINGS_NAME)
	else:
		settings = frappe.new_doc("PawaPay Settings")
		settings.gateway_name = PLACEHOLDER_SETTINGS_NAME

	existing_api_token = settings.get_password("api_token", raise_exception=False) if settings.name else None
	settings.enabled = 0
	settings.company = company
	settings.environment = settings.environment or "Sandbox"
	settings.api_token = existing_api_token or PLACEHOLDER_API_TOKEN
	settings.currency = "RWF"
	settings.pawapay_wallet_account = accounts.get("RWA")
	settings.mode_of_payment = settings.mode_of_payment or "PawaPay-RWA-RWF"
	settings.default_country = settings.default_country or "RWA"
	settings.default_language = settings.default_language or "en"
	settings.default_checkout_expiry = settings.default_checkout_expiry or 15
	settings.checkout_return_method = settings.checkout_return_method or "INSTANT"
	settings.connection_timeout = settings.connection_timeout or 10
	settings.read_timeout = settings.read_timeout or 30
	settings.reconciliation_enabled = 1
	settings.reconciliation_batch_size = settings.reconciliation_batch_size or 25
	settings.active_configuration_cache_ttl = settings.active_configuration_cache_ttl or 3600
	settings.public_key_cache_ttl = settings.public_key_cache_ttl or 900

	_existing_routes = {row.pawapay_country_code for row in settings.get("collection_accounts") or []}
	for route in PAWAPAY_COLLECTION_ROUTES:
		if route["country_code"] in _existing_routes:
			continue
		country = get_country_from_pawapay_code(route["country_code"])
		mode_of_payment = f"PawaPay-{route['country_code']}-{route['currency']}"
		ensure_mode_of_payment(mode_of_payment, company, accounts[route["country_code"]])
		settings.append(
			"collection_accounts",
			{
				"enabled": 1,
				"country": country,
				"pawapay_country_code": route["country_code"],
				"currency": route["currency"],
				"wallet_account": accounts[route["country_code"]],
				"mode_of_payment": mode_of_payment,
				"notes": f"Auto-created placeholder for {route['label']}.",
			},
		)

	settings.save(ignore_permissions=True)
	return settings
