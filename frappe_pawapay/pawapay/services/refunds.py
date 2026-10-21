from __future__ import annotations

import uuid

import frappe
from frappe import _

from frappe_pawapay.compatibility import get_collection_account
from frappe_pawapay.pawapay.client import PawaPayClient
from frappe_pawapay.pawapay.exceptions import PawaPayUncertainError, PawaPayValidationError
from frappe_pawapay.pawapay.observability import record_audit_event
from frappe_pawapay.pawapay.validation import (
	decimal_from,
	validate_amount_against_active_configuration,
	validate_completed_provider_payload,
)

IN_FLIGHT_REFUND_STATUSES = {"ACCEPTED", "ENQUEUED", "PROCESSING", "IN_RECONCILIATION"}


def initiate_refund_request(request_name: str):
	request = frappe.get_doc("PawaPay Refund Request", request_name)
	if not request.enable_remote_initiation:
		frappe.throw(_("Remote PawaPay refund initiation is not enabled for this request."))
	if request.docstatus != 1:
		frappe.throw(_("Submit the PawaPay Refund Request before initiating refund."))
	if request.transaction:
		return request.transaction

	original = frappe.get_doc("PawaPay Transaction", request.original_transaction)
	if _has_in_flight_refund(original.name, request.name):
		frappe.throw(_("Another PawaPay refund is already in progress for the original transaction."))
	deposit_id = original.underlying_deposit_id or original.external_id
	if not deposit_id:
		frappe.throw(_("Original PawaPay deposit ID is required before initiating refund."))
	settings = frappe.get_doc("PawaPay Settings", request.settings)
	amount = decimal_from(request.amount)
	validate_amount_against_active_configuration(settings, amount, request.currency, original.country, "REFUND")
	account_config = (
		{"wallet_account": original.collection_account, "mode_of_payment": original.collection_mode_of_payment}
		if original.collection_account
		else get_collection_account(settings, original.country, request.currency)
	)
	refund_id = str(uuid.uuid4())
	transaction = frappe.get_doc(
		{
			"doctype": "PawaPay Transaction",
			"external_id": refund_id,
			"external_parent_id": deposit_id,
			"operation_type": "Refund",
			"settings": settings.name,
			"environment": settings.environment,
			"initiation_status": "CREATED",
			"lifecycle_status": "ACCEPTED",
			"accounting_status": "PENDING",
			"review_status": "NORMAL",
			"company": request.company,
			"amount": amount,
			"currency": request.currency,
			"country": original.country,
			"collection_account": account_config["wallet_account"],
			"collection_mode_of_payment": account_config["mode_of_payment"],
			"provider": original.provider,
			"client_reference_id": request.name,
			"reference_doctype": request.accounting_reference_doctype,
			"reference_name": request.accounting_reference_name,
			"refund_request": request.name,
		}
	)
	transaction.insert(ignore_permissions=True)
	request.db_set({"transaction": transaction.name, "status": "Initiated"})
	frappe.db.commit()

	client = PawaPayClient(settings)
	try:
		response = client.initiate_refund(
			refund_id=refund_id,
			deposit_id=deposit_id,
			amount=amount if request.refund_type == "Partial" else None,
			currency=request.currency if request.refund_type == "Partial" else None,
			reference=request.name,
		)
	except PawaPayValidationError as exc:
		_mark_rejected(transaction, request, str(exc))
		return transaction.name
	except PawaPayUncertainError as exc:
		transaction.db_set({"initiation_status": "UNCERTAIN", "last_error": str(exc)[:1000]})
		request.db_set({"last_error": str(exc)[:1000]})
		return transaction.name

	_apply_initiation(transaction, request, response)
	return transaction.name


def consume_refund_status(transaction, payload: dict, source: str = "Poll"):
	status_payload = payload.get("data") or payload
	wrapper_status = payload.get("status") or payload.get("searchStatus")
	if wrapper_status == "NOT_FOUND":
		transaction.append_status_event(source, transaction.lifecycle_status, "NOT_FOUND", transaction.lifecycle_status)
		return
	lifecycle = status_payload.get("status") or status_payload.get("lifecycleStatus")
	if lifecycle == "COMPLETED":
		validate_completed_provider_payload(transaction, status_payload, source)
	transaction.provider_transaction_id = status_payload.get("providerTransactionId") or transaction.provider_transaction_id
	failure_reason = status_payload.get("failureReason") or {}
	transaction.failure_code = failure_reason.get("failureCode") or status_payload.get("failureCode") or transaction.failure_code
	transaction.failure_message = failure_reason.get("failureMessage") or status_payload.get("failureMessage") or transaction.failure_message
	transaction.apply_lifecycle_state(lifecycle, source=source, payload=payload)


def cancel_enqueued_refund(transaction_name: str):
	transaction = frappe.get_doc("PawaPay Transaction", transaction_name)
	if transaction.operation_type != "Refund" or transaction.lifecycle_status != "ENQUEUED":
		frappe.throw(_("Only enqueued PawaPay refunds can be cancelled."))
	settings = frappe.get_doc("PawaPay Settings", transaction.settings)
	response = PawaPayClient(settings).cancel_enqueued_refund(transaction.external_id)
	record_audit_event(
		event_type="Request Failed",
		severity="Warning",
		status="Cancel Enqueued Requested",
		transaction=transaction,
		message="Operator requested cancellation of enqueued PawaPay refund.",
		details={"response": response},
	)
	return response


def resend_refund_callback(transaction_name: str):
	transaction = frappe.get_doc("PawaPay Transaction", transaction_name)
	if transaction.operation_type != "Refund":
		frappe.throw(_("Only PawaPay refund callbacks can be resent from this action."))
	settings = frappe.get_doc("PawaPay Settings", transaction.settings)
	response = PawaPayClient(settings).resend_refund_callback(transaction.external_id)
	record_audit_event(
		event_type="Reconciliation Retryable",
		severity="Warning",
		status="Callback Resend Requested",
		transaction=transaction,
		message="Operator requested PawaPay refund callback resend.",
		retryable=True,
		details={"response": response},
	)
	return response


def _has_in_flight_refund(original_transaction: str, request_name: str) -> bool:
	rows = frappe.get_all(
		"PawaPay Refund Request",
		filters={
			"original_transaction": original_transaction,
			"name": ["!=", request_name],
			"docstatus": ["!=", 2],
			"status": ["not in", ["Completed", "Failed", "Cancelled"]],
			"transaction": ["is", "set"],
		},
		fields=["transaction"],
	)
	for row in rows:
		if frappe.db.get_value("PawaPay Transaction", row.transaction, "lifecycle_status") in IN_FLIGHT_REFUND_STATUSES:
			return True
	return False


def _apply_initiation(transaction, request, response: dict):
	status = response.get("status")
	failure_reason = response.get("failureReason") or response.get("rejectionReason") or {}
	transaction.initiation_status = status or "ACCEPTED"
	transaction.provider_created_at = response.get("created") or transaction.provider_created_at
	if status == "REJECTED":
		transaction.lifecycle_status = "FAILED"
		transaction.review_status = "NEEDS_ATTENTION"
		transaction.failure_code = failure_reason.get("failureCode") or failure_reason.get("rejectionCode")
		transaction.failure_message = failure_reason.get("failureMessage") or failure_reason.get("rejectionMessage")
		request.db_set({"status": "Failed", "last_error": transaction.failure_message or transaction.failure_code})
	else:
		request.db_set("status", "Initiated")
	transaction.append_status_event("Initiation", "CREATED", transaction.initiation_status, transaction.lifecycle_status, response)
	transaction.save(ignore_permissions=True)


def _mark_rejected(transaction, request, message: str):
	transaction.db_set(
		{
			"initiation_status": "REJECTED",
			"lifecycle_status": "FAILED",
			"review_status": "NEEDS_ATTENTION",
			"last_error": message[:1000],
		}
	)
	request.db_set({"status": "Failed", "last_error": message[:1000]})
