from __future__ import annotations

import datetime
import hashlib
import os

import frappe
from frappe import _
from requests import Request
from requests.structures import CaseInsensitiveDict


ACCEPT_SIGNATURE = "rsa-pss-sha512,ecdsa-p256-sha256,rsa-v1_5-sha256,ecdsa-p384-sha384"
ACCEPT_DIGEST = "sha-256,sha-512"
SIGNED_REQUEST_COMPONENTS = ("@method", "@authority", "@path", "signature-date", "content-digest", "content-type")
PAWAPAY_PUBLIC_KEYS_CACHE_KEY = "frappe_pawapay:public_keys:{settings}"
MAX_CALLBACK_SIGNATURE_AGE_SECONDS = 300


class SignatureVerificationError(Exception):
	def __init__(self, message, key_id=None):
		super().__init__(message)
		self.key_id = key_id


class PawaPaySigningError(Exception):
	pass


class PawaPayKeyResolver:
	def __init__(self, private_key: bytes):
		self.private_key = private_key

	def resolve_public_key(self, key_id: str):
		raise NotImplementedError

	def resolve_private_key(self, key_id: str):
		return self.private_key


class PawaPayPublicKeyResolver:
	def __init__(self, settings):
		self.settings = settings

	def resolve_private_key(self, key_id: str):
		raise NotImplementedError

	def resolve_public_key(self, key_id: str):
		keys = get_pawapay_public_keys(self.settings)
		if key_id not in keys:
			keys = get_pawapay_public_keys(self.settings, force_refresh=True)
		try:
			return keys[key_id].encode("utf-8")
		except KeyError as exc:
			raise SignatureVerificationError(_("Unknown PawaPay callback signing key: {0}").format(key_id), key_id) from exc


def apply_outgoing_signature(prepared_request, settings):
	"""Add PawaPay RFC 9421 signature headers to a prepared requests.Request."""
	if not settings.signing_enabled:
		return prepared_request
	if not settings.signing_key_id:
		raise PawaPaySigningError(_("Signing Key ID is required when outgoing request signing is enabled."))

	private_key = resolve_private_key(settings)
	signature_date, created, expires = _signature_time_window()

	prepared_request.headers["Content-Type"] = "application/json; charset=UTF-8"
	prepared_request.headers["Content-Digest"] = _content_digest(prepared_request.body or b"")
	prepared_request.headers["Signature-Date"] = signature_date
	prepared_request.headers["Accept-Signature"] = ACCEPT_SIGNATURE
	prepared_request.headers["Accept-Digest"] = ACCEPT_DIGEST

	try:
		from http_message_signatures import HTTPMessageSigner, algorithms
	except ImportError as exc:
		raise PawaPaySigningError(_("RFC 9421 request signing dependency is not installed.")) from exc

	algorithm = _get_signature_algorithm(settings.signing_algorithm, algorithms)
	signer = HTTPMessageSigner(signature_algorithm=algorithm, key_resolver=PawaPayKeyResolver(private_key))
	signer.sign(
		prepared_request,
		key_id=settings.signing_key_id,
		created=created,
		expires=expires,
		label="sig-pp",
		covered_component_ids=SIGNED_REQUEST_COMPONENTS,
	)
	return prepared_request


def get_pawapay_public_keys(settings, force_refresh: bool = False) -> dict[str, str]:
	cache_key = PAWAPAY_PUBLIC_KEYS_CACHE_KEY.format(settings=settings.name)
	if not force_refresh:
		cached_keys = frappe.cache().get_value(cache_key)
		if cached_keys:
			return cached_keys

	from frappe_pawapay.pawapay.client import PawaPayClient

	payload = PawaPayClient(settings).get_public_keys()
	keys = {entry.get("id"): entry.get("key") for entry in payload or [] if entry.get("id") and entry.get("key")}
	if not keys:
		raise SignatureVerificationError(_("PawaPay did not return any callback public keys."))
	frappe.cache().set_value(cache_key, keys, expires_in_sec=int(settings.public_key_cache_ttl or 900))
	return keys


def resolve_private_key(settings) -> bytes:
	reference = (settings.private_key_reference or "").strip()
	if not reference:
		raise PawaPaySigningError(_("Private Key Secret Reference is required when outgoing request signing is enabled."))

	if reference.startswith("env:"):
		key = os.environ.get(reference.removeprefix("env:"))
	elif reference.startswith("site_config:"):
		key = frappe.conf.get(reference.removeprefix("site_config:"))
	elif reference.startswith("file:"):
		key = _read_key_file(reference.removeprefix("file:"))
	else:
		key = frappe.conf.get(reference) or os.environ.get(reference)

	if not key:
		raise PawaPaySigningError(_("Could not resolve the configured PawaPay private key secret reference."))
	if isinstance(key, str):
		key = key.replace("\\n", "\n").encode("utf-8")
	return key


def _read_key_file(path: str) -> str:
	path = os.path.expanduser(path.strip())
	if not path:
		raise PawaPaySigningError(_("Private key file path is empty."))
	with open(path, encoding="utf-8") as key_file:
		return key_file.read()


def _signature_time_window():
	created = datetime.datetime.now(datetime.UTC)
	expires = created + datetime.timedelta(seconds=60)
	signature_date = created.isoformat(timespec="microseconds").replace("+00:00", "Z")
	return signature_date, created, expires


def _content_digest(body: bytes | str):
	if isinstance(body, str):
		body = body.encode("utf-8")
	try:
		import hashlib
		import http_sfv
	except ImportError as exc:
		raise PawaPaySigningError(_("RFC 9421 content digest dependency is not installed.")) from exc
	return str(http_sfv.Dictionary({"sha-512": hashlib.sha512(body or b"").digest()}))


def _get_signature_algorithm(algorithm_name: str, algorithms):
	algorithm_map = {
		"rsa-pss-sha512": algorithms.RSA_PSS_SHA512,
		"rsa-v1_5-sha256": algorithms.RSA_V1_5_SHA256,
		"ecdsa-p256-sha256": _pawapay_ecdsa_p256_sha256(algorithms),
	}
	try:
		return algorithm_map[algorithm_name]
	except KeyError as exc:
		raise PawaPaySigningError(
			_(
				"Unsupported PawaPay signing algorithm for the installed RFC 9421 library: {0}. "
				"Use rsa-pss-sha512, rsa-v1_5-sha256, or ecdsa-p256-sha256."
			).format(algorithm_name)
		) from exc


def _pawapay_ecdsa_p256_sha256(algorithms):
	from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
	from http_message_signatures import HTTPMessageSignaturesException

	class PawaPayECDSA_P256_SHA256(algorithms.ECDSA_P256_SHA256):
		def sign(self, message: bytes):
			return self.private_key.sign(message, signature_algorithm=self.signature_algorithm)

		def verify(self, signature: bytes, message: bytes):
			if len(signature) == 64:
				return super().verify(signature, message)
			try:
				decode_dss_signature(signature)
			except ValueError as exc:
				raise HTTPMessageSignaturesException("Invalid DER-encoded ECDSA signature") from exc
			return self.public_key.verify(
				signature=signature,
				data=message,
				signature_algorithm=self.signature_algorithm,
			)

	return PawaPayECDSA_P256_SHA256


def verify_callback_signature(*, settings, raw_body: bytes, headers) -> tuple[bool, str | None]:
	has_signature_headers = all(headers.get(header) for header in ("Signature", "Signature-Input", "Signature-Date", "Content-Digest"))
	if not has_signature_headers:
		if settings.require_signed_callbacks:
			raise SignatureVerificationError(_("Signed callbacks are required, but signature headers are missing."))
		return False, None

	try:
		from http_message_signatures import HTTPMessageVerifier, InvalidSignature, algorithms
	except ImportError as exc:
		raise SignatureVerificationError(
			_("RFC 9421 signature verification dependency is not installed.")
		) from exc

	headers = CaseInsensitiveDict(headers)
	_verify_content_digest(raw_body, headers.get("Content-Digest"))
	signature_input = _parse_signature_input(headers.get("Signature-Input"))
	algorithm_name = signature_input.get("alg")
	key_id = signature_input.get("keyid")
	if not key_id:
		raise SignatureVerificationError(_("PawaPay callback signature is missing keyid."))

	algorithm = _get_signature_algorithm(algorithm_name, algorithms)
	request = _build_callback_request(raw_body, headers)
	verifier = HTTPMessageVerifier(signature_algorithm=algorithm, key_resolver=PawaPayPublicKeyResolver(settings))
	try:
		results = verifier.verify(request, max_age=datetime.timedelta(seconds=MAX_CALLBACK_SIGNATURE_AGE_SECONDS))
	except InvalidSignature as exc:
		raise SignatureVerificationError(_("Invalid PawaPay callback signature: {0}").format(str(exc)), key_id) from exc

	result = results[0]
	_require_signed_components(result.covered_components)
	return True, result.parameters.get("keyid")


def _verify_content_digest(raw_body: bytes, content_digest: str | None):
	if not content_digest:
		raise SignatureVerificationError(_("PawaPay callback is missing Content-Digest."))
	try:
		import http_sfv

		digest_header = http_sfv.Dictionary()
		digest_header.parse(content_digest.encode("utf-8"))
	except Exception as exc:
		raise SignatureVerificationError(_("Invalid PawaPay Content-Digest header.")) from exc

	supported = {
		"sha-512": hashlib.sha512(raw_body).digest(),
		"sha-256": hashlib.sha256(raw_body).digest(),
	}
	for algorithm, expected_digest in supported.items():
		if algorithm in digest_header and digest_header[algorithm].value == expected_digest:
			return
	raise SignatureVerificationError(_("PawaPay callback Content-Digest does not match the request body."))


def _parse_signature_input(signature_input: str | None):
	if not signature_input:
		raise SignatureVerificationError(_("PawaPay callback is missing Signature-Input."))
	try:
		import http_sfv

		signature_input_header = http_sfv.Dictionary()
		signature_input_header.parse(signature_input.encode("utf-8"))
	except Exception as exc:
		raise SignatureVerificationError(_("Invalid PawaPay Signature-Input header.")) from exc
	if len(signature_input_header) != 1:
		raise SignatureVerificationError(_("PawaPay callback must contain exactly one signature input."))
	return next(iter(signature_input_header.values())).params


def _build_callback_request(raw_body: bytes, headers):
	url = frappe.utils.get_url(frappe.request.path)
	if frappe.request.query_string:
		url = f"{url}?{frappe.request.query_string.decode('utf-8')}"
	return Request("POST", url, headers=dict(headers), data=raw_body).prepare()


def _require_signed_components(covered_components):
	covered = {str(component).strip('"') for component in covered_components}
	missing = set(SIGNED_REQUEST_COMPONENTS) - covered
	if missing:
		raise SignatureVerificationError(
			_("PawaPay callback signature is missing required covered components: {0}").format(
				", ".join(sorted(missing))
			)
		)
