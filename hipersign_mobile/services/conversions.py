"""What a caller turned into business on a day: new deals they made, and follow-ups that
ended with a meeting booked. Shown in the daily / team report."""

from __future__ import annotations

import datetime as dt

import frappe

from hipersign_mobile.services import timeutil

CREATED_BY = "hm_created_by"


def stamp_creator(doc, method=None):
	"""CRM Deal before_insert: remember who made the deal (the app makes deals as Administrator,
	so `owner` doesn't say who did it)."""
	if not doc.meta.has_field(CREATED_BY) or doc.get(CREATED_BY):
		return
	user = frappe.session.user
	if user and user not in ("Administrator", "Guest"):
		doc.set(CREATED_BY, user)


def _between(start, end):
	return ["between", [start, end - dt.timedelta(seconds=1)]]


def _local(value, tz):
	from hipersign_mobile.services.daily_report import _local as local

	return local(value, tz)


def _name(doctype, name) -> str:
	from hipersign_mobile.services.daily_report import _party_name

	return _party_name(doctype, name)


def deals_made(user: str, start, end) -> list[dict]:
	"""Deals this person made in [start, end), newest last."""
	if not frappe.db.exists("DocType", "CRM Deal"):
		return []
	fields = ["name", "creation", "lead", "status"]
	if frappe.get_meta("CRM Deal").has_field(CREATED_BY):
		rows = frappe.get_all("CRM Deal", filters={CREATED_BY: user, "creation": _between(start, end)}, fields=fields, limit=500)
		seen = {r.name for r in rows}
		# Made in the desk before the field existed / without it set: the owner made it.
		rows += [
			r for r in frappe.get_all(
				"CRM Deal", filters={CREATED_BY: ["is", "not set"], "owner": user, "creation": _between(start, end)},
				fields=fields, limit=500,
			) if r.name not in seen
		]
	else:
		rows = frappe.get_all("CRM Deal", filters={"owner": user, "creation": _between(start, end)}, fields=fields, limit=500)
	tz = timeutil.user_tz(user)
	rows.sort(key=lambda r: r.creation)
	return [
		{
			"id": r.name,
			"name": _name("CRM Deal", r.name),
			"status": r.status,
			"from_lead": bool(r.lead),
			"at": _local(r.creation, tz).strftime("%I:%M %p").lstrip("0"),
		}
		for r in rows
	]


def followups_to_meetings(user: str, start, end) -> list[dict]:
	"""Follow-ups this person had that day (due or finished then) where they booked a meeting
	with the same customer / lead / deal that day."""
	meta = frappe.get_meta("CRM Task")
	if not meta.has_field("hm_activity_type"):
		return []
	meetings = frappe.get_all(
		"CRM Task",
		filters={"owner": user, "hm_activity_type": "Meeting", "status": ["!=", "Canceled"], "creation": _between(start, end)},
		fields=["name", "reference_doctype", "reference_docname", "due_date", "creation"]
		+ (["hm_meeting_group"] if meta.has_field("hm_meeting_group") else []),
		order_by="creation asc",
		limit=500,
	)
	if not meetings:
		return []
	parties = {(m.reference_doctype, m.reference_docname) for m in meetings if m.reference_docname}
	if not parties:
		return []
	or_time = [["due_date", "between", [start, end - dt.timedelta(seconds=1)]]]
	if meta.has_field("hm_completed_at"):
		or_time.append(["hm_completed_at", "between", [start, end - dt.timedelta(seconds=1)]])
	followups = frappe.get_all(
		"CRM Task",
		filters=[
			["assigned_to", "=", user],
			["hm_activity_type", "in", ["Follow-up", "", None]],
			["status", "!=", "Canceled"],
			["reference_docname", "in", [p[1] for p in parties]],
		],
		or_filters=or_time,
		fields=["name", "title", "status", "reference_doctype", "reference_docname"],
		limit=1000,
	)
	by_party = {}
	for f in followups:
		key = (f.reference_doctype, f.reference_docname)
		if key in parties:
			by_party.setdefault(key, f)
	tz = timeutil.user_tz(user)
	out, seen = [], set()
	for m in meetings:
		key = (m.reference_doctype, m.reference_docname)
		f = by_party.get(key)
		group = m.get("hm_meeting_group") or m.name
		if not f or key in seen or group in seen:
			continue
		seen.update({key, group})
		at = _local(m.due_date, tz)
		out.append({
			"party_doctype": key[0],
			"party": key[1],
			"name": _name(*key),
			"followup": str(f.name),
			"followup_title": f.title or "Follow-up",
			"followup_done": f.status == "Done",
			"meeting": str(m.name),
			"meeting_at": at.strftime("%d %b, %I:%M %p").replace(" 0", " ") if at else "",
		})
	return out
