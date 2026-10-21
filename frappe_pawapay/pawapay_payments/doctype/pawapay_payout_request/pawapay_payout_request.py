import frappe
from frappe import _
from frappe.model.document import Document

from frappe_pawapay.pawapay.countries import get_pawapay_country_code
from frappe_pawapay.pawapay.validation import decimal_from, validate_amount_against_active_configuration

SUPPORTED_PAYOUT_REFERENCES = {"Purchase Invoice", "Expense Claim", "Employee Advance", "Payment Entry"}


class PawaPayPayoutRequest(Document):
	def validate(self):
		if self.country:
			self.pawapay_country_code = get_pawapay_country_code(self.country)
		if self.amount and self.amount <= 0:
			frappe.throw(_("Payout amount must be greater than zero."))
		if self.docstatus == 1 or self.enable_remote_initiation:
			self._validate_ready_for_remote_initiation()

	def on_submit(self):
		if self.enable_remote_initiation:
			frappe.enqueue(
				"frappe_pawapay.pawapay.services.payouts.initiate_payout_request",
				request_name=self.name,
				enqueue_after_commit=True,
			)

	@frappe.whitelist()
	def initiate_payout(self):
		frappe.only_for(("Accounts Manager", "System Manager"))
		from frappe_pawapay.pawapay.services.payouts import initiate_payout_request

		return initiate_payout_request(self.name)

	def _validate_ready_for_remote_initiation(self):
		if not self.get_password("wallet_number"):
			frappe.throw(_("Wallet Number is required before initiating a PawaPay payout."))
		if not self.provider:
			frappe.throw(_("Provider is required before initiating a PawaPay payout."))
		if not (self.party_type and self.party):
			frappe.throw(_("Party Type and Party are required before initiating a PawaPay payout."))
		if not (self.source_doctype and self.source_name):
			frappe.throw(_("Source document is required before initiating a PawaPay payout."))
		if self.source_doctype not in SUPPORTED_PAYOUT_REFERENCES:
			frappe.throw(_("PawaPay payouts do not yet support source document type {0}.").format(self.source_doctype))
		if not self.authorization_record:
			frappe.throw(_("Authorization Record is required before initiating a PawaPay payout."))
		settings = frappe.get_doc("PawaPay Settings", self.settings)
		validate_amount_against_active_configuration(
			settings,
			decimal_from(self.amount),
			self.currency,
			self.pawapay_country_code,
			"PAYOUT",
		)
