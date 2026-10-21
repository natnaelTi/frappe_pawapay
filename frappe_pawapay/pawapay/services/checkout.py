from __future__ import annotations

import hashlib
import json
import re
import secrets
import uuid
from urllib.parse import quote, urlparse

import frappe
from frappe import _
from frappe.utils import get_datetime, now_datetime

from frappe_pawapay.compatibility import get_collection_account, get_payment_request_source_amount
from frappe_pawapay.pawapay.countries import ISO2_TO_PAWAPAY_COUNTRY
from frappe_pawapay.pawapay.client import PawaPayClient
from frappe_pawapay.pawapay.exceptions import PawaPayUncertainError, PawaPayValidationError
from frappe_pawapay.pawapay.services.fx import get_checkout_amount_plan
from frappe_pawapay.pawapay.validation import (
	assert_allowed_redirect,
	get_deposit_country_options,
	get_first_deposit_currency,
	validate_completed_provider_payload,
	validate_amount_against_active_configuration,
)


def token_hash(token: str) -> str:
	return hashlib.sha256(token.encode("utf-8")).hexdigest()


def get_or_create_payment_link(settings, payment_request_name: str, **kwargs) -> str:
	payment_request = frappe.get_doc("Payment Request", payment_request_name)
	existing = frappe.db.get_value(
		"PawaPay Payment Link",
		{"payment_request": payment_request.name, "settings": settings.name, "status": "Active"},
		"name",
	)
	if existing:
		link = frappe.get_doc("PawaPay Payment Link", existing)
		if _is_expired(link):
			link.db_set("status", "Expired", update_modified=True)
			existing = None
		else:
			token = link.get_password("public_token")
	if not existing:
		token = secrets.token_urlsafe(32)
		link = frappe.get_doc(
			{
				"doctype": "PawaPay Payment Link",
				"token_hash": token_hash(token),
				"public_token": token,
				"settings": settings.name,
				"payment_request": payment_request.name,
				"reference_doctype": payment_request.reference_doctype,
				"reference_name": payment_request.reference_name,
				"company": payment_request.get("company") or settings.company,
				"expected_amount": payment_request.grand_total,
				"currency": payment_request.currency,
				"status": "Active",
				"business_expiry": kwargs.get("business_expiry"),
			}
		)
		link.insert(ignore_permissions=True)
	return frappe.utils.get_url(f"/pawapay/pay/{token}")


def resolve_payment_link(token: str):
	link_name = frappe.db.get_value("PawaPay Payment Link", {"token_hash": token_hash(token)}, "name")
	if not link_name:
		frappe.throw(_("Invalid or expired PawaPay payment link."), frappe.PermissionError)
	link = frappe.get_doc("PawaPay Payment Link", link_name)
	if _is_expired(link) and link.status == "Active":
		link.db_set("status", "Expired", update_modified=True)
		link.status = "Expired"
	return link


def get_public_payment_context(token: str, country: str | None = None, currency: str | None = None):
	link = resolve_payment_link(token)
	payment_request = frappe.get_doc("Payment Request", link.payment_request)
	settings = frappe.get_doc("PawaPay Settings", link.settings)
	country_options = get_deposit_country_options(settings)
	selected_country = _resolve_collection_country(settings, country, country_options)
	selected_currency = _resolve_collection_currency(settings, selected_country, currency)
	checkout_error = None
	amount_plan = None
	try:
		amount_plan = get_checkout_amount_plan(settings, payment_request, selected_country, selected_currency)
		validate_amount_against_active_configuration(
			settings, amount_plan.collection_amount, amount_plan.collection_currency, selected_country
		)
	except frappe.ValidationError as exc:
		checkout_error = str(exc)
	is_link_payable = _can_pay(payment_request, link)
	context = {
		"token": token,
		"status": link.status,
		"merchant": link.company,
		"reference": payment_request.reference_name or payment_request.name,
		"subject": payment_request.subject,
		"amount": get_payment_request_source_amount(payment_request),
		"currency": payment_request.currency,
		"collection_amount": amount_plan.collection_amount if amount_plan else None,
		"collection_currency": selected_currency,
		"collection_country": selected_country,
		"converted_payment": amount_plan.converted if amount_plan else False,
		"exchange_rate_used": amount_plan.exchange_rate if amount_plan and amount_plan.converted else None,
		"fx_source": amount_plan.fx_source if amount_plan and amount_plan.converted else None,
		"fx_rate_date": amount_plan.fx_rate_date if amount_plan and amount_plan.converted else None,
		"fx_source_url": amount_plan.fx_source_url if amount_plan and amount_plan.converted else None,
		"country_options": country_options,
		"country_options_json": json.dumps(country_options, default=str, separators=(",", ":")).replace("</", "<\\/"),
		"detected_country": _detect_collection_country(country_options),
		"checkout_error": checkout_error,
		"is_link_payable": is_link_payable,
		"can_pay": is_link_payable and not checkout_error,
	}
	context.update(get_payment_page_presentation(payment_request, link))
	return context


def get_payment_page_presentation(payment_request, payment_link=None) -> dict:
	"""Collect and normalize optional host-app presentation for the hosted checkout."""
	context = {}
	for method in frappe.get_hooks("pawapay_payment_page_context") or []:
		try:
			values = frappe.get_attr(method)(payment_request, payment_link) or {}
			if isinstance(values, dict):
				context.update(values)
		except Exception:
			frappe.log_error(frappe.get_traceback(), "PawaPay payment page context failed")
	return _normalize_payment_page_presentation(context)


def _normalize_payment_page_presentation(values: dict | None) -> dict:
	"""Keep host customization declarative and safe to render in public templates."""
	values = values or {}
	branded = bool(values.get("is_branded_checkout") or values.get("is_ams_checkout"))
	theme = _plain_text(values.get("checkout_theme"), max_length=60).lower()
	theme = re.sub(r"[^a-z0-9_-]+", "-", theme).strip("-")

	context = {
		"is_branded_checkout": branded,
		# Retained for host apps using the first version of the presentation hook.
		"is_ams_checkout": bool(values.get("is_ams_checkout")),
		"checkout_standalone": branded and bool(values.get("checkout_standalone", True)),
		"checkout_theme": theme,
		"checkout_theme_class": f"pawapay-theme--{theme}" if theme else "",
		"checkout_stylesheet": _safe_asset_url(values.get("checkout_stylesheet"), suffixes=(".css",)),
		"checkout_favicon": _safe_asset_url(
			values.get("checkout_favicon"), suffixes=(".ico", ".png", ".svg", ".webp")
		),
		"checkout_logo": _safe_asset_url(
			values.get("checkout_logo"), suffixes=(".png", ".svg", ".webp", ".jpg", ".jpeg")
		),
		"checkout_provider_logo": _safe_asset_url(
			values.get("checkout_provider_logo"), suffixes=(".png", ".svg", ".webp", ".jpg", ".jpeg")
		),
		"checkout_image": _safe_asset_url(
			values.get("checkout_image"), suffixes=(".png", ".webp", ".jpg", ".jpeg")
		),
		"checkout_home_url": _safe_navigation_url(values.get("checkout_home_url")),
		"checkout_return_url": _safe_navigation_url(values.get("checkout_return_url")),
		"checkout_provider_url": _safe_navigation_url(values.get("checkout_provider_url")),
		"checkout_show_merchant": bool(values.get("checkout_show_merchant", True)),
		"checkout_show_reference": bool(values.get("checkout_show_reference", True)),
		"checkout_details": _normalize_labelled_values(values.get("checkout_details")),
		"checkout_assurances": _normalize_text_list(values.get("checkout_assurances"), limit=4),
	}

	text_fields = {
		"checkout_page_title": 120,
		"checkout_brand": 80,
		"checkout_brand_descriptor": 120,
		"checkout_header_label": 80,
		"checkout_eyebrow": 80,
		"checkout_title": 160,
		"checkout_description": 500,
		"checkout_image_alt": 180,
		"checkout_image_caption": 240,
		"checkout_notice": 300,
		"checkout_amount_label": 80,
		"checkout_local_amount_label": 80,
		"checkout_merchant_label": 80,
		"checkout_reference_label": 80,
		"checkout_submit_label": 100,
		"checkout_processing_label": 100,
		"checkout_provider_note": 240,
		"checkout_provider_prefix": 180,
		"checkout_return_label": 100,
		"checkout_footer_text": 180,
		"checkout_status_eyebrow": 80,
		"checkout_status_title": 160,
		"checkout_status_description": 400,
		"checkout_provider_name": 80,
	}
	for fieldname, max_length in text_fields.items():
		context[fieldname] = _plain_text(values.get(fieldname), max_length=max_length)
	return context


def _plain_text(value, *, max_length: int) -> str:
	if value is None:
		return ""
	return str(value).replace("\x00", "").strip()[:max_length]


def _normalize_labelled_values(values) -> list[dict]:
	if not isinstance(values, (list, tuple)):
		return []
	result = []
	for value in values[:8]:
		if not isinstance(value, dict):
			continue
		label = _plain_text(value.get("label"), max_length=80)
		detail = _plain_text(value.get("value"), max_length=180)
		if label and detail:
			result.append({"label": label, "value": detail})
	return result


def _normalize_text_list(values, *, limit: int) -> list[str]:
	if not isinstance(values, (list, tuple)):
		return []
	return [text for value in values[:limit] if (text := _plain_text(value, max_length=180))]


def _safe_asset_url(value, *, suffixes: tuple[str, ...]) -> str:
	value = _plain_text(value, max_length=500)
	if not value.startswith("/assets/") or value.startswith("//"):
		return ""
	path = value.split("?", 1)[0].lower()
	return value if path.endswith(suffixes) else ""


def _safe_navigation_url(value) -> str:
	value = _plain_text(value, max_length=500)
	if value.startswith("/") and not value.startswith("//"):
		return value
	parsed = urlparse(value)
	if parsed.scheme in {"http", "https"} and parsed.netloc:
		return value
	return ""


def _can_pay(payment_request, link) -> bool:
	if link.status != "Active":
		return False
	if _is_expired(link):
		return False
	if payment_request.docstatus != 1:
		return False
	if payment_request.status in {"Paid", "Cancelled"}:
		return False
	if get_payment_request_source_amount(payment_request) <= 0:
		return False
	return True


def _is_expired(link) -> bool:
	return bool(link.business_expiry and get_datetime(link.business_expiry) < now_datetime())


def _payment_page_error(token: str, message: str) -> str:
	return frappe.utils.get_url(f"/pawapay/pay/{token}?error={quote(str(message)[:300])}")


def create_checkout_for_link(token: str, country: str | None = None, currency: str | None = None) -> str:
	link = resolve_payment_link(token)
	payment_request = frappe.get_doc("Payment Request", link.payment_request)
	if not _can_pay(payment_request, link):
		frappe.throw(_("This payment request cannot be paid through PawaPay."))

	settings = frappe.get_doc("PawaPay Settings", link.settings)
	country_options = get_deposit_country_options(settings)
	collection_country = _resolve_collection_country(settings, country, country_options)
	collection_currency = _resolve_collection_currency(settings, collection_country, currency)
	amount_plan = get_checkout_amount_plan(settings, payment_request, collection_country, collection_currency)
	validate_amount_against_active_configuration(
		settings, amount_plan.collection_amount, amount_plan.collection_currency, collection_country
	)
	account_config = get_collection_account(settings, collection_country, amount_plan.collection_currency)

	active_transaction = _get_active_transaction(
		link.name, amount_plan.collection_amount, amount_plan.collection_currency, collection_country
	)
	if active_transaction and active_transaction.redirect_url:
		return assert_allowed_redirect(active_transaction.redirect_url)

	external_id = str(uuid.uuid4())
	transaction = frappe.get_doc(
		{
			"doctype": "PawaPay Transaction",
			"external_id": external_id,
			"operation_type": "Checkout",
			"settings": settings.name,
			"environment": settings.environment,
			"initiation_status": "CREATED",
			"lifecycle_status": "WAITING_PAYMENT",
			"accounting_status": "PENDING",
			"review_status": "NORMAL",
			"company": link.company,
			"amount": amount_plan.collection_amount,
			"currency": amount_plan.collection_currency,
			"source_amount": amount_plan.source_amount,
			"source_currency": amount_plan.source_currency,
			"exchange_rate_used": amount_plan.exchange_rate,
			"fx_source": amount_plan.fx_source,
			"fx_rate_date": amount_plan.fx_rate_date,
				"fx_source_url": amount_plan.fx_source_url,
				"currency_exchange": amount_plan.currency_exchange,
				"fx_rounding_adjustment": amount_plan.rounding_adjustment,
				"country": collection_country,
				"collection_account": account_config["wallet_account"],
				"collection_mode_of_payment": account_config["mode_of_payment"],
				"client_reference_id": payment_request.name,
			"reference_doctype": payment_request.reference_doctype,
			"reference_name": payment_request.reference_name,
			"payment_request": payment_request.name,
			"payment_link": link.name,
		}
	)
	transaction.insert(ignore_permissions=True)
	link.db_set({"last_checkout_transaction": transaction.name, "attempt_count": (link.attempt_count or 0) + 1})
	frappe.db.commit()  # PawaPay UUID must exist locally before initiation

	client = PawaPayClient(settings)
	try:
		response = client.initiate_checkout(
			checkout_id=external_id,
			amount=amount_plan.collection_amount,
			currency=amount_plan.collection_currency,
			country=collection_country,
			reference=payment_request.name,
			reason=payment_request.subject,
		)
	except PawaPayValidationError as exc:
		transaction.db_set(
			{
				"initiation_status": "REJECTED",
				"lifecycle_status": "FAILED",
				"review_status": "NEEDS_ATTENTION",
				"last_error": str(exc)[:1000],
			}
		)
		frappe.db.commit()
		return _payment_page_error(token, _("PawaPay rejected the checkout: {0}").format(str(exc)[:300]))
	except PawaPayUncertainError as exc:
		transaction.db_set({"initiation_status": "UNCERTAIN", "last_error": str(exc)[:1000]})
		try:
			status_payload = client.get_checkout(external_id)
			consume_checkout_status(transaction, status_payload, source="Poll")
			transaction.save(ignore_permissions=True)
			transaction.reload()
			if transaction.redirect_url:
				return assert_allowed_redirect(transaction.redirect_url)
		except Exception:
			pass
		return frappe.utils.get_url(f"/pawapay/pay/{token}?pending=1")

	transaction.apply_initiation_response(response, source="Initiation")
	transaction.reload()
	if transaction.initiation_status == "REJECTED":
		message = transaction.failure_message or _("PawaPay rejected the checkout.")
		frappe.db.commit()
		return _payment_page_error(token, _("PawaPay rejected the checkout: {0}").format(message))
	if transaction.initiation_status == "DUPLICATE_IGNORED":
		status_payload = client.get_checkout(external_id)
		consume_checkout_status(transaction, status_payload, source="Poll")
		transaction.save(ignore_permissions=True)
		transaction.reload()
	if not transaction.redirect_url:
		transaction.db_set(
			{
				"review_status": "NEEDS_ATTENTION",
				"last_error": _("PawaPay did not return a checkout redirect URL."),
			}
		)
		frappe.db.commit()
		return _payment_page_error(token, _("PawaPay did not return a checkout redirect URL."))
	return assert_allowed_redirect(transaction.redirect_url)


def _get_active_transaction(payment_link: str, amount, currency: str, country: str):
	transaction_name = frappe.db.get_value(
		"PawaPay Transaction",
		{
			"payment_link": payment_link,
			"operation_type": "Checkout",
			"amount": amount,
			"currency": currency,
			"country": country,
			"lifecycle_status": ["in", ["WAITING_PAYMENT", "PROCESSING", "IN_RECONCILIATION"]],
		},
		"name",
		order_by="creation desc",
	)
	return frappe.get_doc("PawaPay Transaction", transaction_name) if transaction_name else None


def _resolve_collection_country(settings, requested_country: str | None, country_options: list[dict]) -> str:
	allowed = {option.get("country") for option in country_options}
	candidates = [
		(requested_country or "").upper(),
		_detect_collection_country(country_options),
		(settings.default_country or "").upper(),
	]
	for candidate in candidates:
		if candidate in allowed:
			return candidate
	return country_options[0].get("country") if country_options else settings.default_country


def _resolve_collection_currency(settings, collection_country: str, requested_currency: str | None) -> str:
	allowed = {
		currency.get("currency")
		for option in get_deposit_country_options(settings)
		if option.get("country") == collection_country
		for currency in option.get("currencies", [])
	}
	requested_currency = (requested_currency or "").upper()
	if requested_currency in allowed:
		return requested_currency
	currency = get_first_deposit_currency(settings, collection_country)
	if currency:
		return currency
	frappe.throw(_("PawaPay does not support deposits in the selected country."))


def _detect_collection_country(country_options: list[dict]) -> str | None:
	allowed = {option.get("country") for option in country_options}
	headers = getattr(frappe.local, "request", None).headers if getattr(frappe.local, "request", None) else {}
	for header in ("CF-IPCountry", "X-Country-Code", "X-AppEngine-Country", "X-Vercel-IP-Country"):
		value = (headers.get(header) or "").upper()
		if value in allowed:
			return value
		if ISO2_TO_PAWAPAY_COUNTRY.get(value) in allowed:
			return ISO2_TO_PAWAPAY_COUNTRY[value]
	return None


def consume_checkout_status(transaction, payload: dict, source: str = "Poll"):
	wrapper_status = payload.get("status") or payload.get("searchStatus")
	checkout = payload.get("checkout") or payload.get("data") or payload
	if wrapper_status == "NOT_FOUND":
		transaction.append_status_event(source, transaction.lifecycle_status, "NOT_FOUND", transaction.lifecycle_status)
		return
	lifecycle = checkout.get("status") or checkout.get("checkoutStatus") or checkout.get("lifecycleStatus")
	if lifecycle == "COMPLETED":
		validate_completed_provider_payload(transaction, checkout, source)
	if checkout.get("redirectUrl"):
		transaction.redirect_url = assert_allowed_redirect(checkout.get("redirectUrl"))
	if checkout.get("checkoutCode"):
		transaction.checkout_code = checkout.get("checkoutCode")
	provider_transaction_id = checkout.get("providerTransactionId")
	deposit = checkout.get("deposit") or checkout.get("latestDeposit") or {}
	if deposit:
		transaction.underlying_deposit_id = deposit.get("depositId") or transaction.underlying_deposit_id
		provider_transaction_id = deposit.get("providerTransactionId") or provider_transaction_id
		transaction.failure_code = deposit.get("failureCode") or transaction.failure_code
		transaction.failure_message = deposit.get("failureMessage") or transaction.failure_message
	transaction.provider_transaction_id = provider_transaction_id or transaction.provider_transaction_id
	transaction.apply_lifecycle_state(lifecycle, source=source, payload=payload)
