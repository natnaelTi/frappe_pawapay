import frappe
from frappe import _
from frappe.model.document import Document

from frappe_pawapay.pawapay.validation import decimal_from, validate_amount_against_active_configuration

SUPPORTED_REFUND_REFERENCES = {"Sales Invoice", "Payment Entry"}


class PawaPayRefundRequest(Document):
	def validate(self):
		if self.refund_type == "Partial":
			if not self.amount:
				frappe.throw(_("Refund amount is required for partial refunds."))
			if decimal_from(self.amount) <= 0:
				frappe.throw(_("Refund amount must be greater than zero."))
		if self.original_transaction:
			transaction = frappe.get_doc("PawaPay Transaction", self.original_transaction)
			if transaction.operation_type not in {"Checkout", "Deposit"} or transaction.lifecycle_status != "COMPLETED":
				frappe.throw(_("Refunds can only be requested for completed PawaPay transactions."))
			if self.currency and transaction.currency != self.currency:
				frappe.throw(_("Refund currency must match the original PawaPay transaction."))
			if self.refund_type == "Full":
				self.amount = decimal_from(transaction.amount) - self._get_non_failed_refund_total(transaction.name)
				if decimal_from(self.amount) <= 0:
					frappe.throw(_("The original PawaPay transaction has no refundable balance."))
			self._validate_refund_limit(transaction)
		if self.docstatus == 1 or self.enable_remote_initiation:
			self._validate_ready_for_remote_initiation()

	def on_submit(self):
		if self.enable_remote_initiation:
			frappe.enqueue(
				"frappe_pawapay.pawapay.services.refunds.initiate_refund_request",
				request_name=self.name,
				enqueue_after_commit=True,
			)

	@frappe.whitelist()
	def initiate_refund(self):
		frappe.only_for(("Accounts Manager", "System Manager"))
		from frappe_pawapay.pawapay.services.refunds import initiate_refund_request

		return initiate_refund_request(self.name)

	def _validate_refund_limit(self, transaction):
		refunded = self._get_non_failed_refund_total(transaction.name)
		total = refunded + decimal_from(self.amount or 0)
		if total > decimal_from(transaction.amount):
			frappe.throw(_("Requested refunds exceed the original PawaPay transaction amount."))

	def _get_non_failed_refund_total(self, transaction_name: str):
		rows = frappe.get_all(
			"PawaPay Refund Request",
			filters={
				"original_transaction": transaction_name,
				"name": ["!=", self.name],
				"docstatus": ["!=", 2],
				"status": ["not in", ["Failed", "Cancelled"]],
			},
			fields=["amount"],
		)
		total = sum(decimal_from(row.amount or 0) for row in rows)
		return total

	def _validate_ready_for_remote_initiation(self):
		if not (self.accounting_reference_doctype and self.accounting_reference_name):
			frappe.throw(_("Accounting Reference is required before initiating a PawaPay refund."))
		if self.accounting_reference_doctype not in SUPPORTED_REFUND_REFERENCES:
			frappe.throw(_("PawaPay refunds do not yet support accounting reference type {0}.").format(self.accounting_reference_doctype))
		original = frappe.get_doc("PawaPay Transaction", self.original_transaction)
		settings = frappe.get_doc("PawaPay Settings", self.settings)
		validate_amount_against_active_configuration(
			settings,
			decimal_from(self.amount),
			self.currency,
			original.country,
			"REFUND",
		)
