frappe.ui.form.on("PawaPay Transaction", {
	refresh(frm) {
		if (frm.doc.docstatus === 2) {
			return;
		}

		frm.add_custom_button(__("Check Status"), () => {
			frm.call({
				doc: frm.doc,
				method: "check_status",
				freeze: true,
				freeze_message: __("Checking PawaPay status..."),
				callback() {
					frm.reload_doc();
				},
			});
		});

		if (frm.doc.accounting_status !== "POSTED" && frm.doc.lifecycle_status === "COMPLETED") {
			frm.add_custom_button(__("Retry Accounting"), () => {
				frm.call({
					doc: frm.doc,
					method: "retry_accounting",
					freeze: true,
					freeze_message: __("Retrying accounting finalization..."),
					callback() {
						frm.reload_doc();
					},
				});
			});
		}

		if (["Payout", "Refund"].includes(frm.doc.operation_type) && frm.doc.lifecycle_status === "ENQUEUED") {
			frm.add_custom_button(__("Cancel Enqueued"), () => {
				frm.call({
					doc: frm.doc,
					method: "cancel_enqueued",
					freeze: true,
					freeze_message: __("Cancelling enqueued PawaPay transaction..."),
					callback() {
						frm.reload_doc();
					},
				});
			});
		}

		if (["Payout", "Refund"].includes(frm.doc.operation_type)) {
			frm.add_custom_button(__("Resend Callback"), () => {
				frm.call({
					doc: frm.doc,
					method: "resend_callback",
					freeze: true,
					freeze_message: __("Requesting PawaPay callback resend..."),
					callback() {
						frm.reload_doc();
					},
				});
			});
		}
	},
});
