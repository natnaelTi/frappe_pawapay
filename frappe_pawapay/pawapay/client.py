from __future__ import annotations

import json
import re
from decimal import Decimal

import frappe
import requests
from frappe import _
from frappe.integrations.utils import create_request_log

from frappe_pawapay.pawapay.exceptions import (
	PawaPayAuthenticationError,
	PawaPayContractError,
	PawaPayTransportError,
	PawaPayUncertainError,
	PawaPayValidationError,
)
from frappe_pawapay.pawapay.models import Money, get_base_url
from frappe_pawapay.pawapay.signing import PawaPaySigningError, apply_outgoing_signature


SENSITIVE_KEYS = {"authorization", "api_token", "token", "signature", "signature-input", "private_key"}
CHECKOUT_REASON_MAX_LENGTH = 22


def redact(value):
	if isinstance(value, dict):
		return {k: ("***" if k.lower() in SENSITIVE_KEYS else redact(v)) for k, v in value.items()}
	if isinstance(value, list):
		return [redact(v) for v in value]
	return value


def _checkout_reason(reason: str | None, reference: str | None) -> str:
	text = _normalize_checkout_reason(reason or reference or "Invoice payment")
	if len(text) <= CHECKOUT_REASON_MAX_LENGTH:
		return text
	fallback = _normalize_checkout_reason(reference or "Invoice payment")
	if len(fallback) <= CHECKOUT_REASON_MAX_LENGTH:
		return fallback
	return fallback[:CHECKOUT_REASON_MAX_LENGTH]


def _normalize_checkout_reason(value: str) -> str:
	text = re.sub(r"[^0-9A-Za-z ]+", " ", value or "")
	text = re.sub(r"\s+", " ", text).strip()
	return text or "Invoice payment"


class PawaPayClient:
	def __init__(self, settings):
		self.settings = settings
		self.base_url = get_base_url(settings.environment)
		self.timeout = (settings.connection_timeout or 10, settings.read_timeout or 30)
		self.token = settings.get_password("api_token")
		if not self.token:
			frappe.throw(_("PawaPay API token is required."))

	def _headers(self):
		return {
			"Authorization": f"Bearer {self.token}",
			"Content-Type": "application/json; charset=UTF-8",
			"Accept": "application/json",
		}

	def _request(self, method: str, path: str, payload: dict | None = None, idempotency_id: str | None = None):
		url = self.base_url + path
		body = json.dumps(payload, separators=(",", ":"), sort_keys=True) if payload is not None else None
		try:
			prepared_request = requests.Request(
				method,
				url,
				headers=self._headers(),
				data=body.encode("utf-8") if body is not None else None,
			).prepare()
			if payload is not None and method.upper() in {"POST", "PUT", "PATCH"}:
				prepared_request = apply_outgoing_signature(prepared_request, self.settings)
			response = requests.Session().send(prepared_request, timeout=self.timeout)
		except PawaPaySigningError:
			raise
		except requests.RequestException as exc:
			self._log(method, path, payload, None, str(exc), idempotency_id)
			if method == "POST":
				raise PawaPayUncertainError(str(exc))
			raise PawaPayTransportError(str(exc))

		data = self._decode(response)
		self._log(method, path, payload, data, None, idempotency_id)
		if response.status_code in (401, 403):
			raise PawaPayAuthenticationError(
				_(
					"PawaPay authentication failed for {0}. Check that the saved API token was generated "
					"from the matching PawaPay {0} dashboard, and that you pasted the full token value, not the Token ID."
				).format(self.settings.environment)
			)
		if response.status_code >= 500 and method == "POST":
			raise PawaPayUncertainError(_("PawaPay returned an uncertain server error."))
		if response.status_code >= 400:
			raise PawaPayValidationError(data or response.text)
		return data

	def _decode(self, response):
		if not response.content:
			return {}
		try:
			return response.json()
		except ValueError as exc:
			raise PawaPayContractError(_("PawaPay returned non-JSON response.")) from exc

	def _log(self, method, path, request_payload, response_payload, error, idempotency_id):
		try:
			create_request_log(
				{
					"method": method,
					"path": path,
					"request": redact(request_payload or {}),
					"error": error,
				},
				"Host",
				"PawaPay",
				idempotency_id or frappe.generate_hash(length=20),
				output=redact(response_payload or {}),
			)
		except Exception:
			frappe.log_error("Unable to create redacted PawaPay Integration Request")

	def initiate_checkout(self, *, checkout_id: str, amount: Decimal, currency: str, country: str, reference: str, reason: str):
		payload = {
			"checkoutId": checkout_id,
			"returnUrl": frappe.utils.get_url("/pawapay/return"),
			"returnMethod": self.settings.checkout_return_method or "INSTANT",
			"defaultLanguage": self.settings.default_language or "en",
			"expiresAfter": int(self.settings.default_checkout_expiry or 15),
			"amounts": [
				{"country": country, "currency": currency, "amount": Money(amount, currency).as_api_string()}
			],
			"clientReferenceId": reference,
			"reason": {self.settings.default_language or "en": _checkout_reason(reason, reference)},
			"metadata": [{"paymentRequest": reference, "isPII": False}],
		}
		return self._request("POST", "/v2/checkouts", payload, checkout_id)

	def initiate_payout(
		self,
		*,
		payout_id: str,
		amount: Decimal,
		currency: str,
		phone_number: str,
		provider: str,
		reference: str | None = None,
		customer_message: str | None = None,
	):
		payload = {
			"payoutId": payout_id,
			"amount": Money(amount, currency).as_api_string(),
			"currency": currency,
			"recipient": {
				"type": "MMO",
				"accountDetails": {"phoneNumber": phone_number, "provider": provider},
			},
		}
		if reference:
			payload["clientReferenceId"] = reference
			payload["metadata"] = [{"payoutRequest": reference, "isPII": False}]
		if customer_message:
			payload["customerMessage"] = _checkout_reason(customer_message, reference)
		return self._request("POST", "/v2/payouts", payload, payout_id)

	def initiate_refund(
		self,
		*,
		refund_id: str,
		deposit_id: str,
		amount: Decimal | None = None,
		currency: str | None = None,
		reference: str | None = None,
	):
		payload = {"refundId": refund_id, "depositId": deposit_id}
		if amount is not None and currency:
			payload["amount"] = Money(amount, currency).as_api_string()
			payload["currency"] = currency
		if reference:
			payload["clientReferenceId"] = reference
			payload["metadata"] = [{"refundRequest": reference, "isPII": False}]
		return self._request("POST", "/v2/refunds", payload, refund_id)

	def get_checkout(self, checkout_id: str):
		return self._request("GET", f"/v2/checkouts/{checkout_id}", idempotency_id=checkout_id)

	def expire_checkout(self, checkout_id: str):
		return self._request("POST", f"/v2/checkouts/{checkout_id}/expire", {}, checkout_id)

	def get_deposit(self, deposit_id: str):
		return self._request("GET", f"/v2/deposits/{deposit_id}", idempotency_id=deposit_id)

	def get_payout(self, payout_id: str):
		return self._request("GET", f"/v2/payouts/{payout_id}", idempotency_id=payout_id)

	def get_refund(self, refund_id: str):
		return self._request("GET", f"/v2/refunds/{refund_id}", idempotency_id=refund_id)

	def cancel_enqueued_payout(self, payout_id: str):
		return self._request("POST", f"/v2/payouts/fail-enqueued/{payout_id}", {}, payout_id)

	def cancel_enqueued_refund(self, refund_id: str):
		return self._request("POST", f"/v2/refunds/fail-enqueued/{refund_id}", {}, refund_id)

	def resend_payout_callback(self, payout_id: str):
		return self._request("POST", f"/v2/payouts/resend-callback/{payout_id}", {}, payout_id)

	def resend_refund_callback(self, refund_id: str):
		return self._request("POST", f"/v2/refunds/resend-callback/{refund_id}", {}, refund_id)

	def get_active_configuration(self):
		return self._request("GET", "/v2/active-conf")

	def get_availability(self):
		return self._request("GET", "/v2/availability")

	def predict_provider(self, msisdn: str):
		return self._request("POST", "/v2/predict-provider", {"msisdn": msisdn})

	def get_public_keys(self):
		return self._request("GET", "/v2/public-key/http")

	def get_wallet_balances(self):
		return self._request("GET", "/v2/wallet-balances")
