import frappe

from frappe_pawapay.pawapay.services.checkout import get_payment_page_presentation


def get_context(context):
	context.no_cache = 1
	checkout_code = frappe.form_dict.get("checkoutCode") or frappe.form_dict.get("checkout_code")
	transaction_name = None
	if checkout_code:
		transaction_name = frappe.db.get_value("PawaPay Transaction", {"checkout_code": checkout_code}, "name")
	context.transaction = frappe.get_doc("PawaPay Transaction", transaction_name) if transaction_name else None
	if context.transaction and context.transaction.payment_request:
		request = frappe.get_doc("Payment Request", context.transaction.payment_request)
		payment_link = None
		if context.transaction.payment_link and frappe.db.exists(
			"PawaPay Payment Link", context.transaction.payment_link
		):
			payment_link = frappe.get_doc("PawaPay Payment Link", context.transaction.payment_link)
		context.update(get_payment_page_presentation(request, payment_link))
	if context.get("checkout_standalone"):
		context.body_class = "pawapay-checkout-body"
	return context
