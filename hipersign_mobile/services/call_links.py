"""Keep calls and recordings on the right customer / lead / deal.

- A call or recording is only linked to a party that actually has that number (the phone
  can remember an old link, e.g. a team member's number once filed under a lead).
- When a number is added or changed on a lead / deal / customer / contact person, earlier
  calls and recordings of that number are linked to it.
- repair(): a sweep over the last days that applies both rules to what is already saved.
"""

from __future__ import annotations

import datetime as dt

import frappe

PARTY = ("CRM Lead", "CRM Deal", "Customer")


def numbers_of(doctype: str, name: str) -> set[str]:
	from hipersign_mobile.services.party import party_summary

	s = party_summary(doctype, name) or {}
	return {p["e164"] for p in s.get("phones") or [] if p.get("e164")}


def owns(doctype: str | None, name: str | None, e164: str | None) -> bool:
	if not (doctype and name and e164):
		return False
	if not frappe.db.exists(doctype, name):
		return False
	return e164 in numbers_of(doctype, name)


def checked_party(doctype, name, e164) -> tuple[str | None, str | None]:
	"""The party sent by the phone if it really has this number; else who has it in ERP."""
	from hipersign_mobile.api.customers import _party_for_phone

	if doctype in PARTY and name and (not e164 or owns(doctype, name, e164)):
		return doctype, name
	if not _is_full_number(e164):
		# a partial number (e.g. read from a file name): trust what the phone chose
		return (doctype, name) if doctype in PARTY and name and frappe.db.exists(doctype, name) else (None, None)
	found = _party_for_phone(e164)
	return found if found else (None, None)


def _is_full_number(e164) -> bool:
	return bool(e164) and str(e164).startswith("+") and len(str(e164)) >= 11


def link_numbers(doctype: str, name: str, numbers=None):
	"""Earlier unlinked calls / recordings of these numbers now belong to this party."""
	from hipersign_mobile.services.party import party_summary

	numbers = [n for n in (numbers or numbers_of(doctype, name)) if n]
	if not numbers:
		return
	title = (party_summary(doctype, name) or {}).get("name")
	frappe.db.sql(
		"""update `tabMobile Call Log` set party_doctype=%(dt)s, party=%(name)s, party_name=%(title)s
		where phone in %(numbers)s and ifnull(party, '') = ''""",
		{"dt": doctype, "name": name, "title": title, "numbers": tuple(numbers)},
	)
	frappe.db.sql(
		"""update `tabCall Recording` set party_doctype=%(dt)s, party=%(name)s, party_name=%(title)s
		where phone in %(numbers)s and ifnull(party, '') = ''""",
		{"dt": doctype, "name": name, "title": title, "numbers": tuple(numbers)},
	)


def on_party_saved(doc, method=None):
	"""doc_event: a lead / deal / customer was added, or its numbers changed."""
	try:
		if method == "on_update" and not doc.is_new():
			before = doc.get_doc_before_save()
			if before and all(before.get(f) == doc.get(f) for f in ("mobile_no", "phone")):
				return
		frappe.local.hm_party_cache = {}
		link_numbers(doc.doctype, doc.name)
	except Exception:
		frappe.log_error(title="Hipersign: could not link earlier calls to this record")


def on_contact_saved(doc, method=None):
	"""doc_event: a contact person (with numbers) was added to / changed on a party."""
	try:
		from hipersign_mobile.services.phone import normalize_phone
		from hipersign_mobile.services import settings as hm_settings

		cc = hm_settings.get().default_country_code
		numbers = [normalize_phone(p.phone, cc) for p in doc.get("phone_nos") or []]
		numbers += [normalize_phone(doc.get("mobile_no"), cc)] if doc.get("mobile_no") else []
		numbers = [n for n in numbers if n]
		if not numbers:
			return
		frappe.local.hm_party_cache = {}
		for link in doc.get("links") or []:
			if link.link_doctype in PARTY and link.link_name:
				link_numbers(link.link_doctype, link.link_name, numbers)
	except Exception:
		frappe.log_error(title="Hipersign: could not link earlier calls to this contact")


def repair(days: int = 90) -> dict:
	"""Fix saved calls / recordings: wrong party (doesn't have the number) or none."""
	from hipersign_mobile.services.party import party_summary

	since = frappe.utils.now_datetime() - dt.timedelta(days=days)
	cache, fixed = {}, {"Mobile Call Log": 0, "Call Recording": 0}
	for doctype, when in (("Mobile Call Log", "call_datetime"), ("Call Recording", "call_started_at")):
		rows = frappe.get_all(
			doctype, filters={when: [">=", since]}, fields=["name", "phone", "party_doctype", "party"], limit=20000
		)
		for r in rows:
			if not _is_full_number(r.phone):
				continue
			key = (r.phone, r.party_doctype, r.party)
			if key not in cache:
				cache[key] = checked_party(r.party_doctype, r.party, r.phone)
			dt_, name = cache[key]
			if (dt_, name) == (r.party_doctype or None, r.party or None):
				continue
			title = (party_summary(dt_, name) or {}).get("name") if name else None
			frappe.db.set_value(doctype, r.name, {"party_doctype": dt_, "party": name, "party_name": title}, update_modified=False)
			fixed[doctype] += 1
	return fixed
