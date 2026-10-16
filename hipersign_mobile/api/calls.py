"""Work calls from the phone's call history -> Mobile Call Log (for the Calling Activity report).

The app reads the phone's own call log (it already has that permission for the caller
pop-up) and uploads only work calls: numbers that belong to an ERPNext customer / lead /
deal, or calls started from Hipersign. Personal calls never leave the phone.

New vs Follow-up: a call is "New" until the number has been reached once (a connected
call); every call after a connected call is a "Follow-up".
"""

from __future__ import annotations

import datetime
import json

import frappe
from frappe.utils import cint

from hipersign_mobile.api._common import limit_per_user, mobile_api
from hipersign_mobile.api.customers import _party_for_phone
from hipersign_mobile.services import permissions as perms
from hipersign_mobile.services import settings as hm_settings
from hipersign_mobile.services import timeutil, whatsapp_rules
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
		log = frappe.get_doc(
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
		try:
			whatsapp_rules.after_call(log)
		except Exception:
			frappe.log_error(title="Hipersign WhatsApp rule (after call)")
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


@mobile_api(methods=("GET",))
def whatsapp_status(doctype: str | None = None, name: str | None = None, since: str | None = None):
	"""For the after-call screen: what happened to the automatic WhatsApp messages of this
	customer / lead / deal since `since` (ISO time the call started)."""
	me = frappe.session.user
	perms.require("customers", me)
	if doctype not in perms.PARTY_DOCTYPES or not name:
		raise frappe.ValidationError("Choose a customer, lead or deal.")
	perms.require_party(doctype, name, me)
	when = None
	if since:
		try:
			when = timeutil.parse_client_datetime(since, me) - datetime.timedelta(minutes=2)
		except frappe.ValidationError:
			when = None
	return {
		"enabled": whatsapp_rules.call_rules_enabled(doctype),
		"items": whatsapp_rules.outcomes(doctype, name, when),
	}


KINDS = {"Customer": "Customer", "CRM Lead": "Lead", "CRM Deal": "Deal"}
CALL_FILTERS = ("all", "not_reached", "connected", "not_connected", "incoming", "missed")


@mobile_api(methods=("GET",))
def list_calls(
	scope: str | None = None,
	filter: str | None = None,
	query: str | None = None,
	cursor=None,
	page_size=None,
):
	"""Latest work calls, newest first: mine, or the whole team's (managers)."""
	from hipersign_mobile.api._common import choice, page_args, page_result

	me = frappe.session.user
	perms.require("customers", me)
	caps = perms.capabilities(me)
	scope = choice(scope, ("mine", "team"), "scope", default="mine")
	if scope == "team" and "team_view" not in caps:
		raise frappe.PermissionError("Only managers can see the team's calls.")
	kind = choice(filter, CALL_FILTERS, "filter", default="all")
	offset, limit = page_args(cursor, page_size)
	if kind == "not_reached":
		result = _not_reached(me if scope == "mine" else None, query, offset, limit)
		if not offset:
			result["today"] = _today(me if scope == "mine" else None)
		return result

	filters = []
	if scope == "mine":
		filters.append(["caller", "=", me])
	if kind == "connected":
		filters.append(["connected", "=", 1])
	elif kind == "not_connected":
		filters += [["connected", "=", 0], ["direction", "=", "Outgoing"]]
	elif kind == "incoming":
		filters.append(["direction", "=", "Incoming"])
	elif kind == "missed":
		filters.append(["direction", "=", "Missed"])
	or_filters = None
	q = (query or "").strip()
	if q:
		digits = "".join(ch for ch in q if ch.isdigit())
		or_filters = [["party_name", "like", f"%{q}%"]]
		if len(digits) >= 3:
			or_filters.append(["phone", "like", f"%{digits}%"])

	return _page(
		frappe.get_all(
			"Mobile Call Log",
			filters=filters,
			or_filters=or_filters,
			fields=LOG_FIELDS,
			order_by="call_datetime desc",
			start=offset,
			limit=limit + 1,
		),
		offset,
		limit,
		me,
		scope,
	)


LOG_FIELDS = [
	"name",
	"caller",
	"call_datetime",
	"direction",
	"call_type",
	"connected",
	"duration_seconds",
	"phone",
	"party_doctype",
	"party",
	"party_name",
	"activity",
	"recording",
	"source",
]


def _not_reached(user: str | None, query: str | None, offset: int, limit: int) -> dict:
	"""Numbers called but never reached yet (no connected call by anyone): one row per number,
	newest attempt first. A number leaves the list as soon as any call to / from it connects."""
	cond, args = "", {}
	if user:
		cond += " and l.caller = %(user)s"
		args["user"] = user
	q = (query or "").strip()
	if q:
		digits = "".join(ch for ch in q if ch.isdigit())
		cond += " and (l.party_name like %(q)s" + (" or l.phone like %(d)s)" if len(digits) >= 3 else ")")
		args.update(q=f"%{q}%", d=f"%{digits}%")
	rows = frappe.db.sql(
		f"""select l.phone, count(*) attempts, min(l.call_datetime) first_at, max(l.call_datetime) last_at
		from `tabMobile Call Log` l
		where l.direction = 'Outgoing' {cond}
		and not exists (select 1 from `tabMobile Call Log` c where c.phone = l.phone and c.connected = 1)
		group by l.phone
		order by last_at desc
		limit %(limit)s offset %(offset)s""",
		{**args, "limit": limit + 1, "offset": offset},
		as_dict=True,
	)
	items = []
	for r in rows[:limit]:
		last = frappe.get_all(
			"Mobile Call Log",
			filters={"phone": r.phone, "direction": "Outgoing", **({"caller": user} if user else {})},
			fields=LOG_FIELDS,
			order_by="call_datetime desc",
			limit=1,
		)
		if not last:
			continue
		fill_parties(last)
		item = _item(last[0], {}, {})
		item["attempts"] = cint(r.attempts)
		item["first_at"] = timeutil.to_iso(r.first_at)
		items.append(item)
	from hipersign_mobile.api._common import page_result

	return page_result(items, offset, limit, len(rows) > limit)


def _item(r, names: dict, recs: dict) -> dict:
	if r.caller not in names:
		names[r.caller] = frappe.utils.get_fullname(r.caller)
	audio = recs.get(r.recording)
	return {
		"id": r.name,
		"at": timeutil.to_iso(r.call_datetime),
		"direction": r.direction,
		"call_type": r.call_type,
		"connected": bool(cint(r.connected)),
		"duration": cint(r.duration_seconds),
		"phone": r.phone,
		"party": {
			"kind": KINDS.get(r.party_doctype, r.party_doctype),
			"doctype": r.party_doctype,
			"id": r.party,
			"name": r.party_name or r.party,
		}
		if r.party
		else None,
		"caller": r.caller,
		"caller_name": names[r.caller],
		"activity_id": str(r.activity) if r.activity else None,
		"recording": {"id": r.recording, "ext": (audio or "").rsplit(".", 1)[-1].lower() or None}
		if r.recording and audio
		else None,
		"via_app": r.source == "Hipersign",
	}


def _page(rows, offset, limit, me, scope) -> dict:
	from hipersign_mobile.api._common import page_result

	recs = {
		r.name: r.audio_file
		for r in frappe.get_all(
			"Call Recording",
			filters={"name": ["in", [x.recording for x in rows if x.recording] or [""]]},
			fields=["name", "audio_file"],
		)
	}
	names: dict = {}
	fill_parties(rows[:limit])
	items = [_item(r, names, recs) for r in rows[:limit]]
	result = page_result(items, offset, limit, len(rows) > limit)
	if not offset:
		result["today"] = _today(me if scope == "mine" else None)
	return result


def _today(user: str | None) -> dict:
	start = timeutil.user_today_start(frappe.session.user)
	cond, args = ("and caller = %(user)s", {"user": user}) if user else ("", {})
	r = frappe.db.sql(
		f"""select count(*) total, coalesce(sum(connected), 0) connected,
		coalesce(sum(call_type = 'New'), 0) new_calls, coalesce(sum(call_type = 'Follow-up'), 0) followups,
		coalesce(sum(duration_seconds), 0) talk
		from `tabMobile Call Log` where direction = 'Outgoing' and call_datetime >= %(start)s {cond}""",
		{"start": start, **args},
		as_dict=True,
	)[0]
	return {k: cint(v) for k, v in r.items()}


def fill_parties(rows) -> None:
	"""Calls saved before the number was in ERPNext (the lead was created afterwards, or typed
	in a different format): find the customer / lead / deal now, show its name and remember it."""
	found: dict = {}
	for r in rows:
		if r.get("party") or not r.get("phone"):
			continue
		if r.phone not in found:
			hit = _party_for_phone(r.phone)
			found[r.phone] = hit
			if hit:
				name = (party_summary(*hit) or {}).get("name")
				frappe.db.sql(
					"""update `tabMobile Call Log` set party_doctype=%s, party=%s, party_name=%s
					where phone=%s and ifnull(party, '')=''""",
					(hit[0], hit[1], name, r.phone),
				)
				found[r.phone] = (hit[0], hit[1], name)
		hit = found[r.phone]
		if hit:
			r.party_doctype, r.party, r.party_name = hit


def backfill_parties():
	"""After an update: link earlier unknown-number calls whose number is in ERPNext now."""
	try:
		phones = frappe.db.sql(
			"""select distinct phone from `tabMobile Call Log` where ifnull(party, '')='' and ifnull(phone, '')!=''
			order by phone limit 5000""",
			pluck=True,
		)
		fill_parties([frappe._dict(phone=p, party=None) for p in phones])
	except Exception:
		frappe.log_error(title="Hipersign: link earlier calls to leads")
