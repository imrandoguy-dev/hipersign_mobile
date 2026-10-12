"""Work calls from the phone's call history -> Mobile Call Log (for the Calling Activity report).

The app reads the phone's own call log (it already has that permission for the caller
pop-up) and uploads only work calls: numbers that belong to an ERPNext customer / lead /
deal, or calls started from Hipersign. Personal calls never leave the phone.

New vs Follow-up: a call is "New" until the number has been reached once (a connected
call); every call after a connected call is a "Follow-up".
"""

from __future__ import annotations

import json

import frappe
from frappe.utils import cint

from hipersign_mobile.api._common import limit_per_user, mobile_api
from hipersign_mobile.api.customers import _party_for_phone
from hipersign_mobile.services import permissions as perms
from hipersign_mobile.services import settings as hm_settings
from hipersign_mobile.services import timeutil
from hipersign_mobile.services.party import party_summary
from hipersign_mobile.services.phone import normalize_phone

DIRECTIONS = {"outgoing": "Outgoing", "incoming": "Incoming", "missed": "Missed"}
MAX_BATCH = 300


def _reached_before(phone: str, party_doctype: str | None, party: str | None, when) -> bool:
	"""Has anyone in the company had a connected call with this number / party before `when`?"""
	cond = "phone = %(phone)s"
	if party_doctype and party:
		cond = "(phone = %(phone)s or (party_doctype = %(pdt)s and party = %(party)s))"
	return bool(
		frappe.db.sql(
			f"""select name from `tabMobile Call Log`
			where {cond} and connected = 1 and call_datetime < %(when)s limit 1""",
			{"phone": phone, "pdt": party_doctype, "party": party, "when": when},
		)
	)


def _promote_later_calls(phone: str, party_doctype: str | None, party: str | None, when):
	"""A connected call synced late: calls after it to the same number are Follow-ups."""
	cond = "phone = %(phone)s"
	if party_doctype and party:
		cond = "(phone = %(phone)s or (party_doctype = %(pdt)s and party = %(party)s))"
	frappe.db.sql(
		f"""update `tabMobile Call Log` set call_type = 'Follow-up'
		where {cond} and call_datetime > %(when)s and call_type = 'New'""",
		{"phone": phone, "pdt": party_doctype, "party": party, "when": when},
	)


def _recording_for(user: str, phone: str, when) -> str | None:
	"""The Call Recording of this call (same caller and number, started within 3 minutes)."""
	rows = frappe.db.sql(
		"""select name from `tabCall Recording`
		where recorded_by = %(user)s and phone = %(phone)s
		and call_started_at between %(when)s - interval 3 minute and %(when)s + interval 3 minute
		order by abs(timestampdiff(second, call_started_at, %(when)s)) limit 1""",
		{"user": user, "phone": phone, "when": when},
	)
	return rows[0][0] if rows else None


@mobile_api(methods=("POST",))
def sync(calls: str | list | None = None):
	"""Upload calls: [{key, number, started_at (ISO), duration, direction, via_app,
	party_doctype?, party?, activity?}]. Safe to resend: each key is stored once."""
	me = frappe.session.user
	perms.require("customers", me)
	limit_per_user("calls_sync", 20, 60)
	if isinstance(calls, str):
		try:
			calls = json.loads(calls)
		except ValueError:
			raise frappe.ValidationError("calls must be a JSON list.")
	if not isinstance(calls, list):
		raise frappe.ValidationError("calls must be a list.")
	if len(calls) > MAX_BATCH:
		raise frappe.ValidationError(f"Send at most {MAX_BATCH} calls at a time.")

	cc = hm_settings.get().default_country_code
	rows = []
	for c in calls:
		if not isinstance(c, dict):
			continue
		phone = normalize_phone(c.get("number"), cc)
		key = str(c.get("key") or "").strip()[:100]
		if not phone or not key:
			continue
		try:
			when = timeutil.parse_client_datetime(c.get("started_at"), me)
		except frappe.ValidationError:
			continue
		rows.append((when, phone, key, c))
	rows.sort(key=lambda r: r[0])  # oldest first, so New/Follow-up is decided in order

	saved = skipped = 0
	for when, phone, key, c in rows:
		sync_key = f"{me}:{key}"
		if frappe.db.exists("Mobile Call Log", {"sync_key": sync_key}):
			skipped += 1
			continue
		party_doctype, party = c.get("party_doctype"), c.get("party")
		if not (party_doctype in perms.PARTY_DOCTYPES and party and frappe.db.exists(party_doctype, party)):
			found = _party_for_phone(phone)
			party_doctype, party = found if found else (None, None)
		via_app = bool(c.get("via_app"))
		if not party and not via_app:
			skipped += 1  # not a work call: personal numbers are never stored
			continue
		direction = DIRECTIONS.get(str(c.get("direction") or "").lower(), "Outgoing")
		duration = max(cint(c.get("duration")), 0)
		connected = direction != "Missed" and duration > 0
		activity = c.get("activity")
		if activity and not frappe.db.exists("CRM Task", activity):
			activity = None
		summary = party_summary(party_doctype, party) if party else None
		frappe.get_doc(
			{
				"doctype": "Mobile Call Log",
				"caller": me,
				"call_datetime": when,
				"direction": direction,
				"call_type": "Follow-up" if _reached_before(phone, party_doctype, party, when) else "New",
				"connected": 1 if connected else 0,
				"duration_seconds": duration,
				"phone": phone,
				"party_doctype": party_doctype,
				"party": party,
				"party_name": (summary or {}).get("name"),
				"activity": activity,
				"recording": _recording_for(me, phone, when),
				"source": "Hipersign" if via_app else "Phone App",
				"sync_key": sync_key,
			}
		).insert(ignore_permissions=True)
		if connected:
			_promote_later_calls(phone, party_doctype, party, when)
		saved += 1
	return {"saved": saved, "skipped": skipped}


@mobile_api(methods=("GET",))
def today_summary():
	"""The caller's own numbers for today (for a small counter in the app)."""
	me = frappe.session.user
	perms.require("customers", me)
	start = timeutil.user_today_start(me)
	r = frappe.db.sql(
		"""select count(*) total, coalesce(sum(connected), 0) connected,
		coalesce(sum(call_type = 'New'), 0) new_calls, coalesce(sum(call_type = 'Follow-up'), 0) followups,
		coalesce(sum(duration_seconds), 0) talk
		from `tabMobile Call Log` where caller = %s and direction = 'Outgoing' and call_datetime >= %s""",
		(me, start),
		as_dict=True,
	)[0]
	return {k: cint(v) for k, v in r.items()}
