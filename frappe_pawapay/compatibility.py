from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import getdate

from frappe_pawapay.pawapay.validation import decimal_from

from erpnext.accounts.party import get_party_account

from frappe_pawapay.pawapay.countries import get_pawapay_country_code

PAWAPAY_PAYMENT_ENTRY_EXTERNAL_ID_FIELD = "custom_pawapay_external_id"
PAWAPAY_PAYMENT_ENTRY_TRANSACTION_FIELD = "custom_pawapay_transaction"


def get_payment_request_source_amount(payment_request):
	"""Return the unpaid amount in ``Payment Request.currency``.

	ERPNext stores ``outstanding_amount`` in company currency when a foreign-
	currency invoice posts to a company-currency receivable account. PawaPay's
	source amount must remain in the Payment Request currency so FX conversion
	and completed-payment validation use the same unit.
	"""
	amount = payment_request.outstanding_amount or payment_request.grand_total
	party_currency = payment_request.get("party_account_currency")
	company_currency = frappe.get_cached_value("Company", payment_request.company, "default_currency")
	if payment_request.currency == party_currency or party_currency != company_currency:
		return amount

	if not (payment_request.reference_doctype and payment_request.reference_name):
		return payment_request.grand_total
	ref_doc = frappe.get_doc(payment_request.reference_doctype, payment_request.reference_name)
	conversion_rate = ref_doc.get("conversion_rate") or ref_doc.get("exchange_rate")
	if not conversion_rate:
		return payment_request.grand_total
	return decimal_from(amount) / decimal_from(conversion_rate)


def get_payment_gateway_controller(payment_gateway: str):
	from payments.utils import get_payment_gateway_controller

	return get_payment_gateway_controller(payment_gateway)


def create_payment_gateway(gateway: str, settings_doctype: str, settings_name: str):
	from payments.utils import create_payment_gateway

	create_payment_gateway(gateway, settings=settings_doctype, controller=settings_name)


def ensure_gateway_account(gateway: str, company: str, currency: str, payment_account: str):
	if frappe.db.exists(
		"Payment Gateway Account",
		{"payment_gateway": gateway, "company": company, "currency": currency},
	):
		frappe.db.set_value(
			"Payment Gateway Account",
			{"payment_gateway": gateway, "company": company, "currency": currency},
			"payment_account",
			payment_account,
		)
		return

	frappe.get_doc(
		{
			"doctype": "Payment Gateway Account",
			"is_default": 0,
			"payment_gateway": gateway,
			"payment_account": payment_account,
			"currency": currency,
			"payment_channel": "Email",
			"company": company,
		}
	).insert(ignore_permissions=True, ignore_if_duplicate=True)


def ensure_mode_of_payment(name: str, company: str, payment_account: str):
	if not frappe.db.exists("Mode of Payment", name):
		doc = frappe.get_doc(
			{
				"doctype": "Mode of Payment",
				"mode_of_payment": name,
				"type": "Bank",
				"accounts": [{"company": company, "default_account": payment_account}],
			}
		)
		doc.insert(ignore_permissions=True)
		return

	doc = frappe.get_doc("Mode of Payment", name)
	for row in doc.accounts:
		if row.company == company:
			row.default_account = payment_account
			doc.save(ignore_permissions=True)
			return
	doc.append("accounts", {"company": company, "default_account": payment_account})
	doc.save(ignore_permissions=True)


def get_collection_account(settings, country: str | None, currency: str):
	country = (country or "").upper()
	currency = (currency or "").upper()
	for row in _iter_collection_account_candidates(settings, country, currency):
		mode_of_payment = _collection_row_mode_of_payment(settings, row)
		return {
			"wallet_account": row.wallet_account,
			"mode_of_payment": mode_of_payment,
			"currency": row.currency,
			"country": row.pawapay_country_code,
			"source": "Collection Account",
		}

	if settings.currency == currency and settings.pawapay_wallet_account:
		return {
			"wallet_account": settings.pawapay_wallet_account,
			"mode_of_payment": settings.mode_of_payment or _default_collection_mode_of_payment(settings, currency),
			"currency": currency,
			"country": country,
			"source": "Fallback Settings",
		}

	frappe.throw(
		_(
			"No PawaPay collection account is configured for {0} in {1}. "
			"Add a Collection Accounts row on PawaPay Settings."
		).format(currency, country or _("the selected country"))
	)


def validate_collection_accounts(settings):
	seen = set()
	for row in settings.get("collection_accounts") or []:
		if not row.enabled:
			continue
		row.pawapay_country_code = get_pawapay_country_code(row.country) if row.country else None
		row.currency = (row.currency or "").upper()
		key = (row.pawapay_country_code or "*", row.currency)
		if key in seen:
			frappe.throw(
				_("Duplicate PawaPay collection account mapping for {0}/{1}.").format(
					row.pawapay_country_code or "*", row.currency
				)
			)
		seen.add(key)
		_validate_collection_account_currency(row.wallet_account, row.currency)

	if settings.pawapay_wallet_account and settings.currency:
		_validate_collection_account_currency(settings.pawapay_wallet_account, settings.currency)


def ensure_collection_gateway_accounts(settings):
	for row in settings.get("collection_accounts") or []:
		if not row.enabled:
			continue
		mode_of_payment = _collection_row_mode_of_payment(settings, row)
		ensure_gateway_account(settings.gateway, settings.company, row.currency, row.wallet_account)
		ensure_mode_of_payment(mode_of_payment, settings.company, row.wallet_account)

	if settings.currency and settings.pawapay_wallet_account:
		mode_of_payment = settings.mode_of_payment or _default_collection_mode_of_payment(settings, settings.currency)
		ensure_gateway_account(settings.gateway, settings.company, settings.currency, settings.pawapay_wallet_account)
		ensure_mode_of_payment(mode_of_payment, settings.company, settings.pawapay_wallet_account)


def create_payment_entry_from_request(payment_request_name: str, transaction):
	payment_request = frappe.get_doc("Payment Request", payment_request_name)
	if payment_request.payment_request_type != "Inward":
		frappe.throw(_("PawaPay can only finalize inward Payment Requests."))

	payment_entry = payment_request.create_payment_entry(submit=False)
	settings = frappe.get_doc("PawaPay Settings", transaction.settings)
	account_config = _get_transaction_collection_account(settings, transaction)

	payment_entry.mode_of_payment = account_config["mode_of_payment"]
	if account_config["wallet_account"]:
		if payment_entry.payment_type == "Receive":
			payment_entry.paid_to = account_config["wallet_account"]
		else:
			payment_entry.paid_from = account_config["wallet_account"]

	payment_entry.reference_no = transaction.provider_transaction_id or transaction.external_id
	payment_entry.reference_date = getdate(transaction.completed_at or frappe.utils.nowdate())
	_set_pawapay_payment_entry_fields(payment_entry, transaction)
	payment_entry.remarks = (
		f"Payment Entry for PawaPay {transaction.operation_type} {transaction.external_id} "
		f"against Payment Request {payment_request.name}. "
		f"Collected {transaction.currency} {transaction.amount}"
	)

	for row in payment_entry.references:
		if hasattr(row, "payment_request") and not row.payment_request:
			row.payment_request = payment_request.name

	payment_entry.insert(ignore_permissions=True)
	payment_entry.submit()
	return payment_entry


def create_outbound_payment_entry(transaction):
	if transaction.operation_type == "Payout":
		return _create_payout_payment_entry(transaction)
	if transaction.operation_type == "Refund":
		return _create_refund_payment_entry(transaction)
	frappe.throw(_("Unsupported outbound PawaPay operation {0}.").format(transaction.operation_type))


def _create_payout_payment_entry(transaction):
	request = frappe.get_doc("PawaPay Payout Request", transaction.payout_request)
	settings = frappe.get_doc("PawaPay Settings", transaction.settings)
	payment_entry = _new_outbound_payment_entry(
		transaction=transaction,
		party_type=request.party_type,
		party=request.party,
		paid_from=transaction.collection_account or settings.pawapay_wallet_account,
		paid_to=get_party_account(request.party_type, request.party, request.company),
		reference_doctype=request.source_doctype,
		reference_name=request.source_name,
	)
	payment_entry.remarks = (
		f"Payment Entry for PawaPay Payout {transaction.external_id} "
		f"against {request.source_doctype} {request.source_name}."
	)
	payment_entry.insert(ignore_permissions=True)
	payment_entry.submit()
	return payment_entry


def _create_refund_payment_entry(transaction):
	request = frappe.get_doc("PawaPay Refund Request", transaction.refund_request)
	party_type, party, party_account = _get_refund_party(request)
	payment_entry = _new_outbound_payment_entry(
		transaction=transaction,
		party_type=party_type,
		party=party,
		paid_from=transaction.collection_account,
		paid_to=party_account,
		reference_doctype=request.accounting_reference_doctype,
		reference_name=request.accounting_reference_name,
	)
	payment_entry.remarks = (
		f"Payment Entry for PawaPay Refund {transaction.external_id} "
		f"against {request.accounting_reference_doctype} {request.accounting_reference_name}."
	)
	payment_entry.insert(ignore_permissions=True)
	payment_entry.submit()
	return payment_entry


def _new_outbound_payment_entry(
	*,
	transaction,
	party_type: str,
	party: str,
	paid_from: str,
	paid_to: str,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
):
	settings = frappe.get_doc("PawaPay Settings", transaction.settings)
	payment_entry = frappe.new_doc("Payment Entry")
	payment_entry.payment_type = "Pay"
	payment_entry.company = transaction.company
	payment_entry.posting_date = getdate(transaction.completed_at or frappe.utils.nowdate())
	payment_entry.mode_of_payment = transaction.collection_mode_of_payment or settings.mode_of_payment
	payment_entry.party_type = party_type
	payment_entry.party = party
	payment_entry.paid_from = paid_from
	payment_entry.paid_to = paid_to
	payment_entry.paid_amount = transaction.amount
	payment_entry.received_amount = transaction.amount
	payment_entry.reference_no = transaction.provider_transaction_id or transaction.external_id
	payment_entry.reference_date = getdate(transaction.completed_at or frappe.utils.nowdate())
	_set_pawapay_payment_entry_fields(payment_entry, transaction)
	if reference_doctype and reference_name:
		payment_entry.append(
			"references",
			{
				"reference_doctype": reference_doctype,
				"reference_name": reference_name,
				"allocated_amount": transaction.amount,
			},
		)
	payment_entry.setup_party_account_field()
	payment_entry.set_missing_values()
	payment_entry.set_exchange_rate()
	return payment_entry


def _get_refund_party(request):
	ref_doc = frappe.get_doc(request.accounting_reference_doctype, request.accounting_reference_name)
	if request.accounting_reference_doctype == "Sales Invoice":
		return "Customer", ref_doc.customer, ref_doc.debit_to
	if request.accounting_reference_doctype == "Payment Entry":
		if not (ref_doc.party_type and ref_doc.party):
			frappe.throw(_("Refund accounting Payment Entry must have a party."))
		return ref_doc.party_type, ref_doc.party, get_party_account(ref_doc.party_type, ref_doc.party, request.company)
	frappe.throw(_("Unsupported refund accounting reference {0}.").format(request.accounting_reference_doctype))


def _set_pawapay_payment_entry_fields(payment_entry, transaction):
	if payment_entry.meta.has_field(PAWAPAY_PAYMENT_ENTRY_TRANSACTION_FIELD):
		payment_entry.set(PAWAPAY_PAYMENT_ENTRY_TRANSACTION_FIELD, transaction.name)
	if payment_entry.meta.has_field(PAWAPAY_PAYMENT_ENTRY_EXTERNAL_ID_FIELD):
		payment_entry.set(PAWAPAY_PAYMENT_ENTRY_EXTERNAL_ID_FIELD, transaction.external_id)


def _iter_collection_account_candidates(settings, country: str, currency: str):
	rows = [
		row
		for row in settings.get("collection_accounts") or []
		if row.enabled and (row.currency or "").upper() == currency
	]
	for row in rows:
		row_country = row.pawapay_country_code or get_pawapay_country_code(row.country)
		if row_country == country:
			yield row
	for row in rows:
		if not row.pawapay_country_code and not (row.country or "").strip():
			yield row


def _default_collection_mode_of_payment(settings, currency: str):
	return settings.mode_of_payment or f"{settings.gateway}-{currency}"


def _collection_row_mode_of_payment(settings, row):
	return row.mode_of_payment or f"{settings.gateway}-{row.currency}"


def _validate_collection_account_currency(account: str, currency: str):
	account_currency = frappe.get_cached_value("Account", account, "account_currency")
	if account_currency and account_currency != currency:
		frappe.throw(
			_("PawaPay collection account {0} is in {1}, but the mapping currency is {2}.").format(
				account, account_currency, currency
			)
		)


def _get_transaction_collection_account(settings, transaction):
	if transaction.collection_account:
		return {
			"wallet_account": transaction.collection_account,
			"mode_of_payment": transaction.collection_mode_of_payment
			or _default_collection_mode_of_payment(settings, transaction.currency),
		}
	return get_collection_account(settings, transaction.country, transaction.currency)
