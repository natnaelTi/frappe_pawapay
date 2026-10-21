app_name = "frappe_pawapay"
app_title = "PawaPay Payments"
app_publisher = "Selfmade Cloud Solutions"
app_description = "PawaPay Payments gateway for Frappe and ERPNext."
app_email = "dev@selfmadecs.com"
app_license = "mit"
required_apps = ["erpnext", "payments"]

web_include_css = "/assets/frappe_pawapay/css/frappe_pawapay.css"

doctype_js = {
	"PawaPay Settings": "public/js/pawapay_settings.js",
	"PawaPay Audit Event": "public/js/pawapay_audit_event.js",
	"PawaPay Webhook Event": "public/js/pawapay_webhook_event.js",
}

after_install = "frappe_pawapay.install.after_install"

scheduler_events = {
	"cron": {
		"*/5 * * * *": [
			"frappe_pawapay.pawapay.services.reconciliation.reconcile_pending_transactions",
			"frappe_pawapay.pawapay.callbacks.retry_received_webhooks",
		],
		"0 * * * *": [
			"frappe_pawapay.pawapay.services.configuration.refresh_enabled_settings",
			"frappe_pawapay.pawapay.services.finances.refresh_wallet_balances",
		],
	}
}

website_route_rules = [
	{"from_route": "/pawapay/pay/<token>", "to_route": "pawapay/pay"},
	{"from_route": "/pawapay/return", "to_route": "pawapay/return"},
]

fixtures = []
