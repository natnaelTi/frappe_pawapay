frappe.ui.form.on("PawaPay Webhook Event", {
	refresh(frm) {
		if (!["Received", "Failed"].includes(frm.doc.processing_status)) {
			return;
		}
		if (!frm.doc.transaction || frm.doc.signature_status === "Invalid") {
			return;
		}

		frm.add_custom_button(__("Retry Processing"), () => {
			frm.call({
				doc: frm.doc,
				method: "retry_processing",
				freeze: true,
				freeze_message: __("Retrying webhook processing..."),
				callback() {
					frm.reload_doc();
				},
			});
		});
	},
});
