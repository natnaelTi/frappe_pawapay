import hashlib
import json

import frappe
from frappe.model.document import Document

from frappe_pawapay.pawapay.state_machine import effective_lifecycle_state
from frappe_pawapay.pawapay.validation import assert_allowed_redirect


class PawaPayTransaction(Document):
	def validate(self):
		if self.external_id:
			self.external_id = self.external_id.strip()

	def before_trash(self):
		if self.initiation_status and self.initiation_status != "CREATED":
			frappe.throw("Cannot delete a PawaPay Transaction after initiation.")

	def apply_initiation_response(self, payload: dict, source: str = "Initiation"):
		status = payload.get("status") or payload.get("initiationStatus") or "ACCEPTED"
		failure_reason = payload.get("failureReason") or {}
		previous_lifecycle_status = self.lifecycle_status
		self.initiation_status = status
		self.redirect_url = assert_allowed_redirect(payload.get("redirectUrl")) if payload.get("redirectUrl") else None
		self.checkout_code = payload.get("checkoutCode") or self.checkout_code
		self.provider_created_at = payload.get("created") or self.provider_created_at
		self.expires_at = payload.get("expiresAt") or self.expires_at
		if failure_reason:
			self.failure_code = failure_reason.get("failureCode") or self.failure_code
			self.failure_message = failure_reason.get("failureMessage") or self.failure_message
		if status == "REJECTED":
			self.lifecycle_status = "FAILED"
			self.review_status = "NEEDS_ATTENTION"
		self.append_status_event(source, previous_lifecycle_status, status, self.lifecycle_status, payload)
		self.save(ignore_permissions=True)

	def apply_lifecycle_state(self, incoming: str | None, source: str = "Poll", payload: dict | None = None):
		if not incoming:
			self.review_status = "NEEDS_ATTENTION"
			self.append_status_event(source, self.lifecycle_status, "UNKNOWN", self.lifecycle_status, payload)
			return
		previous = self.lifecycle_status
		effective, changed = effective_lifecycle_state(previous, incoming)
		self.lifecycle_status = effective
		if effective == "COMPLETED":
			self.completed_at = self.completed_at or frappe.utils.now_datetime()
		if incoming not in {
			"ACCEPTED",
			"WAITING_PAYMENT",
			"PROCESSING",
			"IN_RECONCILIATION",
			"COMPLETED",
			"FAILED",
			"EXPIRED",
			"CANCELLED",
			"ENQUEUED",
		}:
			self.review_status = "NEEDS_ATTENTION"
		self.append_status_event(source, previous, incoming, effective, payload)
		if changed:
			self.last_checked_at = frappe.utils.now_datetime()

	def append_status_event(self, source, previous, received, effective, payload=None):
		raw = json.dumps(payload or {}, default=str, separators=(",", ":"), sort_keys=True)
		self.append(
			"status_events",
			{
				"source": source,
				"previous_state": previous,
				"received_state": received,
				"effective_state": effective,
				"event_timestamp": frappe.utils.now_datetime(),
				"payload_sha256": hashlib.sha256(raw.encode("utf-8")).hexdigest(),
			},
		)

	@frappe.whitelist()
	def check_status(self):
		frappe.only_for(("Accounts Manager", "System Manager"))
		from frappe_pawapay.pawapay.services.reconciliation import reconcile_transaction

		reconcile_transaction(self.name)
		return {"ok": True}

	@frappe.whitelist()
	def retry_accounting(self):
		frappe.only_for(("Accounts Manager", "System Manager"))
		from frappe_pawapay.pawapay.services.accounting import finalize_inbound_payment, finalize_outbound_payment

		if self.lifecycle_status != "COMPLETED":
			frappe.throw("Only completed PawaPay transactions can retry accounting.")
		if self.accounting_status == "POSTED" and self.payment_entry:
			return {"ok": True, "status": "already_posted", "payment_entry": self.payment_entry}
		if self.operation_type in {"Checkout", "Deposit"}:
			finalize_inbound_payment(self.name)
		else:
			finalize_outbound_payment(self.name)
		return {"ok": True}

	@frappe.whitelist()
	def resend_callback(self):
		frappe.only_for(("Accounts Manager", "System Manager"))
		if self.operation_type == "Payout":
			from frappe_pawapay.pawapay.services.payouts import resend_payout_callback

			return resend_payout_callback(self.name)
		if self.operation_type == "Refund":
			from frappe_pawapay.pawapay.services.refunds import resend_refund_callback

			return resend_refund_callback(self.name)
		frappe.throw("Only PawaPay payouts and refunds support callback resend.")

	@frappe.whitelist()
	def cancel_enqueued(self):
		frappe.only_for(("Accounts Manager", "System Manager"))
		if self.operation_type == "Payout":
			from frappe_pawapay.pawapay.services.payouts import cancel_enqueued_payout

			return cancel_enqueued_payout(self.name)
		if self.operation_type == "Refund":
			from frappe_pawapay.pawapay.services.refunds import cancel_enqueued_refund

			return cancel_enqueued_refund(self.name)
		frappe.throw("Only PawaPay payouts and refunds can be cancelled from this action.")
