"""New numbers from Google Maps: managers find them, callers call them (see services/prospects)."""

from __future__ import annotations

import json

import frappe

from hipersign_mobile.api._common import limit_per_user, mobile_api, optional_str, page_args, page_result, require_str
from hipersign_mobile.services import permissions as perms
from hipersign_mobile.services import prospects as svc
from hipersign_mobile.services.google_places import GoogleLookupError

TABS = {
	"to_call": ["New", "Not Connected"],
	"new": ["New"],
	"not_connected": ["Not Connected"],
	"converted": ["Converted"],
	"not_interested": ["Not Interested"],
}


def _get(prospect_id: str, me: str):
	prospect_id = require_str(prospect_id, "Number", 140)
	if not frappe.db.exists(svc.DOCTYPE, prospect_id):
		raise frappe.DoesNotExistError("Not found.")
	p = frappe.get_doc(svc.DOCTYPE, prospect_id)
	if p.assigned_to != me and "team_view" not in perms.capabilities(me):
		raise frappe.DoesNotExistError("Not found.")
	return p


@mobile_api(methods=("GET",))
def callers():
	"""Who can be given new numbers: cold callers and sales people."""
	perms.require("prospect_search", frappe.session.user)
	users = set(
		frappe.get_all(
			"Has Role",
			filters={"role": ["in", (perms.ROLE_COLD_CALLER, perms.ROLE_SALES)], "parenttype": "User"},
			pluck="parent",
		)
	)
	rows = frappe.get_all(
		"User", filters={"name": ["in", list(users) or [""]], "enabled": 1}, fields=["name", "full_name"], order_by="full_name asc"
	)
	return {"items": [{"id": r.name, "name": r.full_name or r.name} for r in rows]}


@mobile_api(methods=("POST",))
def find(business: str | None = None, places: str | None = None, count: str | int | None = None, callers: str | list | None = None):
	"""Search Google Maps and hand the new numbers to the callers."""
	me = frappe.session.user
	perms.require("prospect_search", me)
	limit_per_user("prospect_find", 10, 3600)
	if isinstance(callers, str):
		callers = json.loads(callers) if callers.strip().startswith("[") else [c for c in callers.split(",") if c.strip()]
	callers = [c for c in (callers or []) if frappe.db.get_value("User", c, "enabled")]
	try:
		return svc.find(me, optional_str(business, 300) or "", optional_str(places, 300) or "", count, callers)
	except GoogleLookupError as e:
		raise frappe.ValidationError(str(e))


@mobile_api(methods=("GET",))
def get_list(tab: str | None = None, person: str | None = None, cursor: str | int | None = None, page_size: str | int | None = None):
	me = frappe.session.user
	perms.require("prospects", me)
	statuses = TABS.get(tab or "to_call", TABS["to_call"])
	filters = {"status": ["in", statuses]}
	if person and person != me:
		if "team_view" not in perms.capabilities(me):
			raise frappe.PermissionError("You can only see your own numbers.")
		if person != "all":
			filters["assigned_to"] = person
	else:
		filters["assigned_to"] = me
	offset, limit = page_args(cursor, page_size)
	order = "attempts asc, creation asc" if tab in (None, "to_call", "new", "not_connected") else "modified desc"
	rows = frappe.get_all(svc.DOCTYPE, filters=filters, fields=["*"], order_by=order, start=offset, limit=limit + 1)
	counts = {
		t: frappe.db.count(svc.DOCTYPE, {**{k: v for k, v in filters.items() if k != "status"}, "status": ["in", s]})
		for t, s in TABS.items() if t in ("to_call", "converted", "not_interested")
	}
	page = page_result([svc.serialize(r) for r in rows[:limit]], offset, limit, len(rows) > limit)
	page["counts"] = counts
	return page


@mobile_api(methods=("POST",))
def convert(prospect_id: str | None = None):
	me = frappe.session.user
	perms.require("prospects", me)
	p = _get(prospect_id, me)
	lead = svc.convert(p, me)
	from hipersign_mobile.services.party import party_summary

	return {"prospect": svc.serialize(frappe.get_doc(svc.DOCTYPE, p.name)), "party": party_summary("CRM Lead", lead)}


@mobile_api(methods=("POST",))
def set_status(prospect_id: str | None = None, status: str | None = None, notes: str | None = None):
	"""Not interested / back to the list, with an optional note."""
	me = frappe.session.user
	perms.require("prospects", me)
	p = _get(prospect_id, me)
	if status not in ("Not Interested", "New"):
		raise frappe.ValidationError("Status must be Not Interested or New.")
	if p.status == "Converted":
		raise frappe.ValidationError("This number is already a lead.")
	p.status = status
	note = optional_str(notes, 1000)
	if note:
		p.notes = f"{p.notes}\n{note}" if p.notes else note
	p.save(ignore_permissions=True)
	return {"prospect": svc.serialize(p)}
