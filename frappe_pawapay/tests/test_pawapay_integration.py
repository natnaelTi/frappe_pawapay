from __future__ import annotations

import hashlib
import json
from decimal import Decimal
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase

from frappe_pawapay.pawapay import bootstrap as pawapay_bootstrap
from frappe_pawapay.pawapay.callbacks import process_webhook, receive_callback
from frappe_pawapay.pawapay.services.reconciliation import reconcile_transaction


class FakeRequest:
	method = "POST"
	path = "/api/method/frappe_pawapay.api.pawapay_callback"
	query_string = b""
	headers = {}

	def __init__(self, body: bytes, headers: dict | None = None):
		self._body = body
		self.headers = headers or {}

	def get_data(self):
		return self._body


class FakePawaPayClient:
	def __init__(self, settings, payout_payload=None, refund_payload=None):
		self.settings = settings
		self.payout_payload = payout_payload
		self.refund_payload = refund_payload
		self.resend_response = {"status": "ACCEPTED"}

	def get_payout(self, external_id):
		return self.payout_payload

	def get_refund(self, external_id):
		return self.refund_payload

	def resend_payout_callback(self, external_id):
		return self.resend_response

	def resend_refund_callback(self, external_id):
		return self.resend_response


class PawaPayDatabaseIntegrationTests(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		self.company = frappe.db.get_value("Company", {}, "name")
		if not self.company:
			self.skipTest("A Company is required for PawaPay integration tests.")
		self.settings = self._make_settings()

	def test_bootstrap_creates_placeholder_settings_with_collection_accounts(self):
		placeholder_name = f"PawaPay Test Auto Setup {frappe.generate_hash(length=8)}"
		with patch("frappe_pawapay.pawapay.bootstrap.PLACEHOLDER_SETTINGS_NAME", placeholder_name), patch(
			"frappe_pawapay.pawapay.bootstrap._get_default_company", return_value=self.company
		):
			settings_name = pawapay_bootstrap.bootstrap_default_configuration()

		self.assertEqual(settings_name, placeholder_name)
		settings = frappe.get_doc("PawaPay Settings", settings_name)
		self.assertFalse(settings.enabled)
		self.assertEqual(settings.get_password("api_token"), pawapay_bootstrap.PLACEHOLDER_API_TOKEN)
		self.assertEqual(len(settings.collection_accounts), len(pawapay_bootstrap.PAWAPAY_COLLECTION_ROUTES))
		self.assertEqual(
			{row.pawapay_country_code for row in settings.collection_accounts},
			{route["country_code"] for route in pawapay_bootstrap.PAWAPAY_COLLECTION_ROUTES},
		)
		for row in settings.collection_accounts:
			self.assertTrue(row.wallet_account)
			self.assertEqual(
				frappe.db.get_value("Account", row.wallet_account, "account_currency"),
				row.currency,
			)

	def _make_settings(self):
		name = f"Test PawaPay {frappe.generate_hash(length=10)}"
		doc = frappe.get_doc(
			{
				"doctype": "PawaPay Settings",
				"gateway_name": name,
				"company": self.company,
				"environment": "Sandbox",
				"api_token": "test-token",
				"currency": "USD",
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

	def _make_transaction(self, **overrides):
		values = {
			"doctype": "PawaPay Transaction",
			"external_id": frappe.generate_hash(length=32),
			"operation_type": "Payout",
			"settings": self.settings,
			"environment": "Sandbox",
			"initiation_status": "ACCEPTED",
			"lifecycle_status": "ENQUEUED",
			"accounting_status": "PENDING",
			"review_status": "NORMAL",
			"company": self.company,
			"amount": Decimal("1500"),
			"currency": "USD",
			"country": "RWA",
			"provider": "MTN_MOMO_RWA",
			"client_reference_id": "PPR-TEST-0001",
		}
		values.update(overrides)
		doc = frappe.get_doc(values)
		doc.insert(ignore_permissions=True)
		return doc

	def _make_webhook(self, transaction, payload, signature_status="Verified"):
		doc = frappe.get_doc(
			{
				"doctype": "PawaPay Webhook Event",
				"fingerprint": frappe.generate_hash(length=32),
				"operation_type": transaction.operation_type,
				"external_id": transaction.external_id,
				"received_at": frappe.utils.now_datetime(),
				"settings": self.settings,
				"environment": "Sandbox",
				"signature_status": signature_status,
				"raw_body_hash": hashlib.sha256(json.dumps(payload).encode()).hexdigest(),
				"redacted_payload": frappe.as_json(payload),
				"processing_status": "Received",
				"transaction": transaction.name,
			}
		)
		doc.insert(ignore_permissions=True)
		return doc

	def test_process_verified_payout_webhook_updates_transaction_and_enqueues_outbound_accounting(self):
		transaction = self._make_transaction()
		payload = {
			"payoutId": transaction.external_id,
			"status": "COMPLETED",
			"amount": "1500",
			"currency": "USD",
			"country": "RWA",
			"clientReferenceId": transaction.client_reference_id,
			"providerTransactionId": "provider-payout-1",
		}
		webhook = self._make_webhook(transaction, payload)

		with patch("frappe_pawapay.pawapay.callbacks.frappe.enqueue") as enqueue:
			process_webhook(webhook.name)

		transaction.reload()
		webhook.reload()
		self.assertEqual(webhook.processing_status, "Processed")
		self.assertEqual(transaction.lifecycle_status, "COMPLETED")
		self.assertEqual(transaction.provider_transaction_id, "provider-payout-1")
		self.assertEqual(transaction.callback_count, 1)
		self.assertTrue(
			frappe.db.exists(
				"PawaPay Audit Event",
				{
					"event_type": "Webhook Processed",
					"transaction": transaction.name,
					"webhook_event": webhook.name,
					"status": "COMPLETED",
				},
			)
		)
		self.assertTrue(
			frappe.db.exists(
				"PawaPay Audit Event",
				{"event_type": "Accounting Queued", "transaction": transaction.name},
			)
		)
		enqueue.assert_called_once()
		self.assertEqual(
			enqueue.call_args.args[0],
			"frappe_pawapay.pawapay.services.accounting.finalize_outbound_payment",
		)
		self.assertEqual(enqueue.call_args.kwargs["transaction_name"], transaction.name)

	def test_retry_failed_webhook_reprocesses_and_records_operator_audit(self):
		transaction = self._make_transaction()
		payload = {
			"payoutId": transaction.external_id,
			"status": "COMPLETED",
			"amount": "1500",
			"currency": "USD",
			"country": "RWA",
			"clientReferenceId": transaction.client_reference_id,
			"providerTransactionId": "provider-payout-retry",
		}
		webhook = self._make_webhook(transaction, payload)
		webhook.db_set({"processing_status": "Failed", "last_error": "Transient processing failure"})
		webhook.reload()

		with patch("frappe_pawapay.pawapay.callbacks.frappe.enqueue") as enqueue:
			webhook.retry_processing()

		transaction.reload()
		webhook.reload()
		self.assertEqual(webhook.processing_status, "Processed")
		self.assertEqual(transaction.lifecycle_status, "COMPLETED")
		self.assertEqual(transaction.provider_transaction_id, "provider-payout-retry")
		self.assertTrue(
			frappe.db.exists(
				"PawaPay Audit Event",
				{"event_type": "Reconciliation Retryable", "webhook_event": webhook.name, "status": "Webhook Retry Requested"},
			)
		)
		enqueue.assert_called_once()

	def test_process_failed_refund_webhook_marks_refund_request_failed(self):
		original = self._make_transaction(
			operation_type="Deposit",
			lifecycle_status="COMPLETED",
			external_id=frappe.generate_hash(length=32),
			client_reference_id="PR-TEST-0001",
		)
		refund_request = frappe.get_doc(
			{
				"doctype": "PawaPay Refund Request",
				"settings": self.settings,
				"company": self.company,
				"original_transaction": original.name,
				"refund_type": "Partial",
				"currency": "USD",
				"amount": 500,
				"status": "Initiated",
			}
		)
		refund_request.insert(ignore_permissions=True)
		refund_transaction = self._make_transaction(
			operation_type="Refund",
			lifecycle_status="PROCESSING",
			amount=Decimal("500"),
			client_reference_id=refund_request.name,
			refund_request=refund_request.name,
		)
		payload = {
			"refundId": refund_transaction.external_id,
			"status": "FAILED",
			"failureReason": {"failureCode": "REFUND_FAILED", "failureMessage": "Provider rejected refund"},
		}
		webhook = self._make_webhook(refund_transaction, payload)

		process_webhook(webhook.name)

		refund_transaction.reload()
		refund_request.reload()
		webhook.reload()
		self.assertEqual(webhook.processing_status, "Processed")
		self.assertEqual(refund_transaction.lifecycle_status, "FAILED")
		self.assertEqual(refund_transaction.failure_code, "REFUND_FAILED")
		self.assertEqual(refund_request.status, "Failed")
		self.assertEqual(refund_request.last_error, "Provider rejected refund")

	def test_receive_callback_persists_webhook_and_marks_duplicate_fingerprint(self):
		transaction = self._make_transaction()
		payload = {
			"payoutId": transaction.external_id,
			"status": "ENQUEUED",
		}
		body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
		request = FakeRequest(body)
		expected_fingerprint = hashlib.sha256(body + b"").hexdigest()

		with patch("frappe_pawapay.pawapay.callbacks.frappe.request", request), patch(
			"frappe_pawapay.pawapay.callbacks.verify_callback_signature", return_value=(False, None)
		), patch("frappe_pawapay.pawapay.callbacks.frappe.enqueue") as enqueue:
			first_response = receive_callback()
			second_response = receive_callback()

		self.assertEqual(first_response, {"ok": True})
		self.assertEqual(second_response, {"ok": True, "duplicate": True})
		webhook_name = frappe.db.get_value("PawaPay Webhook Event", {"fingerprint": expected_fingerprint}, "name")
		self.assertTrue(webhook_name)
		webhook = frappe.get_doc("PawaPay Webhook Event", webhook_name)
		self.assertEqual(webhook.processing_status, "Duplicate")
		self.assertEqual(webhook.transaction, transaction.name)
		self.assertTrue(
			frappe.db.exists(
				"PawaPay Audit Event",
				{"event_type": "Callback Duplicate", "webhook_event": webhook.name, "status": "Duplicate"},
			)
		)
		enqueue.assert_called_once()

	def test_reconcile_completed_payout_updates_transaction_and_enqueues_accounting(self):
		transaction = self._make_transaction(lifecycle_status="PROCESSING")
		payload = {
			"status": "FOUND",
			"data": {
				"payoutId": transaction.external_id,
				"status": "COMPLETED",
				"amount": "1500",
				"currency": "USD",
				"country": "RWA",
				"clientReferenceId": transaction.client_reference_id,
				"providerTransactionId": "provider-payout-2",
			},
		}
		fake_client = lambda settings: FakePawaPayClient(settings, payout_payload=payload)

		with patch("frappe_pawapay.pawapay.services.reconciliation.PawaPayClient", side_effect=fake_client), patch(
			"frappe_pawapay.pawapay.services.reconciliation.frappe.enqueue"
		) as enqueue:
			reconcile_transaction(transaction.name)

		transaction.reload()
		self.assertEqual(transaction.lifecycle_status, "COMPLETED")
		self.assertEqual(transaction.provider_transaction_id, "provider-payout-2")
		self.assertEqual(transaction.reconciliation_attempt_count, 1)
		self.assertTrue(
			frappe.db.exists(
				"PawaPay Audit Event",
				{"event_type": "Reconciliation Completed", "transaction": transaction.name, "status": "COMPLETED"},
			)
		)
		enqueue.assert_called_once()
		self.assertEqual(
			enqueue.call_args.args[0],
			"frappe_pawapay.pawapay.services.accounting.finalize_outbound_payment",
		)

	def test_reconcile_refund_not_found_keeps_transaction_pending(self):
		transaction = self._make_transaction(operation_type="Refund", lifecycle_status="ENQUEUED")
		payload = {"status": "NOT_FOUND"}
		fake_client = lambda settings: FakePawaPayClient(settings, refund_payload=payload)

		with patch("frappe_pawapay.pawapay.services.reconciliation.PawaPayClient", side_effect=fake_client), patch(
			"frappe_pawapay.pawapay.services.reconciliation.frappe.enqueue"
		) as enqueue:
			reconcile_transaction(transaction.name)

		transaction.reload()
		self.assertEqual(transaction.lifecycle_status, "ENQUEUED")
		self.assertEqual(transaction.reconciliation_attempt_count, 1)
		self.assertEqual(len(transaction.status_events), 1)
		self.assertEqual(transaction.status_events[0].received_state, "NOT_FOUND")
		self.assertTrue(
			frappe.db.exists(
				"PawaPay Audit Event",
				{"event_type": "Reconciliation Retryable", "transaction": transaction.name, "retryable": 1},
			)
		)
		enqueue.assert_not_called()

	def test_audit_event_retry_related_action_reconciles_transaction(self):
		transaction = self._make_transaction(operation_type="Refund", lifecycle_status="ENQUEUED")
		audit_event = frappe.get_doc(
			{
				"doctype": "PawaPay Audit Event",
				"event_type": "Reconciliation Retryable",
				"severity": "Warning",
				"status": "NOT_FOUND",
				"event_at": frappe.utils.now_datetime(),
				"retryable": 1,
				"operation_type": transaction.operation_type,
				"external_id": transaction.external_id,
				"transaction": transaction.name,
				"settings": self.settings,
				"message": "Retryable reconciliation test",
			}
		)
		audit_event.insert(ignore_permissions=True)
		payload = {"status": "NOT_FOUND"}
		fake_client = lambda settings: FakePawaPayClient(settings, refund_payload=payload)

		with patch("frappe_pawapay.pawapay.services.reconciliation.PawaPayClient", side_effect=fake_client), patch(
			"frappe_pawapay.pawapay.services.reconciliation.frappe.enqueue"
		) as enqueue:
			audit_event.retry_related_action()

		transaction.reload()
		self.assertEqual(transaction.reconciliation_attempt_count, 1)
		self.assertTrue(
			frappe.db.exists(
				"PawaPay Audit Event",
				{"event_type": "Reconciliation Attempt", "transaction": transaction.name},
			)
		)
		enqueue.assert_not_called()

	def test_resend_callback_calls_provider_and_records_audit_event(self):
		transaction = self._make_transaction(operation_type="Payout", lifecycle_status="ENQUEUED")
		fake_client = lambda settings: FakePawaPayClient(settings)

		with patch("frappe_pawapay.pawapay.services.payouts.PawaPayClient", side_effect=fake_client):
			response = transaction.resend_callback()

		self.assertEqual(response, {"status": "ACCEPTED"})
		self.assertTrue(
			frappe.db.exists(
				"PawaPay Audit Event",
				{"event_type": "Reconciliation Retryable", "transaction": transaction.name, "status": "Callback Resend Requested"},
			)
		)


if __name__ == "__main__":
	import unittest

	unittest.main()
