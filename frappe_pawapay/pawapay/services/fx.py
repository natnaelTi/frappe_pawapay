from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

import frappe
from frappe import _

from frappe_pawapay.compatibility import get_payment_request_source_amount
from frappe_pawapay.pawapay.services.frankfurter import get_or_fetch_exchange_rate
from frappe_pawapay.pawapay.validation import decimal_from, get_deposit_amount_constraints


@dataclass(frozen=True)
class PawaPayAmountPlan:
	source_amount: Decimal
	source_currency: str
	collection_amount: Decimal
	collection_currency: str
	exchange_rate: Decimal
	fx_source: str
	rounding_adjustment: Decimal
	fx_rate_date: str | None = None
	fx_source_url: str | None = None
	currency_exchange: str | None = None

	@property
	def converted(self) -> bool:
		return self.source_currency != self.collection_currency


def get_checkout_amount_plan(settings, payment_request, collection_country: str | None = None, collection_currency: str | None = None) -> PawaPayAmountPlan:
	source_amount = decimal_from(get_payment_request_source_amount(payment_request))
	source_currency = payment_request.currency
	collection_country = collection_country or settings.default_country
	collection_currency = collection_currency or settings.currency

	if source_currency == collection_currency:
		return PawaPayAmountPlan(
			source_amount=source_amount,
			source_currency=source_currency,
			collection_amount=source_amount,
			collection_currency=collection_currency,
			exchange_rate=Decimal("1"),
			fx_source="Same Currency",
			rounding_adjustment=Decimal("0"),
		)

	if not settings.allow_source_currency_conversion:
		frappe.throw(
			_(
				"PawaPay settings collect in {0}, but this Payment Request is in {1}. "
				"Enable source currency conversion or create a {0} Payment Request."
			).format(collection_currency, source_currency)
		)

	exchange_rate, fx_source, fx_rate_date, fx_source_url, currency_exchange = _get_exchange_rate(
		settings, payment_request, source_currency, collection_currency
	)
	raw_collection_amount = source_amount * exchange_rate
	collection_amount = _round_collection_amount(settings, raw_collection_amount, collection_country, collection_currency)
	return PawaPayAmountPlan(
		source_amount=source_amount,
		source_currency=source_currency,
		collection_amount=collection_amount,
		collection_currency=collection_currency,
		exchange_rate=exchange_rate,
		fx_source=fx_source,
		rounding_adjustment=collection_amount - raw_collection_amount,
		fx_rate_date=fx_rate_date,
		fx_source_url=fx_source_url,
		currency_exchange=currency_exchange,
	)


def _get_exchange_rate(settings, payment_request, source_currency: str, collection_currency: str):
	source = settings.fx_rate_source or "Reference Document"
	if source == "Manual":
		if not settings.manual_exchange_rate:
			frappe.throw(_("Manual exchange rate is required for PawaPay currency conversion."))
		return Decimal(str(settings.manual_exchange_rate)), "Manual", None, None, None

	if source == "Reference Document":
		rate = _get_reference_document_rate(payment_request, source_currency, collection_currency)
		if rate:
			return rate, "Reference Document", None, None, None

	if source in {"Reference Document", "Currency Exchange", "Frankfurter"}:
		rate_record = get_or_fetch_exchange_rate(settings, payment_request, source_currency, collection_currency)
		if rate_record:
			return (
				rate_record.rate,
				rate_record.source,
				rate_record.rate_date,
				rate_record.source_url,
				rate_record.currency_exchange,
			)

	frappe.throw(
		_("No exchange rate found for {0} to {1}. Create a Currency Exchange record or set a manual rate.").format(source_currency, collection_currency)
	)


def _get_reference_document_rate(payment_request, source_currency: str, collection_currency: str):
	if not (payment_request.reference_doctype and payment_request.reference_name):
		return None
	ref_doc = frappe.get_doc(payment_request.reference_doctype, payment_request.reference_name)
	company_currency = ref_doc.get("company_currency") or frappe.get_cached_value(
		"Company", payment_request.company, "default_currency"
	)
	if ref_doc.get("currency") == source_currency and company_currency == collection_currency:
		rate = ref_doc.get("conversion_rate") or ref_doc.get("exchange_rate")
		if rate:
			return Decimal(str(rate))
	return None


def _round_collection_amount(settings, amount: Decimal, collection_country: str, collection_currency: str):
	decimals = _get_collection_decimals(settings, collection_country, collection_currency)
	if decimals == "NONE":
		return amount.quantize(Decimal("1"), rounding=ROUND_HALF_UP)
	if decimals == "TWO_PLACES":
		return amount.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
	return amount


def _get_collection_decimals(settings, collection_country: str, collection_currency: str):
	matches = get_deposit_amount_constraints(settings, collection_country, collection_currency)
	for match in matches:
		if match.get("decimalsInAmount") or match.get("decimals_in_amount"):
			return match.get("decimalsInAmount") or match.get("decimals_in_amount")
	return None
