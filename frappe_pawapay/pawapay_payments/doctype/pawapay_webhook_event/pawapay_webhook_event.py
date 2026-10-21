import frappe
from frappe.model.document import Document


class PawaPayWebhookEvent(Document):
	@frappe.whitelist()
	def retry_processing(self):
		frappe.only_for(("Accounts Manager", "System Manager"))
		if self.processing_status in {"Processed", "Duplicate"}:
			return {"ok": True, "status": self.processing_status}
		if not self.transaction:
			frappe.throw("Only webhook events linked to a PawaPay Transaction can be retried.")
		if self.signature_status == "Invalid":
			frappe.throw("Webhook events with invalid signatures cannot be retried.")

		from frappe_pawapay.pawapay.callbacks import process_webhook
		from frappe_pawapay.pawapay.observability import record_audit_event

		self.db_set("processing_status", "Received")
		record_audit_event(
			event_type="Reconciliation Retryable",
			severity="Warning",
			status="Webhook Retry Requested",
			transaction_name=self.transaction,
			webhook_event=self.name,
			settings=self.settings,
			operation_type=self.operation_type,
			external_id=self.external_id,
			message="Operator requested webhook processing retry.",
			retryable=True,
		)
		process_webhook(self.name)
		return {"ok": True}
