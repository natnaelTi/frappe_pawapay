import frappe


def after_install():
	ensure_workspace()


def ensure_workspace():
	if frappe.db.exists("Workspace", "PawaPay Payments"):
		return

	workspace = frappe.get_doc(
		{
			"doctype": "Workspace",
			"label": "PawaPay Payments",
			"title": "PawaPay Payments",
			"module": "PawaPay Payments",
			"public": 1,
			"for_user": "",
			"icon": "money-coins-1",
			"content": "[]",
			"links": [
				{"type": "Link", "link_type": "DocType", "label": "Settings", "link_to": "PawaPay Settings"},
				{"type": "Link", "link_type": "DocType", "label": "Payment Links", "link_to": "PawaPay Payment Link"},
				{"type": "Link", "link_type": "DocType", "label": "Transactions", "link_to": "PawaPay Transaction"},
				{"type": "Link", "link_type": "DocType", "label": "Webhook Events", "link_to": "PawaPay Webhook Event"},
				{"type": "Link", "link_type": "DocType", "label": "Audit Events", "link_to": "PawaPay Audit Event"},
				{"type": "Link", "link_type": "DocType", "label": "Payout Requests", "link_to": "PawaPay Payout Request"},
				{"type": "Link", "link_type": "DocType", "label": "Refund Requests", "link_to": "PawaPay Refund Request"},
				{"type": "Link", "link_type": "DocType", "label": "Wallet Snapshots", "link_to": "PawaPay Wallet Snapshot"},
			],
		}
	)
	workspace.insert(ignore_permissions=True)
