import frappe
from frappe.model.document import Document


class PawaPayAuditEvent(Document):
	@frappe.whitelist()
	def retry_related_action(self):
		frappe.only_for(("Accounts Manager", "System Manager"))
		if self.webhook_event and self.event_type in {"Callback Failed", "Reconciliation Retryable"}:
			webhook = frappe.get_doc("PawaPay Webhook Event", self.webhook_event)
			return webhook.retry_processing()

		if self.transaction and self.event_type in {"Accounting Failed", "Accounting Conflict"}:
			transaction = frappe.get_doc("PawaPay Transaction", self.transaction)
			return transaction.retry_accounting()

		if self.transaction and self.event_type in {
			"Reconciliation Retryable",
			"Reconciliation Failed",
			"Reconciliation Attempt",
		}:
			transaction = frappe.get_doc("PawaPay Transaction", self.transaction)
			return transaction.check_status()

		frappe.throw("This audit event does not have a retryable related action.")
