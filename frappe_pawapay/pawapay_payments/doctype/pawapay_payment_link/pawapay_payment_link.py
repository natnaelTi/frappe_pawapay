import frappe
from frappe.model.document import Document


class PawaPayPaymentLink(Document):
	def validate(self):
		if self.payment_request:
			pr = frappe.get_doc("Payment Request", self.payment_request)
			self.reference_doctype = pr.reference_doctype
			self.reference_name = pr.reference_name
			self.company = self.company or pr.get("company")
			self.expected_amount = pr.grand_total
			self.currency = pr.currency

	def on_trash(self):
		if self.last_checkout_transaction:
			frappe.throw("Cannot delete a PawaPay Payment Link after checkout initiation.")
