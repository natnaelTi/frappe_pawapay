import frappe

from frappe_pawapay.pawapay.countries import (
	get_country_from_pawapay_code,
	get_pawapay_country_code,
	normalize_pawapay_country_code,
)


def execute():
	if not frappe.db.table_exists("PawaPay Collection Account"):
		return

	rows = frappe.db.sql(
		"""
		select name, country, pawapay_country_code
		from `tabPawaPay Collection Account`
		where coalesce(country, '') != '' or coalesce(pawapay_country_code, '') != ''
		""",
		as_dict=True,
	)
	for row in rows:
		try:
			pawapay_country_code = normalize_pawapay_country_code(row.pawapay_country_code) or get_pawapay_country_code(row.country)
		except Exception:
			continue
		country = get_country_from_pawapay_code(pawapay_country_code) or row.country
		frappe.db.set_value(
			"PawaPay Collection Account",
			row.name,
			{"country": country, "pawapay_country_code": pawapay_country_code},
			update_modified=False,
		)
