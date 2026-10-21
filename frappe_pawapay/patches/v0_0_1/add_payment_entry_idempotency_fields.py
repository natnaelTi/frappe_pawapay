import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields


PAWAPAY_PAYMENT_ENTRY_EXTERNAL_ID_FIELD = "custom_pawapay_external_id"
PAWAPAY_PAYMENT_ENTRY_TRANSACTION_FIELD = "custom_pawapay_transaction"


def execute():
	create_custom_fields(_custom_fields(), update=True)
	frappe.clear_cache(doctype="Payment Entry")
	frappe.db.updatedb("Payment Entry")


def _custom_fields():
	return {
		"Payment Entry": [
			{
				"fieldname": PAWAPAY_PAYMENT_ENTRY_TRANSACTION_FIELD,
				"fieldtype": "Link",
				"label": "PawaPay Transaction",
				"options": "PawaPay Transaction",
				"insert_after": "reference_no",
				"read_only": 1,
				"no_copy": 1,
				"print_hide": 1,
				"allow_on_submit": 1,
			},
			{
				"fieldname": PAWAPAY_PAYMENT_ENTRY_EXTERNAL_ID_FIELD,
				"fieldtype": "Data",
				"label": "PawaPay External ID",
				"insert_after": PAWAPAY_PAYMENT_ENTRY_TRANSACTION_FIELD,
				"read_only": 1,
				"no_copy": 1,
				"print_hide": 1,
				"allow_on_submit": 1,
				"unique": 1,
			},
		]
	}
