from __future__ import annotations

import hashlib
import json

import frappe

from frappe_pawapay.pawapay.observability import record_audit_event
from frappe_pawapay.pawapay.signing import SignatureVerificationError, verify_callback_signature
from frappe_pawapay.pawapay.validation import validate_completed_provider_payload

MAX_CALLBACK_BYTES = 256 * 1024


def receive_callback():
	if frappe.request.method != "POST":
		frappe.throw("Method Not Allowed")
	raw = frappe.request.get_data() or b""
	if len(raw) > MAX_CALLBACK_BYTES:
		frappe.local.response.http_status_code = 413
		return {"ok": False, "error": "callback_too_large"}

	fingerprint = hashlib.sha256(raw + str(frappe.request.headers.get("Signature", "")).encode()).hexdigest()
	try:
		payload = json.loads(raw.decode("utf-8") or "{}")
	except (UnicodeDecodeError, json.JSONDecodeError):
		_insert_unprocessable_webhook(fingerprint, raw, "Invalid JSON")
		frappe.local.response.http_status_code = 200
		return {"ok": True, "stored": "invalid_json"}

	external_id = _extract_external_id(payload)

	existing = frappe.db.get_value("PawaPay Webhook Event", {"fingerprint": fingerprint}, "name")
	if existing:
		frappe.db.set_value("PawaPay Webhook Event", existing, "processing_status", "Duplicate")
		record_audit_event(
			event_type="Callback Duplicate",
			severity="Warning",
			status="Duplicate",
			webhook_event=existing,
			external_id=external_id,
			message="Duplicate PawaPay callback fingerprint received.",
			details={"fingerprint": fingerprint},
		)
		frappe.local.response.http_status_code = 200
		return {"ok": True, "duplicate": True}

	transaction_name = frappe.db.get_value("PawaPay Transaction", {"external_id": external_id}, "name")
	settings_name = None
	environment = None
	signature_status = "Unsigned"
	if transaction_name:
		settings_name, environment = frappe.db.get_value(
			"PawaPay Transaction", transaction_name, ["settings", "environment"]
		)
		settings = frappe.get_doc("PawaPay Settings", settings_name)
		try:
			verified, key_id = verify_callback_signature(
				settings=settings, raw_body=raw, headers=frappe.request.headers
			)
			signature_status = "Verified" if verified else "Unsigned"
		except SignatureVerificationError as exc:
			signature_status = "Invalid"
			key_id = getattr(exc, "key_id", None)
			signature_error = str(exc)
		else:
			signature_error = None
	else:
		key_id = None
		signature_error = None

	processing_status = "Orphaned"
	if transaction_name:
		processing_status = "Received"
		if signature_status == "Invalid":
			processing_status = "Failed"

	webhook = frappe.get_doc(
		{
			"doctype": "PawaPay Webhook Event",
			"fingerprint": fingerprint,
			"operation_type": _extract_operation(payload),
			"external_id": external_id,
			"transaction": transaction_name,
			"received_at": frappe.utils.now_datetime(),
			"settings": settings_name,
			"environment": environment,
			"signature_status": signature_status,
			"key_id": key_id,
			"raw_body_hash": hashlib.sha256(raw).hexdigest(),
			"redacted_payload": frappe.as_json(_redact(payload), indent=2),
			"processing_status": processing_status,
			"last_error": signature_error,
		}
	)
	webhook.insert(ignore_permissions=True)
	audit_event_type = "Callback Received"
	audit_severity = "Info"
	if signature_status == "Invalid":
		audit_event_type = "Callback Failed"
		audit_severity = "Error"
	elif processing_status == "Orphaned":
		audit_event_type = "Callback Orphaned"
		audit_severity = "Warning"
	record_audit_event(
		event_type=audit_event_type,
		severity=audit_severity,
		status=processing_status,
		transaction_name=transaction_name,
		webhook_event=webhook.name,
		settings=settings_name,
		operation_type=webhook.operation_type,
		external_id=external_id,
		message=signature_error or f"PawaPay callback stored as {processing_status}.",
		details={"fingerprint": fingerprint, "signature_status": signature_status},
		create_error_log=signature_status == "Invalid",
		error_title="PawaPay callback signature verification failed",
		reference_doctype="PawaPay Webhook Event",
		reference_name=webhook.name,
	)
	frappe.db.commit()
	if processing_status == "Received":
		frappe.enqueue(
			"frappe_pawapay.pawapay.callbacks.process_webhook",
			webhook_name=webhook.name,
			queue="short",
			enqueue_after_commit=True,
		)
	frappe.local.response.http_status_code = 200
	return {"ok": True}


def process_webhook(webhook_name: str):
	webhook = frappe.get_doc("PawaPay Webhook Event", webhook_name)
	if webhook.processing_status in {"Processed", "Duplicate"} or not webhook.transaction:
		return
	if webhook.signature_status == "Invalid":
		record_audit_event(
			event_type="Callback Failed",
			severity="Error",
			status="Invalid Signature",
			webhook_event=webhook.name,
			transaction_name=webhook.transaction,
			settings=webhook.settings,
			operation_type=webhook.operation_type,
			external_id=webhook.external_id,
			message=webhook.last_error or "Invalid PawaPay callback signature.",
			create_error_log=True,
			error_title="PawaPay invalid callback skipped",
			reference_doctype="PawaPay Webhook Event",
			reference_name=webhook.name,
		)
		return
	if webhook.signature_status != "Verified":
		_reconcile_from_webhook(webhook)
		webhook.db_set("processing_status", "Processed")
		record_audit_event(
			event_type="Webhook Processed",
			severity="Info",
			status="Reconciled Unsigned Callback",
			webhook_event=webhook.name,
			transaction_name=webhook.transaction,
			settings=webhook.settings,
			operation_type=webhook.operation_type,
			external_id=webhook.external_id,
			message="Unsigned callback was accepted through reconciliation.",
		)
		return
	payload = json.loads(webhook.redacted_payload or "{}")
	transaction = frappe.get_doc("PawaPay Transaction", webhook.transaction)
	state = _extract_lifecycle_state(payload)
	if state == "COMPLETED":
		try:
			validate_completed_provider_payload(transaction, payload, "callback", mark_conflict=False)
		except frappe.ValidationError as exc:
			try:
				_reconcile_from_webhook(webhook)
			except Exception as reconciliation_exc:
				failure_message = (
					f"Callback payload validation failed: {str(exc)[:450]}; "
					f"reconciliation failed: {str(reconciliation_exc)[:450]}"
				)
				webhook.db_set(
					{
						"processing_status": "Failed",
						"last_error": failure_message,
					}
				)
				record_audit_event(
					event_type="Callback Failed",
					severity="Error",
					status="Validation Failed",
					transaction=transaction,
					webhook_event=webhook.name,
					message=failure_message,
					details={
						"validation_error": str(exc),
						"reconciliation_error": str(reconciliation_exc),
					},
					create_error_log=True,
					error_title="PawaPay callback validation and reconciliation failed",
					reference_doctype="PawaPay Webhook Event",
					reference_name=webhook.name,
				)
				return
			reconciled_message = f"Callback payload required reconciliation: {str(exc)[:900]}"
			webhook.db_set({"processing_status": "Processed", "last_error": reconciled_message})
			record_audit_event(
				event_type="Webhook Processed",
				severity="Warning",
				status="Reconciled Payload Mismatch",
				transaction=transaction,
				webhook_event=webhook.name,
				message=reconciled_message,
				details={"validation_error": str(exc)},
			)
			return
	transaction.provider_transaction_id = _extract_payload_value(payload, "providerTransactionId") or transaction.provider_transaction_id
	transaction.failure_code = _extract_payload_value(payload, "failureCode") or transaction.failure_code
	transaction.failure_message = _extract_payload_value(payload, "failureMessage") or transaction.failure_message
	transaction.callback_count = (transaction.callback_count or 0) + 1
	transaction.callback_received_at = frappe.utils.now_datetime()
	transaction.apply_lifecycle_state(state, source="Callback", payload=payload)
	transaction.save(ignore_permissions=True)
	webhook.db_set("processing_status", "Processed")
	record_audit_event(
		event_type="Webhook Processed",
		severity="Info" if transaction.lifecycle_status != "FAILED" else "Warning",
		status=transaction.lifecycle_status,
		transaction=transaction,
		webhook_event=webhook.name,
		message=f"PawaPay webhook processed with lifecycle status {transaction.lifecycle_status}.",
	)
	if transaction.lifecycle_status == "COMPLETED":
		_enqueue_accounting(transaction)
	elif transaction.lifecycle_status == "FAILED":
		_mark_request_failed(transaction)


def _reconcile_from_webhook(webhook):
	from frappe_pawapay.pawapay.services.reconciliation import reconcile_transaction

	reconcile_transaction(webhook.transaction)


def _extract_lifecycle_state(payload: dict) -> str | None:
	for key in ("status", "checkoutStatus", "lifecycleStatus"):
		value = _extract_payload_value(payload, key)
		if value:
			return value
	return None


def _extract_payload_value(payload: dict, key: str):
	if payload.get(key):
		return payload.get(key)
	failure_reason = payload.get("failureReason")
	if key in {"failureCode", "failureMessage"} and isinstance(failure_reason, dict) and failure_reason.get(key):
		return failure_reason.get(key)
	for wrapper in ("checkout", "deposit", "payout", "refund", "data"):
		wrapped = payload.get(wrapper)
		if isinstance(wrapped, dict) and wrapped.get(key):
			return wrapped.get(key)
		failure_reason = wrapped.get("failureReason") if isinstance(wrapped, dict) else None
		if key in {"failureCode", "failureMessage"} and isinstance(failure_reason, dict) and failure_reason.get(key):
			return failure_reason.get(key)
	return None


def _enqueue_accounting(transaction):
	if transaction.operation_type in {"Checkout", "Deposit"}:
		method = "frappe_pawapay.pawapay.services.accounting.finalize_inbound_payment"
	elif transaction.operation_type in {"Payout", "Refund"}:
		method = "frappe_pawapay.pawapay.services.accounting.finalize_outbound_payment"
	else:
		return
	frappe.enqueue(method, transaction_name=transaction.name, queue="short", enqueue_after_commit=True)
	record_audit_event(
		event_type="Accounting Queued",
		severity="Info",
		status=transaction.accounting_status,
		transaction=transaction,
		message=f"Queued accounting finalization for PawaPay {transaction.operation_type}.",
		details={"method": method},
	)


def _mark_request_failed(transaction):
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


def retry_received_webhooks():
	for name in frappe.get_all(
		"PawaPay Webhook Event",
		filters={"processing_status": "Received", "transaction": ["is", "set"]},
		pluck="name",
		limit=50,
	):
		frappe.enqueue("frappe_pawapay.pawapay.callbacks.process_webhook", webhook_name=name, queue="short")
		record_audit_event(
			event_type="Reconciliation Retryable",
			severity="Warning",
			status="Webhook Retry Queued",
			webhook_event=name,
			message="Queued retry for received PawaPay webhook.",
			retryable=True,
		)


def _extract_external_id(payload: dict) -> str | None:
	for key in ("checkoutId", "depositId", "payoutId", "refundId"):
		if payload.get(key):
			return payload.get(key)
	return None


def _extract_operation(payload: dict) -> str:
	if payload.get("checkoutId"):
		return "Checkout"
	if payload.get("depositId"):
		return "Deposit"
	if payload.get("payoutId"):
		return "Payout"
	if payload.get("refundId"):
		return "Refund"
	return "Unknown"


def _redact(payload):
	if isinstance(payload, dict):
		return {
			key: ("***" if key.lower() in {"msisdn", "phone", "authorization"} else _redact(value))
			for key, value in payload.items()
		}
	if isinstance(payload, list):
		return [_redact(value) for value in payload]
	return payload


def _insert_unprocessable_webhook(fingerprint: str, raw: bytes, error: str):
	if frappe.db.exists("PawaPay Webhook Event", {"fingerprint": fingerprint}):
		return
	webhook = frappe.get_doc(
		{
			"doctype": "PawaPay Webhook Event",
			"fingerprint": fingerprint,
			"operation_type": "Unknown",
			"received_at": frappe.utils.now_datetime(),
			"signature_status": "Invalid",
			"raw_body_hash": hashlib.sha256(raw).hexdigest(),
			"processing_status": "Failed",
			"last_error": error,
		}
	)
	webhook.insert(ignore_permissions=True)
	record_audit_event(
		event_type="Callback Failed",
		severity="Error",
		status="Invalid JSON",
		webhook_event=webhook.name,
		message=error,
		details={"fingerprint": fingerprint, "raw_body_hash": hashlib.sha256(raw).hexdigest()},
		create_error_log=True,
		error_title="PawaPay callback invalid JSON",
		reference_doctype="PawaPay Webhook Event",
		reference_name=webhook.name,
	)
	frappe.db.commit()
