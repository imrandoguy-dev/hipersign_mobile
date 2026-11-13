"""New numbers to call, found on Google Maps (Google Business Profiles).

A manager searches "solar companies" in "Kochi" (any business type, any place), picks how
many and which callers; numbers already in ERP (customers, leads, contacts, earlier numbers)
are skipped. Callers get them in their "New numbers" list - not as leads. When a call to
one connects and lasts more than 5 seconds it becomes a CRM Lead; otherwise it stays in the
list to try again. Callers can also convert by hand or mark "Not interested". Nothing is
deleted automatically.
"""

from __future__ import annotations

import datetime as dt

import frappe
import requests
from frappe.utils import cint, now_datetime

from hipersign_mobile.services import settings as hm_settings
from hipersign_mobile.services.google_places import ENDPOINT, REGIONS, GoogleLookupError, _api_key
from hipersign_mobile.services.phone import normalize_phone

DOCTYPE = "Hipersign Prospect"
MIN_TALK_SECONDS = 5  # a connected call longer than this turns the number into a lead
OPEN = ("New", "Not Connected")
FIELDS = ",".join(
	"places." + f
	for f in ("id", "displayName", "formattedAddress", "googleMapsUri", "nationalPhoneNumber", "internationalPhoneNumber")
) + ",nextPageToken"
MAX_PAGES = 3  # Google gives at most 60 results (3 x 20) for one search


def _search(text: str, page_token: str | None = None) -> tuple[list[dict], str | None]:
	cc = hm_settings.get().default_country_code
	body = {"textQuery": text, "pageSize": 20}
	if page_token:
		body["pageToken"] = page_token
	if cc in REGIONS:
		body["regionCode"] = REGIONS[cc]
	try:
		r = requests.post(
			ENDPOINT, json=body, headers={"X-Goog-Api-Key": _api_key(), "X-Goog-FieldMask": FIELDS}, timeout=15
		)
	except requests.RequestException:
		raise GoogleLookupError("Google could not be reached. Try again.")
	if r.status_code != 200:
		try:
			message = r.json().get("error", {}).get("message") or ""
		except ValueError:
			message = ""
		frappe.log_error(title="Hipersign: Google business search failed", message=f"HTTP {r.status_code}: {message}")
		raise GoogleLookupError(f"Google search failed (HTTP {r.status_code}). {message[:200]}".strip())
	data = r.json() or {}
	return data.get("places") or [], data.get("nextPageToken")


def known_number(e164: str) -> bool:
	"""Already in ERP: a customer / lead / deal / contact, or an earlier new number."""
	from hipersign_mobile.api.customers import _party_for_phone

	if frappe.db.exists(DOCTYPE, {"phone": e164}):
		return True
	if _party_for_phone(e164):
		return True
	digits = e164.lstrip("+")[-10:]
	return bool(frappe.db.sql("select 1 from `tabContact Phone` where phone like %s limit 1", (f"%{digits}",)))


def _split(value: str) -> list[str]:
	return [x.strip() for x in (value or "").replace("\n", ",").split(",") if x.strip()]


def find(user: str, business: str, places: str, count: int, callers: list[str]) -> dict:
	"""Search Google for each business type in each place until `count` new numbers are found,
	and share them out among `callers` (round robin)."""
	kinds, where = _split(business), _split(places)
	if not kinds:
		raise frappe.ValidationError("Type what kind of businesses to find (e.g. solar companies).")
	if not where:
		raise frappe.ValidationError("Type the place (e.g. Kochi).")
	count = max(1, min(cint(count) or 70, 200))
	if not callers:
		raise frappe.ValidationError("Choose who will call these numbers.")
	cc = hm_settings.get().default_country_code
	batch = f"{dt.date.today().isoformat()} {business} @ {places}"[:140]
	found, skipped_known, no_phone, seen = [], 0, 0, set()
	for place in where:
		for kind in kinds:
			token, pages = None, 0
			while len(found) < count and pages < MAX_PAGES:
				results, token = _search(f"{kind} in {place}", token)
				pages += 1
				for p in results:
					raw = p.get("internationalPhoneNumber") or p.get("nationalPhoneNumber")
					e164 = normalize_phone(raw, cc) if raw else None
					if not e164:
						no_phone += 1
						continue
					if e164 in seen:
						continue
					seen.add(e164)
					if known_number(e164):
						skipped_known += 1
						continue
					found.append({
						"business_name": ((p.get("displayName") or {}).get("text") or e164)[:200],
						"phone": e164,
						"address": (p.get("formattedAddress") or "")[:500] or None,
						"maps_url": p.get("googleMapsUri"),
						"place_id": p.get("id"),
						"search_text": kind[:140],
						"place": place[:140],
					})
					if len(found) >= count:
						break
				if not token:
					break
			if len(found) >= count:
				break
		if len(found) >= count:
			break
	per_caller = {c: 0 for c in callers}
	for i, row in enumerate(found):
		caller = callers[i % len(callers)]
		per_caller[caller] += 1
		frappe.get_doc({"doctype": DOCTYPE, **row, "status": "New", "assigned_to": caller, "batch": batch, "found_by": user}).insert(
			ignore_permissions=True
		)
	if found:
		from hipersign_mobile.notifications import queue

		for caller, n in per_caller.items():
			if n:
				queue.enqueue(
					caller, "Team Alert", f"{n} new numbers to call",
					f"{', '.join(kinds)} in {', '.join(where)}. Open New numbers in the app.",
					dedup_key=f"prs:{batch}:{caller}"[:140], reference_doctype=DOCTYPE, reference_name=None,
				)
	return {
		"found": len(found),
		"skipped_known": skipped_known,
		"no_phone": no_phone,
		"per_caller": [{"user": u, "name": frappe.utils.get_fullname(u), "count": n} for u, n in per_caller.items()],
		"batch": batch,
		"short": len(found) < count,
	}


# ---------------------------------------------------------------- calls

def for_phone(e164: str):
	name = frappe.db.get_value(DOCTYPE, {"phone": e164, "status": ["in", list(OPEN) + ["Not Interested", "Converted"]]}, "name")
	return frappe.get_doc(DOCTYPE, name) if name else None


def convert(prospect, user: str) -> str:
	"""Make it a CRM Lead (once) and mark it Converted. Returns the lead."""
	from hipersign_mobile.api.customers import _lead_source, _lead_status, _party_for_phone

	if prospect.crm_lead and frappe.db.exists("CRM Lead", prospect.crm_lead):
		return prospect.crm_lead
	existing = _party_for_phone(prospect.phone)
	if existing and existing[0] == "CRM Lead":
		lead = existing[1]
	else:
		doc = frappe.get_doc({
			"doctype": "CRM Lead",
			"first_name": prospect.business_name[:140],
			"organization": prospect.business_name[:140],
			"mobile_no": prospect.phone,
			"lead_owner": user,
			"status": _lead_status(),
			"source": _lead_source(True),
		})
		meta = frappe.get_meta("CRM Lead")
		for f, v in (("hm_google_place_id", prospect.place_id), ("hm_google_maps_url", prospect.maps_url), ("hm_address", prospect.address)):
			if v and meta.has_field(f):
				doc.set(f, v)
		doc.insert(ignore_permissions=True)
		lead = doc.name
		frappe.local.hm_party_cache = {}
	prospect.status = "Converted"
	prospect.crm_lead = lead
	prospect.converted_on = now_datetime()
	prospect.save(ignore_permissions=True)
	return lead


def after_call(e164: str, user: str, connected: bool, seconds: int, when) -> tuple[str, str] | None:
	"""Record a call to a new number. Returns ("CRM Lead", name) when it became a lead."""
	p = frappe.db.get_value(DOCTYPE, {"phone": e164, "status": ["in", list(OPEN)]}, "name")
	if not p:
		return None
	prospect = frappe.get_doc(DOCTYPE, p)
	prospect.attempts = cint(prospect.attempts) + 1
	prospect.last_called = when
	prospect.last_duration = seconds
	if connected and seconds > MIN_TALK_SECONDS:
		prospect.save(ignore_permissions=True)
		return ("CRM Lead", convert(prospect, user))
	prospect.status = "Not Connected"
	prospect.save(ignore_permissions=True)
	return None


def serialize(r) -> dict:
	return {
		"id": r.name,
		"name": r.business_name,
		"phone": r.phone,
		"address": r.address,
		"maps_url": r.maps_url,
		"status": r.status,
		"search": r.search_text,
		"place": r.place,
		"attempts": cint(r.attempts),
		"last_called": str(r.last_called) if r.last_called else None,
		"last_duration": cint(r.last_duration),
		"lead": r.crm_lead,
		"assigned_to": r.assigned_to,
		"assigned_name": frappe.utils.get_fullname(r.assigned_to) if r.assigned_to else None,
		"notes": r.notes,
	}
