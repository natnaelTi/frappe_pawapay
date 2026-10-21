frappe.ui.form.on("PawaPay Payout Request", {
	refresh(frm) {
		if (frm.doc.docstatus === 1 && frm.doc.enable_remote_initiation && !frm.doc.transaction) {
			frm.add_custom_button(__("Initiate Payout"), () => {
				frm.call({
					doc: frm.doc,
					method: "initiate_payout",
					freeze: true,
					freeze_message: __("Initiating PawaPay payout..."),
					callback() {
						frm.reload_doc();
					},
				});
			});
		}
	},
});
