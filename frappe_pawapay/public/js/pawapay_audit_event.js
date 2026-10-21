frappe.ui.form.on("PawaPay Audit Event", {
	refresh(frm) {
		const retryable_events = [
			"Callback Failed",
			"Accounting Failed",
			"Accounting Conflict",
			"Reconciliation Retryable",
			"Reconciliation Failed",
			"Reconciliation Attempt",
		];
		if (!retryable_events.includes(frm.doc.event_type)) {
			return;
		}
		if (!frm.doc.transaction && !frm.doc.webhook_event) {
			return;
		}

		frm.add_custom_button(__("Retry Related Action"), () => {
			frm.call({
				doc: frm.doc,
				method: "retry_related_action",
				freeze: true,
				freeze_message: __("Retrying related PawaPay action..."),
				callback() {
					frm.reload_doc();
				},
			});
		});
	},
});
