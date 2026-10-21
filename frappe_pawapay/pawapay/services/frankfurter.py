from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from urllib.parse import urlencode

import frappe
import requests
from frappe import _


FRANKFURTER_BASE_URL = "https://api.frankfurter.dev"
FRANKFURTER_SOURCE = "Frankfurter v2 public exchange-rate API"


@dataclass(frozen=True)
class ExchangeRateResult:
	rate: Decimal
	rate_date: str
	source: str
	source_url: str
	currency_exchange: str | None = None


def get_or_fetch_exchange_rate(settings, payment_request, from_currency: str, to_currency: str) -> ExchangeRateResult | None:
	transaction_date = payment_request.get("transaction_date") or frappe.utils.nowdate()
	existing_name = _find_currency_exchange(from_currency, to_currency, transaction_date)
	if existing_name:
		existing = frappe.get_doc("Currency Exchange", existing_name)
		log = _get_success_log_for_currency_exchange(existing.name)
		return ExchangeRateResult(
			rate=Decimal(str(existing.exchange_rate)),
			rate_date=frappe.utils.getdate(existing.date).isoformat(),
			source=log.get("source_description") if log else "Currency Exchange",
			source_url=log.get("source_url") if log else frappe.utils.get_url_to_form("Currency Exchange", existing.name),
			currency_exchange=existing.name,
		)

	try:
		result = fetch_frankfurter_rate(from_currency, to_currency, transaction_date)
		currency_exchange = _create_currency_exchange(from_currency, to_currency, result.rate, result.rate_date)
		_log_rate(
			settings,
			payment_request,
			result,
			"Success",
			from_currency=from_currency,
			to_currency=to_currency,
			currency_exchange=currency_exchange,
		)
		return ExchangeRateResult(
			rate=result.rate,
			rate_date=result.rate_date,
			source=result.source,
			source_url=result.source_url,
			currency_exchange=currency_exchange,
		)
	except Exception as exc:
		_log_rate(settings, payment_request, None, "Failed", from_currency, to_currency, str(exc))
		return None


def fetch_frankfurter_rate(from_currency: str, to_currency: str, rate_date: str | None = None) -> ExchangeRateResult:
	from_currency = from_currency.upper()
	to_currency = to_currency.upper()
	path = f"/v2/rate/{from_currency}/{to_currency}"
	params = {"date": rate_date} if rate_date else {}
	source_url = f"{FRANKFURTER_BASE_URL}{path}"
	if params:
		source_url = f"{source_url}?{urlencode(params)}"

	response = requests.get(source_url, timeout=15)
	if response.status_code >= 400:
		raise frappe.ValidationError(
			_("Frankfurter could not provide {0} to {1}: HTTP {2}").format(
				from_currency, to_currency, response.status_code
			)
		)
	data = json.loads(response.text, parse_float=Decimal)
	rate = data.get("rate")
	if not rate:
		raise frappe.ValidationError(_("Frankfurter response did not include an exchange rate."))
	return ExchangeRateResult(
		rate=Decimal(str(rate)),
		rate_date=data.get("date") or rate_date or frappe.utils.nowdate(),
		source=FRANKFURTER_SOURCE,
		source_url=source_url,
	)


def _find_currency_exchange(from_currency: str, to_currency: str, transaction_date: str) -> str | None:
	return frappe.db.get_value(
		"Currency Exchange",
		{
			"from_currency": from_currency,
			"to_currency": to_currency,
			"date": ["<=", transaction_date],
			"for_selling": 1,
		},
		"name",
		order_by="date desc, creation desc",
	)


def _get_success_log_for_currency_exchange(currency_exchange: str):
	log_name = frappe.db.get_value(
		"PawaPay FX Rate Log",
		{"currency_exchange": currency_exchange, "status": "Success"},
		"name",
		order_by="creation desc",
	)
	if not log_name:
		return None
	return frappe.db.get_value(
		"PawaPay FX Rate Log",
		log_name,
		["source_description", "source_url"],
		as_dict=True,
	)


def _create_currency_exchange(from_currency: str, to_currency: str, rate: Decimal, rate_date: str) -> str:
	doc = frappe.get_doc(
		{
			"doctype": "Currency Exchange",
			"date": rate_date,
			"from_currency": from_currency,
			"to_currency": to_currency,
			"exchange_rate": rate,
			"for_buying": 1,
			"for_selling": 1,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc.name


def _log_rate(
	settings,
	payment_request,
	result: ExchangeRateResult | None,
	status: str,
	from_currency: str | None = None,
	to_currency: str | None = None,
	error: str | None = None,
	currency_exchange: str | None = None,
):
	try:
		log = frappe.get_doc(
			{
				"doctype": "PawaPay FX Rate Log",
				"settings": settings.name if settings else None,
				"provider": "Frankfurter",
				"status": status,
				"from_currency": from_currency or (payment_request.currency if payment_request else None),
				"to_currency": to_currency,
				"rate": result.rate if result else None,
				"rate_date": result.rate_date if result else None,
				"fetched_at": frappe.utils.now_datetime(),
				"source_url": result.source_url if result else None,
				"source_description": FRANKFURTER_SOURCE,
				"currency_exchange": currency_exchange,
				"reference_doctype": payment_request.reference_doctype if payment_request else None,
				"reference_name": payment_request.reference_name if payment_request else None,
				"payment_request": payment_request.name if payment_request else None,
				"error": (error or "")[:1000],
			}
		)
		log.insert(ignore_permissions=True)
	except Exception:
		frappe.log_error("Unable to create PawaPay FX Rate Log")
