from __future__ import annotations

from decimal import Decimal

import frappe

from frappe_pawapay.compatibility import (
	PAWAPAY_PAYMENT_ENTRY_EXTERNAL_ID_FIELD,
	create_payment_entry_from_request,
	create_outbound_payment_entry,
	get_payment_request_source_amount,
)
from frappe_pawapay.pawapay.observability import record_audit_event


def finalize_inbound_payment(transaction_name: str):
	_lock_doc("PawaPay Transaction", transaction_name)
	transaction = frappe.get_doc("PawaPay Transaction", transaction_name)
	if transaction.operation_type not in {"Checkout", "Deposit"} or transaction.lifecycle_status != "COMPLETED":
		return
	if transaction.accounting_status == "POSTED" and transaction.payment_entry:
		return
	if not transaction.payment_request:
		transaction.db_set({"accounting_status": "FAILED", "review_status": "NEEDS_ATTENTION"})
		record_audit_event(
			event_type="Accounting Failed",
			severity="Error",
			status="Missing Payment Request",
			transaction=transaction,
			message="Completed inbound PawaPay transaction has no Payment Request.",
			create_error_log=True,
			error_title="PawaPay inbound accounting missing Payment Request",
		)
		return

	existing = _get_existing_payment_entry(transaction)
	if existing:
		_mark_inbound_posted(transaction, existing)
		record_audit_event(
			event_type="Accounting Posted",
			severity="Info",
			status="Existing Payment Entry",
			transaction=transaction,
			payment_entry=existing,
			message="PawaPay inbound transaction reused an existing Payment Entry.",
		)
		return

	try:
		_lock_doc("Payment Request", transaction.payment_request)
		payment_request = frappe.get_doc("Payment Request", transaction.payment_request)
		source_currency = transaction.source_currency or transaction.currency
		source_amount = Decimal(str(transaction.source_amount or transaction.amount))
		if payment_request.currency != source_currency:
			_mark_conflict(transaction, "Currency mismatch during PawaPay accounting finalization.")
			return
		outstanding = Decimal(str(get_payment_request_source_amount(payment_request)))
		if source_amount != outstanding:
			_mark_conflict(transaction, "Amount mismatch during PawaPay accounting finalization.")
			return
		if payment_request.status == "Paid":
			_mark_conflict(transaction, "Payment Request was already paid before PawaPay finalization.")
			return
		existing_payment_entry = _get_existing_payment_entry(transaction)
		if existing_payment_entry:
			_mark_inbound_posted(transaction, existing_payment_entry)
			return
		if _submitted_payment_entry_exists(transaction):
			_mark_conflict(transaction, "A submitted Payment Entry already exists for this PawaPay transaction.")
			return
		try:
			payment_entry = create_payment_entry_from_request(transaction.payment_request, transaction)
		except frappe.DuplicateEntryError:
			existing_payment_entry = _get_existing_payment_entry(transaction)
			if existing_payment_entry:
				_mark_inbound_posted(transaction, existing_payment_entry)
				return
			raise
		_mark_inbound_posted(transaction, payment_entry.name)
		record_audit_event(
			event_type="Accounting Posted",
			severity="Info",
			status="Posted",
			transaction=transaction,
			payment_entry=payment_entry.name,
			message="PawaPay inbound Payment Entry submitted.",
		)
	except Exception as exc:
		frappe.db.set_value(
			"PawaPay Transaction",
			transaction.name,
			{"accounting_status": "FAILED", "review_status": "NEEDS_ATTENTION", "last_error": str(exc)[:1000]},
		)
		record_audit_event(
			event_type="Accounting Failed",
			severity="Error",
			status="Failed",
			transaction=transaction,
			message=str(exc),
			details={"exception": str(exc)},
			create_error_log=True,
			error_title="PawaPay accounting finalization failed",
		)


def finalize_outbound_payment(transaction_name: str):
	_lock_doc("PawaPay Transaction", transaction_name)
	transaction = frappe.get_doc("PawaPay Transaction", transaction_name)
	if transaction.operation_type not in {"Payout", "Refund"} or transaction.lifecycle_status != "COMPLETED":
		return
	if transaction.accounting_status == "POSTED" and transaction.payment_entry:
		return
	if transaction.operation_type == "Payout" and not transaction.payout_request:
		transaction.db_set({"accounting_status": "FAILED", "review_status": "NEEDS_ATTENTION"})
		record_audit_event(
			event_type="Accounting Failed",
			severity="Error",
			status="Missing Payout Request",
			transaction=transaction,
			message="Completed PawaPay payout transaction has no Payout Request.",
			create_error_log=True,
			error_title="PawaPay payout accounting missing request",
		)
		return
	if transaction.operation_type == "Refund" and not transaction.refund_request:
		transaction.db_set({"accounting_status": "FAILED", "review_status": "NEEDS_ATTENTION"})
		record_audit_event(
			event_type="Accounting Failed",
			severity="Error",
			status="Missing Refund Request",
			transaction=transaction,
			message="Completed PawaPay refund transaction has no Refund Request.",
			create_error_log=True,
			error_title="PawaPay refund accounting missing request",
		)
		return

	existing = _get_existing_payment_entry(transaction)
	if existing:
		_mark_outbound_posted(transaction, existing)
		return

	try:
		request_doctype = f"PawaPay {transaction.operation_type} Request"
		request_name = transaction.payout_request if transaction.operation_type == "Payout" else transaction.refund_request
		_lock_doc(request_doctype, request_name)
		existing_payment_entry = _get_existing_payment_entry(transaction)
		if existing_payment_entry:
			_mark_outbound_posted(transaction, existing_payment_entry)
			return
		try:
			payment_entry = create_outbound_payment_entry(transaction)
		except frappe.DuplicateEntryError:
			existing_payment_entry = _get_existing_payment_entry(transaction)
			if existing_payment_entry:
				_mark_outbound_posted(transaction, existing_payment_entry)
				return
			raise
		_mark_outbound_posted(transaction, payment_entry.name)
	except Exception as exc:
		frappe.db.set_value(
			"PawaPay Transaction",
			transaction.name,
			{"accounting_status": "FAILED", "review_status": "NEEDS_ATTENTION", "last_error": str(exc)[:1000]},
		)
		record_audit_event(
			event_type="Accounting Failed",
			severity="Error",
			status="Failed",
			transaction=transaction,
			message=str(exc),
			details={"exception": str(exc)},
			create_error_log=True,
			error_title="PawaPay outbound accounting finalization failed",
		)


def _lock_doc(doctype: str, name: str):
	frappe.db.sql(f"select name from `tab{doctype}` where name=%s for update", name)


def _mark_conflict(transaction, message: str):
	transaction.db_set(
		{
			"accounting_status": "CONFLICT",
			"review_status": "NEEDS_ATTENTION",
			"last_error": message[:1000],
		}
	)
	record_audit_event(
		event_type="Accounting Conflict",
		severity="Warning",
		status="Conflict",
		transaction=transaction,
		message=message,
		details={
			"amount": str(transaction.amount),
			"currency": transaction.currency,
			"source_amount": str(transaction.source_amount),
			"source_currency": transaction.source_currency,
			"payment_request": transaction.payment_request,
		},
	)


def _submitted_payment_entry_exists(transaction) -> bool:
	if not transaction.provider_transaction_id and not transaction.external_id:
		return False
	return bool(
		frappe.db.exists(
			"Payment Entry",
			{
				"docstatus": 1,
				"reference_no": ["in", [transaction.provider_transaction_id, transaction.external_id]],
			},
		)
	)


def _mark_outbound_posted(transaction, payment_entry: str):
	transaction.db_set({"payment_entry": payment_entry, "accounting_status": "POSTED"})
	if transaction.operation_type == "Payout":
		frappe.db.set_value(
			"PawaPay Payout Request",
			transaction.payout_request,
			{"payment_entry": payment_entry, "status": "Completed"},
		)
	elif transaction.operation_type == "Refund":
		frappe.db.set_value(
			"PawaPay Refund Request",
			transaction.refund_request,
			{"payment_entry": payment_entry, "status": "Completed"},
		)
	record_audit_event(
		event_type="Accounting Posted",
		severity="Info",
		status="Posted",
		transaction=transaction,
		payment_entry=payment_entry,
		message=f"PawaPay {transaction.operation_type} Payment Entry submitted.",
	)


def _mark_inbound_posted(transaction, payment_entry: str):
	transaction.db_set({"payment_entry": payment_entry, "accounting_status": "POSTED"})
	if transaction.payment_link:
		frappe.db.set_value("PawaPay Payment Link", transaction.payment_link, "status", "Paid")
	_notify_inbound_payment_completed(transaction.name)


def _notify_inbound_payment_completed(transaction_name: str):
	"""Notify installed apps after verified PawaPay accounting is posted.

	Consumer failures are isolated from the immutable payment result. Consumers
	must be idempotent and can reconcile from the posted transaction later.
	"""
	for method in frappe.get_hooks("pawapay_inbound_payment_completed") or []:
		try:
			frappe.get_attr(method)(transaction_name=transaction_name)
		except Exception:
			frappe.log_error(
				frappe.get_traceback(),
				f"PawaPay completion consumer failed: {method}",
			)


def _get_existing_payment_entry(transaction) -> str | None:
	if transaction.payment_entry:
		return transaction.payment_entry

	existing = frappe.db.get_value(
		"PawaPay Transaction",
		{"external_id": transaction.external_id, "name": ["!=", transaction.name], "payment_entry": ["is", "set"]},
		"payment_entry",
	)
	if existing:
		return existing

	if _payment_entry_has_field(PAWAPAY_PAYMENT_ENTRY_EXTERNAL_ID_FIELD):
		existing = frappe.db.get_value(
			"Payment Entry",
			{PAWAPAY_PAYMENT_ENTRY_EXTERNAL_ID_FIELD: transaction.external_id, "docstatus": ["!=", 2]},
			"name",
		)
		if existing:
			return existing

	return None


def _payment_entry_has_field(fieldname: str) -> bool:
	return any(field.fieldname == fieldname for field in frappe.get_meta("Payment Entry").fields)
