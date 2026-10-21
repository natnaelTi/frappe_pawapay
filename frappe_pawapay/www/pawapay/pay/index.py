import frappe

from frappe_pawapay.pawapay.services.checkout import get_public_payment_context


def get_context(context):
	token = frappe.form_dict.get("token") or frappe.request.path.rsplit("/", 1)[-1]
	context.no_cache = 1
	context.csrf_token = frappe.sessions.get_csrf_token()
	context.update(
		get_public_payment_context(
			token,
			country=frappe.form_dict.get("country"),
			currency=frappe.form_dict.get("currency"),
		)
	)
	context.error_message = frappe.form_dict.get("error")
	if context.get("checkout_standalone"):
		context.body_class = "pawapay-checkout-body"
	return context
