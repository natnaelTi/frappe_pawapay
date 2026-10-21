import frappe

from frappe_pawapay.pawapay.client import PawaPayClient


def refresh_enabled_settings():
	for name in frappe.get_all("PawaPay Settings", filters={"enabled": 1}, pluck="name"):
		try:
			settings = frappe.get_doc("PawaPay Settings", name)
			payload = PawaPayClient(settings).get_active_configuration()
			settings.db_set(
				{
					"active_configuration_cache": frappe.as_json(payload, indent=2),
					"last_configuration_sync": frappe.utils.now_datetime(),
				}
			)
		except Exception:
			frappe.log_error("PawaPay active configuration refresh failed")
