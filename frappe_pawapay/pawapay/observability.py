from __future__ import annotations

import frappe


def record_audit_event(
	*,
	event_type: str,
	severity: str = "Info",
	status: str | None = None,
	message: str | None = None,
	transaction=None,
	transaction_name: str | None = None,
	webhook_event: str | None = None,
	settings: str | None = None,
	operation_type: str | None = None,
	external_id: str | None = None,
	payment_request: str | None = None,
	payment_entry: str | None = None,
	payout_request: str | None = None,
	refund_request: str | None = None,
	retryable: bool = False,
	next_retry=None,
	details: dict | None = None,
	create_error_log: bool = False,
	error_title: str | None = None,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
):
	try:
		if transaction:
			transaction_name = transaction.name
			settings = settings or transaction.settings
			operation_type = operation_type or transaction.operation_type
			external_id = external_id or transaction.external_id
			payment_request = payment_request or transaction.payment_request
			payment_entry = payment_entry or transaction.payment_entry
			payout_request = payout_request or transaction.payout_request
			refund_request = refund_request or transaction.refund_request

		error_log = None
		if create_error_log:
			error_doc = frappe.log_error(
				title=(error_title or f"PawaPay {event_type}")[:140],
				message=frappe.as_json(details or {"message": message}, indent=2),
				reference_doctype=reference_doctype or ("PawaPay Transaction" if transaction_name else None),
				reference_name=reference_name or transaction_name,
			)
			error_log = getattr(error_doc, "name", None)

		doc = frappe.get_doc(
			{
				"doctype": "PawaPay Audit Event",
				"event_type": event_type,
				"severity": severity,
				"event_at": frappe.utils.now_datetime(),
				"status": status,
				"retryable": 1 if retryable else 0,
				"next_retry": next_retry,
				"operation_type": operation_type,
				"external_id": external_id,
				"transaction": transaction_name,
				"webhook_event": webhook_event,
				"settings": settings,
				"payment_request": payment_request,
				"payment_entry": payment_entry,
				"payout_request": payout_request,
				"refund_request": refund_request,
				"error_log": error_log,
				"message": (message or "")[:1000],
				"details_json": frappe.as_json(details or {}, indent=2) if details else None,
			}
		)
		doc.insert(ignore_permissions=True)
		return doc
	except Exception:
		return None
