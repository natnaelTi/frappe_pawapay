from __future__ import annotations

import json
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import frappe
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature, encode_dss_signature
from frappe.tests import UnitTestCase
from http_message_signatures import algorithms

from frappe_pawapay.pawapay.client import PawaPayClient
from frappe_pawapay.pawapay.countries import normalize_pawapay_country_code
from frappe_pawapay.pawapay.exceptions import PawaPayUncertainError
from frappe_pawapay.pawapay.services.checkout import get_payment_page_presentation
from frappe_pawapay.pawapay.signing import _get_signature_algorithm
from frappe_pawapay.pawapay.validation import (
	get_operation_amount_constraints,
	validate_amount_against_active_configuration,
	validate_completed_provider_payload,
)
from frappe_pawapay.www.pawapay.pay import index as payment_page


class FakeResponse:
	def __init__(self, status_code=200, payload=None):
		self.status_code = status_code
		self._payload = payload if payload is not None else {}
		self.content = json.dumps(self._payload).encode()
		self.text = json.dumps(self._payload)

	def json(self):
		return self._payload


class FakeSettings(SimpleNamespace):
	def get(self, fieldname, default=None):
		return getattr(self, fieldname, default)

	def get_password(self, fieldname):
		return getattr(self, fieldname)


class FakeTransaction(SimpleNamespace):
	def __init__(self, **kwargs):
		super().__init__(**kwargs)
		self.db_updates = []

	def db_set(self, values):
		self.db_updates.append(values)


def make_settings(**overrides):
	settings = FakeSettings(
		environment="Sandbox",
		connection_timeout=10,
		read_timeout=30,
		api_token="test-token",
		signing_enabled=0,
		active_configuration_cache=None,
	)
	for key, value in overrides.items():
		setattr(settings, key, value)
	return settings


def make_active_configuration():
	return {
		"countries": [
			{
				"country": "RWA",
				"providers": [
					{
						"provider": "MTN_MOMO_RWA",
						"currencies": [
							{
								"currency": "RWF",
								"operationTypes": {
									"DEPOSIT": {
										"status": "OPERATIONAL",
										"decimalsInAmount": "NONE",
										"minAmount": "100",
										"maxAmount": "500000",
									},
									"PAYOUT": {
										"status": "DELAYED",
										"decimalsInAmount": "NONE",
										"minAmount": "100",
										"maxAmount": "500000",
									},
									"REFUND": {
										"status": "OPERATIONAL",
										"decimalsInAmount": "NONE",
										"minAmount": "100",
										"maxAmount": "500000",
									},
								},
							}
						],
					}
				],
			}
		]
	}


class PawaPayPaymentPageTests(UnitTestCase):
	def test_checkout_form_posts_the_session_csrf_token(self):
		context = frappe._dict()
		original_form_dict = frappe.local.form_dict
		original_request = getattr(frappe.local, "request", None)
		frappe.local.form_dict = frappe._dict(token="test-payment-token")
		frappe.local.request = SimpleNamespace(path="/pawapay/pay/test-payment-token")

		try:
			with patch.object(payment_page, "get_public_payment_context", return_value={}), patch(
				"frappe.sessions.get_csrf_token", return_value="test-csrf-token"
			):
				payment_page.get_context(context)
		finally:
			frappe.local.form_dict = original_form_dict
			if original_request is None:
				del frappe.local.request
			else:
				frappe.local.request = original_request

		self.assertEqual(context.csrf_token, "test-csrf-token")
		template = Path(payment_page.__file__).with_suffix(".html").read_text()
		self.assertIn('name="csrf_token"', template)
		self.assertIn("window.frappe && window.frappe.csrf_token", template)
		self.assertIn("frappe_pawapay.api.get_checkout_csrf_token", template)
		self.assertNotIn("art_gallery.api", template)
		self.assertIn("submitButton.disabled = true", template)
		self.assertIn('id="pawapay-pricing-loader"', template)
		self.assertIn('checkoutForm.setAttribute("aria-busy", "true")', template)
		self.assertIn("control.disabled = true", template)
		self.assertIn("window.requestAnimationFrame", template)
		self.assertNotIn('{{ _("Rate Source") }}', template)
		self.assertLess(template.index("pawapay-summary-price"), template.index("pawapay-summary-rate"))
		self.assertLess(template.index("pawapay-summary-rate"), template.index("pawapay-summary-local"))
		self.assertIn("checkout_footer_text", template)

	def test_installed_app_can_supply_checkout_presentation(self):
		presentation = {
			"is_branded_checkout": True,
			"checkout_standalone": True,
			"checkout_theme": "Studio Ledger",
			"checkout_stylesheet": "/assets/studio/css/checkout.css?v=1",
			"checkout_brand": "AMS",
			"checkout_title": "Complete Your Pro Upgrade",
			"checkout_return_url": "javascript:alert(1)",
			"checkout_provider_name": "PawaPay",
			"checkout_provider_prefix": "Processing is provided by",
			"checkout_provider_logo": "/assets/studio/images/pawapay.svg",
			"checkout_provider_url": "https://provider.example/",
			"checkout_footer_text": "Studio Company · September 2026",
			"checkout_details": [{"label": "Plan", "value": "Pro"}],
		}
		with (
			patch("frappe_pawapay.pawapay.services.checkout.frappe.get_hooks", return_value=["app.payment_context"]),
			patch(
				"frappe_pawapay.pawapay.services.checkout.frappe.get_attr",
				return_value=lambda payment_request, payment_link: presentation,
			),
		):
			context = get_payment_page_presentation(SimpleNamespace(name="PRQ-1"), SimpleNamespace(name="PPL-1"))

		self.assertTrue(context["is_branded_checkout"])
		self.assertTrue(context["checkout_standalone"])
		self.assertEqual(context["checkout_theme_class"], "pawapay-theme--studio-ledger")
		self.assertEqual(context["checkout_stylesheet"], "/assets/studio/css/checkout.css?v=1")
		self.assertEqual(context["checkout_brand"], "AMS")
		self.assertEqual(context["checkout_title"], "Complete Your Pro Upgrade")
		self.assertEqual(context["checkout_details"], [{"label": "Plan", "value": "Pro"}])
		self.assertEqual(context["checkout_return_url"], "")
		self.assertEqual(context["checkout_provider_name"], "PawaPay")
		self.assertEqual(context["checkout_provider_prefix"], "Processing is provided by")
		self.assertEqual(context["checkout_provider_logo"], "/assets/studio/images/pawapay.svg")
		self.assertEqual(context["checkout_provider_url"], "https://provider.example/")
		self.assertEqual(context["checkout_footer_text"], "Studio Company · September 2026")

	def test_legacy_ams_flag_still_enables_branded_checkout(self):
		with (
			patch("frappe_pawapay.pawapay.services.checkout.frappe.get_hooks", return_value=["app.payment_context"]),
			patch(
				"frappe_pawapay.pawapay.services.checkout.frappe.get_attr",
				return_value=lambda payment_request, payment_link: {"is_ams_checkout": True},
			),
		):
			context = get_payment_page_presentation(SimpleNamespace(name="PRQ-1"))

		self.assertTrue(context["is_branded_checkout"])
		self.assertTrue(context["checkout_standalone"])
		self.assertTrue(context["is_ams_checkout"])


class PawaPaySigningTests(UnitTestCase):
	def test_pawapay_ecdsa_verifier_accepts_der_and_raw_signatures(self):
		private_key = ec.generate_private_key(ec.SECP256R1())
		public_key = private_key.public_key().public_bytes(
			serialization.Encoding.PEM,
			serialization.PublicFormat.SubjectPublicKeyInfo,
		)
		message = b"PawaPay callback signature base"
		der_signature = private_key.sign(message, ec.ECDSA(hashes.SHA256()))
		r, s = decode_dss_signature(der_signature)
		raw_signature = r.to_bytes(32, byteorder="big") + s.to_bytes(32, byteorder="big")
		algorithm = _get_signature_algorithm("ecdsa-p256-sha256", algorithms)(public_key=public_key)

		algorithm.verify(der_signature, message)
		algorithm.verify(raw_signature, message)

	def test_pawapay_ecdsa_signer_emits_documented_der_encoding(self):
		private_key = ec.generate_private_key(ec.SECP256R1())
		private_key_pem = private_key.private_bytes(
			serialization.Encoding.PEM,
			serialization.PrivateFormat.PKCS8,
			serialization.NoEncryption(),
		)
		message = b"PawaPay request signature base"
		algorithm = _get_signature_algorithm("ecdsa-p256-sha256", algorithms)(private_key=private_key_pem)
		signature = algorithm.sign(message)
		r, s = decode_dss_signature(signature)

		private_key.public_key().verify(encode_dss_signature(r, s), message, ec.ECDSA(hashes.SHA256()))


class PawaPayClientTests(UnitTestCase):
	def setUp(self):
		patcher = patch("frappe_pawapay.pawapay.client._", side_effect=lambda message, *args, **kwargs: message)
		patcher.start()
		self.addCleanup(patcher.stop)

	def _capture_request(self, call):
		with patch("frappe_pawapay.pawapay.client.create_request_log"), patch(
			"frappe_pawapay.pawapay.client.requests.Session.send"
		) as send:
			send.return_value = FakeResponse(200, {"status": "ACCEPTED"})
			response = call()
			request = send.call_args.args[0]
		return response, json.loads(request.body.decode())

	def test_initiate_payout_uses_v2_recipient_payload(self):
		client = PawaPayClient(make_settings())

		response, payload = self._capture_request(
			lambda: client.initiate_payout(
				payout_id="payout-1",
				amount=Decimal("1500"),
				currency="RWF",
				phone_number="250788123456",
				provider="MTN_MOMO_RWA",
				reference="PPR-0001",
				customer_message="Expense Claim ACC-EXP-1",
			)
		)

		self.assertEqual(response["status"], "ACCEPTED")
		self.assertEqual(payload["payoutId"], "payout-1")
		self.assertEqual(payload["amount"], "1500")
		self.assertEqual(payload["currency"], "RWF")
		self.assertEqual(payload["recipient"]["type"], "MMO")
		self.assertEqual(payload["recipient"]["accountDetails"]["phoneNumber"], "250788123456")
		self.assertEqual(payload["recipient"]["accountDetails"]["provider"], "MTN_MOMO_RWA")
		self.assertEqual(payload["clientReferenceId"], "PPR-0001")

	def test_full_refund_payload_omits_amount_and_currency(self):
		client = PawaPayClient(make_settings())

		_, payload = self._capture_request(
			lambda: client.initiate_refund(
				refund_id="refund-1",
				deposit_id="deposit-1",
				reference="PRR-0001",
			)
		)

		self.assertEqual(payload["refundId"], "refund-1")
		self.assertEqual(payload["depositId"], "deposit-1")
		self.assertNotIn("amount", payload)
		self.assertNotIn("currency", payload)

	def test_partial_refund_payload_includes_amount_and_currency(self):
		client = PawaPayClient(make_settings())

		_, payload = self._capture_request(
			lambda: client.initiate_refund(
				refund_id="refund-2",
				deposit_id="deposit-1",
				amount=Decimal("250"),
				currency="RWF",
				reference="PRR-0002",
			)
		)

		self.assertEqual(payload["amount"], "250")
		self.assertEqual(payload["currency"], "RWF")

	def test_post_server_error_is_uncertain(self):
		client = PawaPayClient(make_settings())
		with patch("frappe_pawapay.pawapay.client.create_request_log"), patch(
			"frappe_pawapay.pawapay.client.requests.Session.send"
		) as send:
			send.return_value = FakeResponse(500, {"error": "temporary"})
			with self.assertRaises(PawaPayUncertainError):
				client.initiate_refund(refund_id="refund-3", deposit_id="deposit-1")


class PawaPayValidationTests(UnitTestCase):
	def setUp(self):
		translate_patcher = patch(
			"frappe_pawapay.pawapay.validation._", side_effect=lambda message, *args, **kwargs: message
		)
		throw_patcher = patch(
			"frappe_pawapay.pawapay.validation.frappe.throw",
			side_effect=lambda message, *args, **kwargs: (_ for _ in ()).throw(frappe.ValidationError(message)),
		)
		translate_patcher.start()
		throw_patcher.start()
		self.addCleanup(translate_patcher.stop)
		self.addCleanup(throw_patcher.stop)

	def test_country_code_normalization_accepts_iso2_and_pawapay_codes(self):
		self.assertEqual(normalize_pawapay_country_code("RW"), "RWA")
		self.assertEqual(normalize_pawapay_country_code("rwa"), "RWA")
		self.assertIsNone(normalize_pawapay_country_code("US"))

	def test_amount_validation_allows_delayed_payout_provider(self):
		settings = make_settings(active_configuration_cache=json.dumps(make_active_configuration()))

		validate_amount_against_active_configuration(settings, Decimal("1500"), "RWF", "RWA", "PAYOUT")

	def test_amount_validation_rejects_unsupported_currency(self):
		settings = make_settings(active_configuration_cache=json.dumps(make_active_configuration()))

		with self.assertRaises(frappe.ValidationError):
			validate_amount_against_active_configuration(settings, Decimal("1500"), "USD", "RWA", "PAYOUT")

	def test_operation_constraints_are_operation_specific(self):
		settings = make_settings(active_configuration_cache=json.dumps(make_active_configuration()))

		constraints = get_operation_amount_constraints(settings, "RWA", "RWF", "REFUND")

		self.assertEqual(len(constraints), 1)
		self.assertEqual(constraints[0]["status"], "OPERATIONAL")

	def test_completed_payload_mismatch_marks_transaction_conflict(self):
		transaction = FakeTransaction(
			operation_type="Payout",
			client_reference_id="PPR-0001",
			amount=Decimal("1500"),
			currency="RWF",
			country="RWA",
		)

		with self.assertRaises(frappe.ValidationError):
			validate_completed_provider_payload(
				transaction,
				{
					"payoutId": "payout-1",
					"clientReferenceId": "PPR-0001",
					"status": "COMPLETED",
					"amount": "1200",
					"currency": "RWF",
					"country": "RWA",
				},
				"callback",
			)

		self.assertEqual(transaction.db_updates[0]["accounting_status"], "CONFLICT")
		self.assertEqual(transaction.db_updates[0]["review_status"], "NEEDS_ATTENTION")

	def test_completed_checkout_uses_outer_identity_and_nested_deposit_amount(self):
		transaction = FakeTransaction(
			operation_type="Checkout",
			client_reference_id="ACC-PRQ-2026-00008",
			amount=Decimal("103"),
			currency="RWF",
			country="RWA",
		)

		validate_completed_provider_payload(
			transaction,
			{
				"checkoutId": "checkout-1",
				"clientReferenceId": "ACC-PRQ-2026-00008",
				"status": "COMPLETED",
				"deposit": {
					"depositId": "deposit-1",
					"status": "COMPLETED",
					"amount": "103.00",
					"currency": "RWF",
					"country": "RWA",
				},
			},
			"Reconciliation",
		)

		self.assertEqual(transaction.db_updates, [])


if __name__ == "__main__":
	unittest.main()
