"""CRM Task document events.

Runs for every save of a CRM Task, whether it came from the mobile app, the
Frappe CRM UI or Desk. That keeps three things consistent everywhere:
  1. server-maintained fields (completed_at/by, reschedule_count)
  2. the immutable Sales Activity History audit trail
  3. push notifications (assignment, reschedule) and scheduled reminders
"""

from __future__ import annotations

import frappe
from frappe.utils import cint, get_datetime

from hipersign_mobile.services import timeutil

OPEN = ("Backlog", "Todo", "In Progress")


def _dt(value):
	return get_datetime(value) if value else None


def before_insert(doc, method=None):
	if doc.meta.has_field("hm_activity_type") and not doc.get("hm_activity_type"):
		doc.hm_activity_type = "Follow-up"
	if doc.get("due_date") and not doc.get("start_date"):
		doc.start_date = _dt(doc.due_date).date()
	if doc.get("status") == "Done":
		doc.hm_completed_at = timeutil.now_system()
		doc.hm_completed_by = frappe.session.user
	doc.hm_reschedule_count = 0


def validate(doc, method=None):
	if not doc.meta.has_field("hm_reschedule_count"):
		return

	# An end time before the start is meaningless; fall back to the default duration.
	if doc.get("hm_end_datetime") and doc.get("due_date") and _dt(doc.hm_end_datetime) <= _dt(doc.due_date):
		doc.hm_end_datetime = None

	if doc.is_new():
		return
	before = doc.get_doc_before_save()
	if not before:
		return

	# Reschedule counter: an open activity whose date moves (not its first scheduling).
	# Always derived from the stored value, never from what the client sent.
	count = cint(before.get("hm_reschedule_count"))
	moved = (
		before.get("due_date")
		and doc.get("due_date")
		and _dt(before.due_date) != _dt(doc.due_date)
		and before.status in OPEN
		and doc.status in OPEN
	)
	doc.hm_reschedule_count = count + 1 if moved else count
	if doc.get("due_date"):
		doc.start_date = _dt(doc.due_date).date()

	# Completion stamp.
	if doc.status == "Done" and before.status != "Done":
		doc.hm_completed_at = timeutil.now_system()
		doc.hm_completed_by = frappe.session.user
	elif doc.status != "Done":
		doc.hm_completed_at = None
		doc.hm_completed_by = None
	else:
		doc.hm_completed_at = before.get("hm_completed_at")
		doc.hm_completed_by = before.get("hm_completed_by")


def after_insert(doc, method=None):
	pass  # creation is handled in on_update (Frappe calls on_update after insert as well)


def on_update(doc, method=None):
	from hipersign_mobile.notifications import events as notify
	from hipersign_mobile.services.activity_service import add_history

	if not doc.meta.has_field("hm_reschedule_count"):
		return
	reason = doc.flags.get("hm_reason")
	before = doc.get_doc_before_save()

	if not before:
		add_history(
			doc.name,
			"Created",
			new_status=doc.status,
			new_start_datetime=doc.get("due_date"),
			new_end_datetime=doc.get("hm_end_datetime"),
			new_assigned_to=doc.get("assigned_to"),
			reason=reason,
		)
		notify.on_task_created(doc)
		return

	status_changed = before.status != doc.status
	schedule_changed = _dt(before.get("due_date")) != _dt(doc.get("due_date")) or _dt(
		before.get("hm_end_datetime")
	) != _dt(doc.get("hm_end_datetime"))
	assignee_changed = (before.get("assigned_to") or None) != (doc.get("assigned_to") or None)

	if status_changed:
		add_history(
			doc.name, "Status Changed", old_status=before.status, new_status=doc.status, reason=reason
		)
	if schedule_changed:
		add_history(
			doc.name,
			"Rescheduled",
			old_start_datetime=before.get("due_date"),
			old_end_datetime=before.get("hm_end_datetime"),
			new_start_datetime=doc.get("due_date"),
			new_end_datetime=doc.get("hm_end_datetime"),
			reason=reason,
		)
	if assignee_changed:
		add_history(
			doc.name,
			"Reassigned",
			old_assigned_to=before.get("assigned_to"),
			new_assigned_to=doc.get("assigned_to"),
			reason=reason,
		)

	if status_changed or schedule_changed or assignee_changed:
		notify.on_task_changed(
			doc,
			before,
			status_changed=status_changed,
			schedule_changed=schedule_changed,
			assignee_changed=assignee_changed,
		)


def on_trash(doc, method=None):
	from hipersign_mobile.notifications import queue

	queue.cancel_for_reference("CRM Task", doc.name)
