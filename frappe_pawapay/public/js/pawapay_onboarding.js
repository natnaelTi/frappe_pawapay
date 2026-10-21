(function () {
	const STORAGE_KEY = "frappe_pawapay_onboarding_dismissed_until";

	function should_skip_prompt() {
		const dismissed_until = Number(localStorage.getItem(STORAGE_KEY) || 0);
		return dismissed_until && Date.now() < dismissed_until;
	}

	function remind_later() {
		localStorage.setItem(STORAGE_KEY, String(Date.now() + 24 * 60 * 60 * 1000));
	}

	function show_onboarding_dialog(context) {
		const dialog = new frappe.ui.Dialog({
			title: __("Configure PawaPay"),
			size: "large",
			fields: [
				{
					fieldtype: "HTML",
					fieldname: "intro",
					options: `<p>${__(
						"PawaPay created {0} collection account routes for {1}. Complete the remaining settings before enabling payments.",
						[context.collection_account_count, frappe.utils.escape_html(context.company || "")]
					)}</p><p><strong>${__("Callback URL")}:</strong><br>${frappe.utils.escape_html(
						context.callback_url || ""
					)}</p>`,
				},
				{ fieldtype: "Data", fieldname: "settings", label: __("Settings"), read_only: 1, default: context.settings },
				{
					fieldtype: "Select",
					fieldname: "environment",
					label: __("Environment"),
					options: "Sandbox\nProduction",
					default: context.environment || "Sandbox",
					reqd: 1,
				},
				{ fieldtype: "Password", fieldname: "api_token", label: __("API Token"), reqd: 1 },
				{ fieldtype: "Column Break" },
				{
					fieldtype: "Data",
					fieldname: "default_country",
					label: __("Default PawaPay Country Code"),
					default: context.default_country || "RWA",
					reqd: 1,
				},
				{
					fieldtype: "Select",
					fieldname: "default_language",
					label: __("Default Language"),
					options: "en\nfr",
					default: context.default_language || "en",
					reqd: 1,
				},
				{ fieldtype: "Section Break", label: __("FX") },
				{
					fieldtype: "Check",
					fieldname: "allow_source_currency_conversion",
					label: __("Allow Source Currency Conversion"),
					default: context.allow_source_currency_conversion || 0,
				},
				{
					fieldtype: "Select",
					fieldname: "fx_rate_source",
					label: __("FX Rate Source"),
					options: "Reference Document\nCurrency Exchange\nFrankfurter\nManual",
					default: context.fx_rate_source || "Reference Document",
				},
				{
					fieldtype: "Float",
					fieldname: "manual_exchange_rate",
					label: __("Manual Exchange Rate"),
					depends_on: "eval:doc.allow_source_currency_conversion && doc.fx_rate_source == 'Manual'",
				},
				{ fieldtype: "Section Break", label: __("Signing") },
				{
					fieldtype: "Check",
					fieldname: "signing_enabled",
					label: __("Sign Outgoing Requests"),
					default: context.signing_enabled || 0,
				},
				{ fieldtype: "Data", fieldname: "signing_key_id", label: __("Signing Key ID"), default: context.signing_key_id },
				{
					fieldtype: "Select",
					fieldname: "signing_algorithm",
					label: __("Signing Algorithm"),
					options: "rsa-pss-sha512\nrsa-v1_5-sha256\necdsa-p256-sha256",
					default: context.signing_algorithm || "rsa-pss-sha512",
				},
				{
					fieldtype: "Data",
					fieldname: "private_key_reference",
					label: __("Private Key Secret Reference"),
					default: context.private_key_reference,
				},
				{
					fieldtype: "Check",
					fieldname: "require_signed_callbacks",
					label: __("Require Signed Callbacks"),
					default: context.require_signed_callbacks || 0,
				},
				{ fieldtype: "Section Break" },
				{ fieldtype: "Check", fieldname: "enabled", label: __("Enable after saving"), default: 0 },
			],
			primary_action_label: __("Save PawaPay Settings"),
			primary_action(values) {
				frappe.call({
					method: "frappe_pawapay.pawapay.bootstrap.configure_placeholder_settings",
					args: { settings_name: context.settings, values },
					freeze: true,
					freeze_message: __("Saving PawaPay settings..."),
					callback() {
						localStorage.removeItem(STORAGE_KEY);
						dialog.hide();
						frappe.set_route("Form", "PawaPay Settings", context.settings);
					},
				});
			},
			secondary_action_label: __("Remind Me Tomorrow"),
			secondary_action() {
				remind_later();
				dialog.hide();
			},
		});
		dialog.show();
	}

	frappe.after_ajax(() => {
		if (should_skip_prompt()) {
			return;
		}
		if (!frappe.user_roles?.includes("System Manager")) {
			return;
		}
		frappe.call({
			method: "frappe_pawapay.pawapay.bootstrap.get_onboarding_context",
			callback(response) {
				const context = response.message;
				if (context && context.show) {
					show_onboarding_dialog(context);
				}
			},
		});
	});
})();
