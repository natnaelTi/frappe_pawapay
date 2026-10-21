frappe.ui.form.on("PawaPay Refund Request", {
	refresh(frm) {
		if (frm.doc.docstatus === 1 && frm.doc.enable_remote_initiation && !frm.doc.transaction) {
			frm.add_custom_button(__("Initiate Refund"), () => {
				frm.call({
					doc: frm.doc,
					method: "initiate_refund",
					freeze: true,
					freeze_message: __("Initiating PawaPay refund..."),
					callback() {
						frm.reload_doc();
					},
				});
			});
		}
	},
});
