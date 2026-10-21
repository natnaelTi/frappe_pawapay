from __future__ import annotations

import frappe

from frappe_pawapay.pawapay.client import PawaPayClient
from frappe_pawapay.pawapay.observability import record_audit_event
from frappe_pawapay.pawapay.services.checkout import consume_checkout_status
from frappe_pawapay.pawapay.services.payouts import consume_payout_status
from frappe_pawapay.pawapay.services.refunds import consume_refund_status


def reconcile_pending_transactions(batch_size: int | None = None):
	names = frappe.get_all(
		"PawaPay Transaction",
		filters=[
			["operation_type", "in", ["Checkout", "Deposit", "Payout", "Refund"]],
			["docstatus", "<", 2],
			[
				"lifecycle_status",
				"in",
				["WAITING_PAYMENT", "PROCESSING", "IN_RECONCILIATION", "ACCEPTED", "ENQUEUED"],
			],
		],
		pluck="name",
		limit=batch_size or 25,
		order_by="modified asc",
	)
	for name in names:
		try:
			reconcile_transaction(name)
		except Exception as exc:
			record_audit_event(
				event_type="Reconciliation Failed",
				severity="Error",
				status="Failed",
				transaction_name=name,
				message=str(exc),
				details={"exception": str(exc)},
				create_error_log=True,
				error_title="PawaPay reconciliation failed",
			)


def reconcile_transaction(name: str):
	transaction = frappe.get_doc("PawaPay Transaction", name)
	settings = frappe.get_doc("PawaPay Settings", transaction.settings)
	if not settings.reconciliation_enabled:
		return
	client = PawaPayClient(settings)
	transaction.reconciliation_attempt_count = (transaction.reconciliation_attempt_count or 0) + 1
	record_audit_event(
		event_type="Reconciliation Attempt",
		severity="Info",
		status=transaction.lifecycle_status,
		transaction=transaction,
		message=f"Started PawaPay reconciliation attempt {transaction.reconciliation_attempt_count}.",
	)
	if transaction.operation_type == "Checkout":
		payload = client.get_checkout(transaction.external_id)
		consume_checkout_status(transaction, payload, source="Reconciliation")
	elif transaction.operation_type == "Deposit":
		payload = client.get_deposit(transaction.external_id)
		transaction.apply_lifecycle_state(payload.get("status"), source="Reconciliation", payload=payload)
	elif transaction.operation_type == "Payout":
		payload = client.get_payout(transaction.external_id)
		consume_payout_status(transaction, payload, source="Reconciliation")
	elif transaction.operation_type == "Refund":
		payload = client.get_refund(transaction.external_id)
		consume_refund_status(transaction, payload, source="Reconciliation")
	transaction.save(ignore_permissions=True)
	_log_reconciliation_result(transaction, payload)
	if transaction.lifecycle_status == "COMPLETED":
		if transaction.operation_type in {"Checkout", "Deposit"}:
			method = "frappe_pawapay.pawapay.services.accounting.finalize_inbound_payment"
		else:
			method = "frappe_pawapay.pawapay.services.accounting.finalize_outbound_payment"
		frappe.enqueue(method, transaction_name=transaction.name, queue="short", enqueue_after_commit=True)
		record_audit_event(
			event_type="Accounting Queued",
			severity="Info",
			status=transaction.accounting_status,
			transaction=transaction,
			message=f"Queued accounting finalization after PawaPay reconciliation for {transaction.operation_type}.",
			details={"method": method},
		)
	elif transaction.lifecycle_status == "FAILED":
		_mark_failed_request(transaction)


def _mark_failed_request(transaction):
	values = {
		"status": "Failed",
		"last_error": transaction.failure_message or transaction.failure_code or "PawaPay transaction failed.",
	}
	if transaction.operation_type == "Payout" and transaction.payout_request:
		frappe.db.set_value("PawaPay Payout Request", transaction.payout_request, values)
	elif transaction.operation_type == "Refund" and transaction.refund_request:
		frappe.db.set_value("PawaPay Refund Request", transaction.refund_request, values)
	record_audit_event(
		event_type="Request Failed",
		severity="Warning",
		status="Failed",
		transaction=transaction,
		message=values["last_error"],
	)


def _log_reconciliation_result(transaction, payload: dict):
	provider_status = payload.get("status") if isinstance(payload, dict) else None
	if provider_status == "NOT_FOUND":
		record_audit_event(
			event_type="Reconciliation Retryable",
			severity="Warning",
			status="NOT_FOUND",
			transaction=transaction,
			message="PawaPay reconciliation returned NOT_FOUND; transaction remains retryable.",
			retryable=True,
			next_retry=transaction.next_check_at,
			details={"provider_status": provider_status, "attempts": transaction.reconciliation_attempt_count},
		)
		return

	record_audit_event(
		event_type="Reconciliation Completed",
		severity="Info" if transaction.lifecycle_status != "FAILED" else "Warning",
		status=transaction.lifecycle_status,
		transaction=transaction,
		message=f"PawaPay reconciliation completed with lifecycle status {transaction.lifecycle_status}.",
		details={"provider_status": provider_status, "attempts": transaction.reconciliation_attempt_count},
	)
