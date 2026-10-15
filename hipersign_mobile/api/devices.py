from __future__ import annotations

import frappe
from frappe.utils import cint

from hipersign_mobile.api._common import choice, mobile_api, page_args, page_result, require_str
from hipersign_mobile.notifications import queue
from hipersign_mobile.services import timeutil


@mobile_api(methods=("POST",))
def register_device(
	device_id: str,
	platform: str,
	push_token: str | None = None,
	app_version: str | None = None,
	device_name: str | None = None,
):
	"""Register / refresh this phone for the signed-in user.

	One row per (user, device_id); token rotation updates it in place. If the same
	physical device was registered by another user before, that registration is
	deactivated so pushes never reach the wrong person after a shared-phone sign-in.
	"""
	me = frappe.session.user
	device_id = require_str(device_id, "device_id", 140)
	platform = choice(platform, ("Android", "iOS"), "platform")
	now = timeutil.now_system()

	for other in frappe.get_all(
		"Mobile Device", filters={"device_id": device_id, "user": ["!=", me], "is_active": 1}, pluck="name"
	):
		frappe.db.set_value("Mobile Device", other, "is_active", 0)

	name = frappe.db.get_value("Mobile Device", {"user": me, "device_id": device_id}, "name")
	doc = frappe.get_doc("Mobile Device", name) if name else frappe.new_doc("Mobile Device")
	doc.update(
		{
			"user": me,
			"device_id": device_id,
			"platform": platform,
			"device_name": (device_name or "")[:140] or None,
			"app_version": (app_version or "")[:40] or None,
			"is_active": 1,
			"last_seen_at": now,
		}
	)
	if push_token:
		current = doc.get_password("push_token", raise_exception=False) if name else None
		if current != push_token:
			doc.push_token = push_token[:1024]
			doc.token_updated_at = now
	doc.save(ignore_permissions=True) if name else doc.insert(ignore_permissions=True)
	return {
		"registered": True,
		"has_token": bool(push_token or (name and doc.get_password("push_token", raise_exception=False))),
	}


@mobile_api(methods=("POST",))
def deactivate_device(device_id: str):
	me = frappe.session.user
	for name in frappe.get_all("Mobile Device", filters={"user": me, "device_id": device_id}, pluck="name"):
		frappe.db.set_value("Mobile Device", name, "is_active", 0)
	return {"deactivated": True}


@mobile_api(methods=("GET",))
def get_notification_preferences():
	p = queue.get_preferences(frappe.session.user)
	return {
		"reminder_minutes": cint(p.reminder_minutes),
		"notify_assignments": bool(cint(p.notify_assignments)),
		"notify_reminders": bool(cint(p.notify_reminders)),
		"notify_trials": bool(cint(p.notify_trials)),
		"reminder_choices": [0, 5, 10, 15, 30, 60, 120, 1440],
		"reminder_offsets": list(p.reminder_offsets),
		"alarm_high_priority": bool(cint(p.alarm_high_priority)),
		"notify_whatsapp": bool(cint(p.notify_whatsapp)),
	}


@mobile_api(methods=("POST",))
def update_notification_preferences(
	reminder_minutes: str | int | None = None,
	notify_assignments: str | int | bool | None = None,
	notify_reminders: str | int | bool | None = None,
	notify_trials: str | int | bool | None = None,
	reminder_offsets: str | list | None = None,
	alarm_high_priority: str | int | bool | None = None,
	notify_whatsapp: str | int | bool | None = None,
):
	me = frappe.session.user
	doc = (
		frappe.get_doc("Mobile User Preference", me)
		if frappe.db.exists("Mobile User Preference", me)
		else frappe.new_doc("Mobile User Preference")
	)
	doc.user = me
	if reminder_minutes is not None:
		minutes = cint(reminder_minutes)
		if minutes < 0 or minutes > 1440:
			raise frappe.ValidationError("Reminder must be between 0 and 1440 minutes.")
		doc.reminder_minutes = minutes
	elif doc.is_new():
		doc.reminder_minutes = queue.get_preferences(me).reminder_minutes

	def flag(v):
		return 1 if str(v).lower() in ("1", "true", "yes", "on") else 0

	if reminder_offsets is not None:
		offsets = queue.parse_offsets(reminder_offsets)
		if not offsets:
			raise frappe.ValidationError("Choose at least one reminder.")
		doc.reminder_offsets = queue.offsets_text(offsets)
		doc.reminder_minutes = max(offsets)  # the earliest one (kept for older apps)
	for field, value in (
		("alarm_high_priority", alarm_high_priority),
		("notify_assignments", notify_assignments),
		("notify_reminders", notify_reminders),
		("notify_trials", notify_trials),
		("notify_whatsapp", notify_whatsapp),
	):
		if value is not None:
			doc.set(field, flag(value))
	doc.save(ignore_permissions=True) if not doc.is_new() else doc.insert(ignore_permissions=True)

	if reminder_minutes is not None and reminder_offsets is None:
		doc.db_set("reminder_offsets", None)  # old app: single setting replaces the list
	if reminder_minutes is not None or reminder_offsets is not None:
		# Re-plan pending reminders with the new lead time.
		from hipersign_mobile.notifications.events import schedule_reminder

		for task in frappe.get_all(
			"CRM Task",
			filters={
				"assigned_to": me,
				"status": ["in", ("Backlog", "Todo", "In Progress")],
				"due_date": [">", timeutil.now_system()],
			},
			pluck="name",
			limit=500,
		):
			schedule_reminder(frappe.get_doc("CRM Task", task))
	return {"saved": True}


@mobile_api(methods=("GET",))
def get_notification_history(cursor: str | int | None = None, page_size: str | int | None = None):
	me = frappe.session.user
	offset, limit = page_args(cursor, page_size)
	rows = frappe.get_all(
		"Mobile Notification",
		filters={
			"recipient": me,
			"status": ["in", ("Sent", "Failed", "Queued")],
			"creation": ["<=", timeutil.now_system()],
		},
		or_filters=[["scheduled_for", "is", "not set"], ["scheduled_for", "<=", timeutil.now_system()]],
		fields=[
			"name",
			"event_type",
			"title",
			"body",
			"status",
			"reference_doctype",
			"reference_name",
			"creation",
			"scheduled_for",
			"last_error_code",
		],
		order_by="creation desc",
		start=offset,
		page_length=limit + 1,
	)
	items = [
		{
			"id": r.name,
			"event_type": r.event_type,
			"title": r.title,
			"body": r.body,
			"status": r.status,
			"route": queue.route_for(r.reference_doctype, r.reference_name),
			"at": timeutil.to_iso(r.scheduled_for or r.creation),
			"delivery_problem": r.last_error_code if r.status == "Failed" else None,
		}
		for r in rows[:limit]
	]
	return page_result(items, offset, limit, len(rows) > limit)
