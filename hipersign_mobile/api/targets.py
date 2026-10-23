"""Call targets: the caller's progress today, and managers setting each caller's target."""

from __future__ import annotations

import frappe
from frappe.utils import cint

from hipersign_mobile.api._common import mobile_api, optional_str, require_str
from hipersign_mobile.services import call_targets as ct
from hipersign_mobile.services import permissions as perms


@mobile_api(methods=("GET",))
def my_target():
	me = frappe.session.user
	return {"target": ct.live_progress(me)}


@mobile_api(methods=("GET",))
def list_targets():
	"""Every cold caller (and anyone who has a target) with today's progress - managers."""
	me = frappe.session.user
	perms.require("call_targets", me)
	callers = set(frappe.get_all("Has Role", filters={"role": perms.ROLE_COLD_CALLER, "parenttype": "User"}, pluck="parent"))
	callers |= set(frappe.get_all(ct.DOCTYPE, pluck="user"))
	rows = frappe.get_all(
		"User", filters={"name": ["in", list(callers) or [""]], "enabled": 1}, fields=["name", "full_name"], order_by="full_name asc"
	)
	out = []
	for r in rows:
		t = frappe.get_doc(ct.DOCTYPE, r.name) if frappe.db.exists(ct.DOCTYPE, r.name) else None
		out.append(
			{
				"user": r.name,
				"name": r.full_name or r.name,
				"enabled": bool(t and cint(t.enabled)),
				"daily_target": cint(t.daily_target) if t else 70,
				"check_times": (t.check_times if t else "") or "",
				"idle_minutes": cint(t.idle_minutes) if t else 30,
				"work_start": str(t.work_start or "09:30:00")[:5] if t else "09:30",
				"work_end": str(t.work_end or "18:00:00")[:5] if t else "18:00",
				"count_connected_only": bool(t and cint(t.count_connected_only)),
				"count_new_only": bool(t and cint(t.get("count_new_only"))),
				"meeting_target": cint(t.get("meeting_target")) if t else 0,
				"progress": ct.progress(r.name, t=t) if t and cint(t.enabled) else None,
			}
		)
	return {"items": out}


@mobile_api(methods=("POST",))
def save_target(
	user: str | None = None,
	daily_target: int | str | None = None,
	check_times: str | None = None,
	idle_minutes: int | str | None = None,
	work_start: str | None = None,
	work_end: str | None = None,
	enabled: str | int | bool | None = None,
	count_connected_only: str | int | bool | None = None,
	count_new_only: str | int | bool | None = None,
	meeting_target: int | str | None = None,
):
	me = frappe.session.user
	perms.require("call_targets", me)
	user = require_str(user, "Caller", 140)
	if not frappe.db.exists("User", user):
		raise frappe.DoesNotExistError("User not found.")
	doc = frappe.get_doc(ct.DOCTYPE, user) if frappe.db.exists(ct.DOCTYPE, user) else frappe.new_doc(ct.DOCTYPE)
	doc.user = user

	def flag(v):
		return 1 if str(v).lower() in ("1", "true", "yes", "on") else 0

	if daily_target is not None:
		doc.daily_target = max(cint(daily_target), 0)
	if check_times is not None:
		doc.check_times = optional_str(check_times, 140)
	if idle_minutes is not None:
		doc.idle_minutes = max(cint(idle_minutes), 0)
	if work_start:
		doc.work_start = work_start if work_start.count(":") == 2 else work_start + ":00"
	if work_end:
		doc.work_end = work_end if work_end.count(":") == 2 else work_end + ":00"
	if enabled is not None:
		doc.enabled = flag(enabled)
	elif doc.is_new():
		doc.enabled = 1
	if count_connected_only is not None:
		doc.count_connected_only = flag(count_connected_only)
	if count_new_only is not None:
		doc.count_new_only = flag(count_new_only)
	if meeting_target is not None:
		doc.meeting_target = max(cint(meeting_target), 0)
	doc.set_by = me
	doc.save(ignore_permissions=True) if not doc.is_new() else doc.insert(ignore_permissions=True)
	return {"saved": True, "progress": ct.progress(user, t=doc) if doc.enabled else None}
