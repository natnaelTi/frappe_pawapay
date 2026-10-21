from __future__ import annotations

import frappe
from frappe import _


ISO2_TO_PAWAPAY_COUNTRY = {
	"BJ": "BEN",
	"CM": "CMR",
	"CD": "COD",
	"CG": "COG",
	"GA": "GAB",
	"CI": "CIV",
	"KE": "KEN",
	"RW": "RWA",
	"SN": "SEN",
	"SL": "SLE",
	"UG": "UGA",
	"ZM": "ZMB",
}

PAWAPAY_COUNTRY_TO_ISO2 = {value: key for key, value in ISO2_TO_PAWAPAY_COUNTRY.items()}


def normalize_pawapay_country_code(country_code: str | None) -> str | None:
	country_code = (country_code or "").strip().upper()
	if not country_code:
		return None
	if country_code in ISO2_TO_PAWAPAY_COUNTRY:
		return ISO2_TO_PAWAPAY_COUNTRY[country_code]
	if country_code in PAWAPAY_COUNTRY_TO_ISO2:
		return country_code
	return None


def get_pawapay_country_code(country: str | None) -> str | None:
	if not country:
		return None
	direct = normalize_pawapay_country_code(country)
	if direct:
		return direct
	iso2 = frappe.get_cached_value("Country", country, "code")
	code = normalize_pawapay_country_code(iso2)
	if not code:
		frappe.throw(_("Country {0} is not supported by PawaPay.").format(country))
	return code


def get_country_from_pawapay_code(pawapay_country_code: str | None) -> str | None:
	iso2 = PAWAPAY_COUNTRY_TO_ISO2.get((pawapay_country_code or "").strip().upper())
	if not iso2:
		return None
	return frappe.db.get_value("Country", {"code": iso2}, "name")
