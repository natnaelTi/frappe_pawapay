import frappe
from frappe import _
from frappe.model.document import Document

from frappe_pawapay.compatibility import (
	create_payment_gateway,
	ensure_collection_gateway_accounts,
	validate_collection_accounts,
)
from frappe_pawapay.pawapay.client import PawaPayClient
from frappe_pawapay.pawapay.services.checkout import get_or_create_payment_link
from frappe_pawapay.pawapay.models import get_base_url


class PawaPaySettings(Document):
	def validate(self):
		self.base_url = get_base_url(self.environment)
		self.gateway = f"PawaPay-{self.gateway_name}"
		self.callback_url = frappe.utils.get_url("/api/method/frappe_pawapay.api.pawapay_callback")
		self.statement_callback_url = self.callback_url
		if self.environment == "Production":
			if not frappe.utils.get_url().startswith("https://"):
				frappe.throw(_("Production PawaPay settings require an HTTPS site URL."))
			if not self.require_signed_callbacks:
				frappe.throw(_("Signed callbacks are required for Production PawaPay settings."))
		if self.default_checkout_expiry and not 3 <= int(self.default_checkout_expiry) <= 60:
			frappe.throw(_("Default checkout expiry must be between 3 and 60 minutes."))
		if self.enabled and not self.company:
			frappe.throw(_("Company is required before enabling PawaPay settings."))
		if self.enabled and not (self.pawapay_wallet_account and self.currency):
			has_collection_account = any(row.enabled for row in self.get("collection_accounts") or [])
			if not has_collection_account:
				frappe.throw(_("Company and at least one PawaPay collection account are required before enabling."))
		if self.signing_enabled and not (self.signing_key_id and self.private_key_reference):
			frappe.throw(_("Signing Key ID and Private Key Secret Reference are required to sign outgoing requests."))
		if self.allow_source_currency_conversion and self.fx_rate_source == "Manual" and not self.manual_exchange_rate:
			frappe.throw(_("Manual exchange rate is required when FX Rate Source is Manual."))
		validate_collection_accounts(self)

	def on_update(self):
		if not self.enabled:
			return
		create_payment_gateway(self.gateway, "PawaPay Settings", self.name)
		ensure_collection_gateway_accounts(self)

	def validate_transaction_currency(self, currency):
		if self.currency and currency != self.currency and not self.allow_source_currency_conversion:
			frappe.throw(_("PawaPay gateway {0} only accepts {1}.").format(self.name, self.currency))

	def get_payment_url(self, **kwargs):
		payment_request = kwargs.get("reference_docname") or kwargs.get("order_id")
		if kwargs.get("reference_doctype") != "Payment Request" or not payment_request:
			frappe.throw(_("PawaPay checkout requires an ERPNext Payment Request."))
		return get_or_create_payment_link(self, payment_request, **kwargs)

	@frappe.whitelist()
	def test_connection(self):
		payload = PawaPayClient(self).get_active_configuration()
		self.db_set("last_successful_credential_test", frappe.utils.now_datetime())
		return payload

	@frappe.whitelist()
	def sync_active_configuration(self):
		payload = PawaPayClient(self).get_active_configuration()
		self.db_set(
			{
				"active_configuration_cache": frappe.as_json(payload, indent=2),
				"last_configuration_sync": frappe.utils.now_datetime(),
			}
		)
		return payload

	@frappe.whitelist()
	def refresh_wallet_balances(self):
		payload = PawaPayClient(self).get_wallet_balances()
		return payload
