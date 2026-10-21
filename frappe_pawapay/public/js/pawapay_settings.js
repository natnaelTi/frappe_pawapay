frappe.ui.form.on("PawaPay Settings", {
	refresh(frm) {
		if (frm.is_new()) {
			return;
		}

		frm.add_custom_button(
			__("Test Connection"),
			() => call_settings_method(frm, "test_connection", {
				freeze_message: __("Testing PawaPay connection..."),
				success_title: __("Connection Successful"),
				success_message: __("PawaPay returned an active configuration response."),
			}),
			__("PawaPay")
		);

		frm.add_custom_button(
			__("Sync Active Configuration"),
			() => call_settings_method(frm, "sync_active_configuration", {
				freeze_message: __("Syncing PawaPay active configuration..."),
				success_title: __("Active Configuration Synced"),
				success_message: __("This PawaPay Settings record now has the latest active configuration cache."),
			}),
			__("PawaPay")
		);

		frm.add_custom_button(
			__("Refresh Wallet Balances"),
			() => call_settings_method(frm, "refresh_wallet_balances", {
				freeze_message: __("Refreshing PawaPay wallet balances..."),
				success_title: __("Wallet Balances Refreshed"),
				show_payload: true,
			}),
			__("PawaPay")
		);
	},
});

function call_settings_method(frm, method, options) {
	if (frm.is_dirty()) {
		frappe.msgprint({
			title: __("Save Required"),
			message: __("Save this PawaPay Settings record before testing or syncing with PawaPay."),
			indicator: "orange",
		});
		return Promise.resolve();
	}

	return frm
		.call({
			method,
			doc: frm.doc,
			freeze: true,
			freeze_message: options.freeze_message,
		})
		.then((response) => {
			const message = options.show_payload
				? `<pre>${frappe.utils.escape_html(JSON.stringify(response.message || {}, null, 2))}</pre>`
				: options.success_message;

			frappe.msgprint({
				title: options.success_title,
				message,
				indicator: "green",
				wide: Boolean(options.show_payload),
			});

			return frm.reload_doc();
		});
}
