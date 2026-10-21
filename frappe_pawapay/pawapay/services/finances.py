import frappe

from frappe_pawapay.pawapay.client import PawaPayClient


def refresh_wallet_balances():
	for name in frappe.get_all("PawaPay Settings", filters={"enabled": 1}, pluck="name"):
		try:
			settings = frappe.get_doc("PawaPay Settings", name)
			payload = PawaPayClient(settings).get_wallet_balances()
			doc = frappe.get_doc(
				{
					"doctype": "PawaPay Wallet Snapshot",
					"settings": settings.name,
					"company": settings.company,
					"snapshot_at": frappe.utils.now_datetime(),
					"payload": frappe.as_json(payload, indent=2),
				}
			)
			doc.insert(ignore_permissions=True)
		except Exception:
			frappe.log_error("PawaPay wallet balance refresh failed")
