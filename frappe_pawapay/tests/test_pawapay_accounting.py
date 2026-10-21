from __future__ import annotations

from decimal import Decimal

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime, nowdate

from erpnext.accounts.test.accounts_mixin import AccountsTestMixin

from frappe_pawapay.compatibility import get_payment_request_source_amount
from frappe_pawapay.pawapay.services.accounting import finalize_inbound_payment, finalize_outbound_payment
from frappe_pawapay.pawapay.services.checkout import _can_pay, resolve_payment_link, token_hash


class PawaPayAccountingFixtureTests(AccountsTestMixin, IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.create_company(company_name="PawaPay Test Company", abbr="PTC")
		self.item = self._make_item()
		self.create_customer(
			customer_name="PawaPay Test Customer",
			default_account=self.debit_to,
			company=self.company,
		)
		self.create_supplier(supplier_name="PawaPay Test Supplier")
		self.mode_of_payment = self._ensure_mode_of_payment()
		self.settings = self._make_settings()

	def tearDown(self):
		frappe.db.rollback()
		super().tearDown()

	def _ensure_mode_of_payment(self):
		name = "PawaPay Test Mobile Money"
		if frappe.db.exists("Mode of Payment", name):
			return name
		doc = frappe.get_doc(
			{
				"doctype": "Mode of Payment",
				"mode_of_payment": name,
				"type": "Bank",
				"accounts": [{"company": self.company, "default_account": self.bank}],
			}
		)
		doc.insert(ignore_permissions=True)
		return doc.name

	def _make_item(self):
		item_code = f"PawaPay Test Item {frappe.generate_hash(length=8)}"
		doc = frappe.get_doc(
			{
				"doctype": "Item",
				"item_code": item_code,
				"item_name": item_code,
				"item_group": frappe.db.get_value("Item Group", {"is_group": 0}, "name") or "All Item Groups",
				"stock_uom": "Nos",
				"is_stock_item": 0,
				"is_sales_item": 1,
				"is_purchase_item": 1,
				"include_item_in_manufacturing": 0,
			}
		)
		doc.insert(ignore_permissions=True)
		return doc.name

	def _make_settings(self):
		doc = frappe.get_doc(
			{
				"doctype": "PawaPay Settings",
				"gateway_name": f"PawaPay Test Accounting {frappe.generate_hash(length=10)}",
				"company": self.company,
				"environment": "Sandbox",
				"api_token": "test-token",
				"currency": "INR",
				"pawapay_wallet_account": self.bank,
				"mode_of_payment": self.mode_of_payment,
				"default_country": "RWA",
				"default_language": "en",
				"reconciliation_enabled": 1,
				"connection_timeout": 10,
				"read_timeout": 30,
				"active_configuration_cache_ttl": 3600,
				"public_key_cache_ttl": 900,
			}
		)
		doc.insert(ignore_permissions=True)
		return doc.name

	def _make_sales_invoice(self, amount=1000, currency="INR", conversion_rate=1):
		doc = frappe.get_doc(
			{
				"doctype": "Sales Invoice",
				"company": self.company,
				"customer": self.customer,
				"debit_to": self.debit_to,
				"currency": currency,
				"conversion_rate": conversion_rate,
				"price_list_currency": currency,
				"plc_conversion_rate": 1,
				"posting_date": nowdate(),
				"items": [
					{
						"item_code": self.item,
						"qty": 1,
						"uom": "Nos",
						"stock_uom": "Nos",
						"conversion_factor": 1,
						"rate": amount,
						"price_list_rate": amount,
						"income_account": self.income_account,
						"expense_account": self.expense_account,
						"cost_center": self.cost_center,
					}
				],
			}
		)
		doc.insert(ignore_permissions=True)
		doc.submit()
		return doc

	def _make_purchase_invoice(self, amount=1000):
		doc = frappe.get_doc(
			{
				"doctype": "Purchase Invoice",
				"company": self.company,
				"supplier": self.supplier,
				"credit_to": self.creditors,
				"currency": "INR",
				"conversion_rate": 1,
				"posting_date": nowdate(),
				"items": [
					{
						"item_code": self.item,
						"qty": 1,
						"uom": "Nos",
						"stock_uom": "Nos",
						"conversion_factor": 1,
						"rate": amount,
						"price_list_rate": amount,
						"expense_account": self.expense_account,
						"cost_center": self.cost_center,
					}
				],
			}
		)
		doc.insert(ignore_permissions=True)
		doc.submit()
		return doc

	def _make_payment_request(self, invoice):
		doc = frappe.get_doc(
			{
				"doctype": "Payment Request",
				"payment_request_type": "Inward",
				"transaction_date": nowdate(),
				"naming_series": "ACC-PRQ-.YYYY.-",
				"company": self.company,
				"mode_of_payment": self.mode_of_payment,
				"party_type": "Customer",
				"party": self.customer,
				"reference_doctype": invoice.doctype,
				"reference_name": invoice.name,
				"grand_total": invoice.grand_total,
				"outstanding_amount": invoice.grand_total,
				"currency": invoice.currency,
				"party_account_currency": frappe.get_cached_value("Account", self.debit_to, "account_currency"),
				"payment_account": self.bank,
				"payment_gateway": "PawaPay Test Gateway",
				"payment_channel": "Email",
				"email_to": "payer@example.com",
				"subject": f"Payment Request for {invoice.name}",
				"message": "Please pay.",
				"status": "Initiated",
				"mute_email": 1,
			}
		)
		doc.insert(ignore_permissions=True)
		doc.submit()
		return doc

	def _make_payment_link(self, payment_request):
		doc = frappe.get_doc(
			{
				"doctype": "PawaPay Payment Link",
				"token_hash": frappe.generate_hash(length=32),
				"settings": self.settings,
				"payment_request": payment_request.name,
				"reference_doctype": payment_request.reference_doctype,
				"reference_name": payment_request.reference_name,
				"company": self.company,
				"expected_amount": payment_request.outstanding_amount,
				"currency": payment_request.currency,
				"status": "Active",
			}
		)
		doc.insert(ignore_permissions=True)
		return doc

	def _make_transaction(self, **overrides):
		values = {
			"doctype": "PawaPay Transaction",
			"external_id": frappe.generate_hash(length=32),
			"operation_type": "Checkout",
			"settings": self.settings,
			"environment": "Sandbox",
			"initiation_status": "ACCEPTED",
			"lifecycle_status": "COMPLETED",
			"accounting_status": "PENDING",
			"review_status": "NORMAL",
			"company": self.company,
			"amount": Decimal("1000"),
			"currency": "INR",
			"source_amount": Decimal("1000"),
			"source_currency": "INR",
			"country": "RWA",
			"provider": "MTN_MOMO_RWA",
			"provider_transaction_id": frappe.generate_hash(length=20),
			"client_reference_id": "PawaPay Test Reference",
			"collection_account": self.bank,
			"collection_mode_of_payment": self.mode_of_payment,
			"completed_at": frappe.utils.now_datetime(),
		}
		values.update(overrides)
		doc = frappe.get_doc(values)
		doc.insert(ignore_permissions=True)
		return doc

	def test_completed_checkout_creates_receive_payment_entry_once(self):
		invoice = self._make_sales_invoice()
		payment_request = self._make_payment_request(invoice)
		payment_link = self._make_payment_link(payment_request)
		transaction = self._make_transaction(
			payment_request=payment_request.name,
			payment_link=payment_link.name,
			client_reference_id=payment_request.name,
			reference_doctype=invoice.doctype,
			reference_name=invoice.name,
		)

		finalize_inbound_payment(transaction.name)
		transaction.reload()
		first_payment_entry = transaction.payment_entry
		finalize_inbound_payment(transaction.name)
		transaction.reload()

		self.assertEqual(transaction.accounting_status, "POSTED")
		self.assertEqual(transaction.payment_entry, first_payment_entry)
		self.assertEqual(
			frappe.db.count("Payment Entry", {"custom_pawapay_external_id": transaction.external_id}),
			1,
		)
		payment_entry = frappe.get_doc("Payment Entry", transaction.payment_entry)
		self.assertEqual(payment_entry.docstatus, 1)
		self.assertEqual(payment_entry.payment_type, "Receive")
		self.assertEqual(payment_entry.paid_to, self.bank)
		self.assertEqual(payment_entry.reference_no, transaction.provider_transaction_id)
		self.assertEqual(payment_entry.custom_pawapay_transaction, transaction.name)
		self.assertEqual(payment_entry.references[0].reference_doctype, "Sales Invoice")
		self.assertEqual(payment_entry.references[0].reference_name, invoice.name)
		self.assertEqual(frappe.db.get_value("PawaPay Payment Link", payment_link.name, "status"), "Paid")

	def test_foreign_currency_payment_request_amount_is_normalized_to_source_currency(self):
		invoice = self._make_sales_invoice(amount=10, currency="USD", conversion_rate=80)
		payment_request = self._make_payment_request(invoice)

		self.assertEqual(Decimal(str(payment_request.outstanding_amount)), Decimal("800"))
		self.assertEqual(Decimal(str(get_payment_request_source_amount(payment_request))), Decimal("10"))

	def test_gateway_accepts_source_currency_only_when_conversion_is_enabled(self):
		settings = frappe.get_doc("PawaPay Settings", self.settings)
		with self.assertRaises(frappe.ValidationError):
			settings.validate_transaction_currency("USD")

		settings.allow_source_currency_conversion = 1
		settings.validate_transaction_currency("USD")

	def test_expired_payment_link_cannot_be_reused_or_paid(self):
		invoice = self._make_sales_invoice()
		payment_request = self._make_payment_request(invoice)
		payment_link = self._make_payment_link(payment_request)
		token = frappe.generate_hash(length=32)
		payment_link.token_hash = token_hash(token)
		payment_link.public_token = token
		payment_link.business_expiry = add_to_date(now_datetime(), seconds=-1)
		payment_link.save(ignore_permissions=True)

		resolved = resolve_payment_link(token)
		self.assertEqual(resolved.status, "Expired")
		self.assertFalse(_can_pay(payment_request, resolved))

	def test_duplicate_callback_transaction_reuses_existing_payment_entry(self):
		invoice = self._make_sales_invoice()
		payment_request = self._make_payment_request(invoice)
		payment_link = self._make_payment_link(payment_request)
		external_id = frappe.generate_hash(length=32)
		first = self._make_transaction(
			external_id=external_id,
			payment_request=payment_request.name,
			payment_link=payment_link.name,
			client_reference_id=payment_request.name,
			reference_doctype=invoice.doctype,
			reference_name=invoice.name,
		)
		second = self._make_transaction(
			external_id=frappe.generate_hash(length=32),
			payment_request=payment_request.name,
			payment_link=payment_link.name,
			client_reference_id=payment_request.name,
			reference_doctype=invoice.doctype,
			reference_name=invoice.name,
		)

		finalize_inbound_payment(first.name)
		first.reload()
		frappe.db.set_value(
			"Payment Entry",
			first.payment_entry,
			"custom_pawapay_external_id",
			second.external_id,
		)
		finalize_inbound_payment(second.name)
		second.reload()

		self.assertEqual(second.accounting_status, "POSTED")
		self.assertEqual(second.payment_entry, first.payment_entry)
		self.assertEqual(
			frappe.db.count("Payment Entry", {"custom_pawapay_external_id": second.external_id}),
			1,
		)

	def test_completed_refund_creates_outbound_payment_entry(self):
		invoice = self._make_sales_invoice()
		original = self._make_transaction(
			operation_type="Deposit",
			amount=Decimal("1000"),
			currency="INR",
			source_amount=Decimal("1000"),
			source_currency="INR",
			reference_doctype=invoice.doctype,
			reference_name=invoice.name,
			client_reference_id=invoice.name,
			underlying_deposit_id=frappe.generate_hash(length=32),
		)
		refund_request = frappe.get_doc(
			{
				"doctype": "PawaPay Refund Request",
				"settings": self.settings,
				"company": self.company,
				"original_transaction": original.name,
				"refund_type": "Partial",
				"currency": "INR",
				"amount": 250,
				"accounting_reference_doctype": "Sales Invoice",
				"accounting_reference_name": invoice.name,
				"status": "Initiated",
			}
		)
		refund_request.insert(ignore_permissions=True)
		transaction = self._make_transaction(
			operation_type="Refund",
			amount=Decimal("250"),
			source_amount=None,
			source_currency=None,
			client_reference_id=refund_request.name,
			reference_doctype="Sales Invoice",
			reference_name=invoice.name,
			refund_request=refund_request.name,
		)

		finalize_outbound_payment(transaction.name)
		transaction.reload()
		refund_request.reload()

		self.assertEqual(transaction.accounting_status, "POSTED")
		self.assertEqual(refund_request.status, "Completed")
		self.assertEqual(refund_request.payment_entry, transaction.payment_entry)
		payment_entry = frappe.get_doc("Payment Entry", transaction.payment_entry)
		self.assertEqual(payment_entry.docstatus, 1)
		self.assertEqual(payment_entry.payment_type, "Pay")
		self.assertEqual(payment_entry.party_type, "Customer")
		self.assertEqual(payment_entry.party, self.customer)
		self.assertEqual(payment_entry.paid_from, self.bank)
		self.assertEqual(payment_entry.paid_to, self.debit_to)
		self.assertEqual(payment_entry.custom_pawapay_transaction, transaction.name)

	def test_completed_payout_creates_outbound_payment_entry(self):
		purchase_invoice = self._make_purchase_invoice()
		payout_request = frappe.get_doc(
			{
				"doctype": "PawaPay Payout Request",
				"settings": self.settings,
				"company": self.company,
				"country": "Rwanda",
				"currency": "INR",
				"amount": 300,
				"pawapay_wallet_account": self.bank,
				"party_type": "Supplier",
				"party": self.supplier,
				"source_doctype": "Purchase Invoice",
				"source_name": purchase_invoice.name,
				"provider": "MTN_MOMO_RWA",
				"authorization_record": "Approved by test",
				"status": "Initiated",
			}
		)
		payout_request.insert(ignore_permissions=True)
		transaction = self._make_transaction(
			operation_type="Payout",
			amount=Decimal("300"),
			source_amount=None,
			source_currency=None,
			client_reference_id=payout_request.name,
			reference_doctype="Purchase Invoice",
			reference_name=purchase_invoice.name,
			payout_request=payout_request.name,
		)

		finalize_outbound_payment(transaction.name)
		transaction.reload()
		payout_request.reload()

		self.assertEqual(transaction.accounting_status, "POSTED")
		self.assertEqual(payout_request.status, "Completed")
		self.assertEqual(payout_request.payment_entry, transaction.payment_entry)
		payment_entry = frappe.get_doc("Payment Entry", transaction.payment_entry)
		self.assertEqual(payment_entry.docstatus, 1)
		self.assertEqual(payment_entry.payment_type, "Pay")
		self.assertEqual(payment_entry.party_type, "Supplier")
		self.assertEqual(payment_entry.party, self.supplier)
		self.assertEqual(payment_entry.paid_from, self.bank)
		self.assertEqual(payment_entry.paid_to, self.creditors)
		self.assertEqual(payment_entry.references[0].reference_doctype, "Purchase Invoice")
		self.assertEqual(payment_entry.references[0].reference_name, purchase_invoice.name)

	def test_inbound_amount_mismatch_marks_conflict_without_payment_entry(self):
		invoice = self._make_sales_invoice()
		payment_request = self._make_payment_request(invoice)
		transaction = self._make_transaction(
			amount=Decimal("900"),
			source_amount=Decimal("900"),
			payment_request=payment_request.name,
			client_reference_id=payment_request.name,
			reference_doctype=invoice.doctype,
			reference_name=invoice.name,
		)

		finalize_inbound_payment(transaction.name)
		transaction.reload()

		self.assertEqual(transaction.accounting_status, "CONFLICT")
		self.assertEqual(transaction.review_status, "NEEDS_ATTENTION")
		self.assertFalse(transaction.payment_entry)
		self.assertTrue(
			frappe.db.exists(
				"PawaPay Audit Event",
				{"event_type": "Accounting Conflict", "transaction": transaction.name, "status": "Conflict"},
			)
		)


if __name__ == "__main__":
	import unittest

	unittest.main()
