from urllib.parse import quote

import frappe
from frappe import _

from frappe_pawapay.pawapay.callbacks import receive_callback
from frappe_pawapay.pawapay.exceptions import PawaPayError
from frappe_pawapay.pawapay.services.checkout import create_checkout_for_link, get_public_payment_context
from frappe_pawapay.pawapay.signing import PawaPaySigningError


@frappe.whitelist(allow_guest=True)
def pawapay_callback():
	return receive_callback()


@frappe.whitelist(allow_guest=True, methods=["POST"])
def start_checkout(token: str):
	frappe.local.response["type"] = "redirect"
	try:
		frappe.local.response["location"] = create_checkout_for_link(
			token,
			country=frappe.form_dict.get("country"),
			currency=frappe.form_dict.get("currency"),
		)
	except (frappe.ValidationError, PawaPayError, PawaPaySigningError) as exc:
		message = str(exc)[:300]
		frappe.local.response["location"] = f"/pawapay/pay/{token}?error={quote(message)}"


@frappe.whitelist(allow_guest=True, methods=["GET"])
def get_checkout_csrf_token():
	"""Refresh the guest session token without depending on an integrating app."""
	return {"csrf_token": frappe.sessions.get_csrf_token()}


@frappe.whitelist(allow_guest=True)
def payment_status(token: str):
	context = get_public_payment_context(token)
	return {
		"status": context.get("status"),
		"amount": context.get("amount"),
		"currency": context.get("currency"),
		"reference": context.get("reference"),
	}
