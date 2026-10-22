"""Business events -> notifications.

The stored notification is generic (time only). When it is sent, the text is rebuilt from the
activity as it is *then* - customer, follow-up title, notes, latest note - unless
"Show Customer and Notes in Notifications" is switched off in Hipersign Mobile Settings.
"""

from __future__ import annotations

import datetime as dt

import frappe
from frappe.utils import format_datetime, get_datetime

from hipersign_mobile.notifications import queue
from hipersign_mobile.services import timeutil

OPEN = ("Backlog", "Todo", "In Progress")


def _kind(doc) -> str:
	t = (doc.get("hm_activity_type") or "Follow-up").lower()
	return {"follow-up": "follow-up", "meeting": "meeting", "trial": "trial"}.get(t, "activity")


def _when(value) -> str:
	return format_datetime(value, "d MMM, h:mm a") if value else "no date set"


def on_task_created(doc):
	actor = frappe.session.user
	# Trials: the trainer already gets "New trial booked".
	if doc.get("assigned_to") and doc.assigned_to != actor and not doc.get("hm_trial_booking"):
		queue.enqueue(
			doc.assigned_to,
			"Assignment",
			f"New {_kind(doc)} assigned",
			f"Scheduled for {_when(doc.get('due_date'))}. Tap to open.",
			dedup_key=f"assign:{doc.name}:{doc.assigned_to}:{doc.modified}",
			reference_doctype="CRM Task",
			reference_name=doc.name,
		)
	schedule_reminder(doc)


def on_task_changed(doc, before, *, status_changed, schedule_changed, assignee_changed):
	actor = frappe.session.user
	if assignee_changed:
		queue.cancel_for_reference("CRM Task", doc.name, "Reminder")
		if doc.get("assigned_to") and doc.assigned_to != actor and doc.status in OPEN:
			queue.enqueue(
				doc.assigned_to,
				"Assignment",
				f"New {_kind(doc)} assigned",
				f"Scheduled for {_when(doc.get('due_date'))}. Tap to open.",
				dedup_key=f"assign:{doc.name}:{doc.assigned_to}:{doc.modified}",
				reference_doctype="CRM Task",
				reference_name=doc.name,
			)
	elif schedule_changed and doc.get("assigned_to") and doc.assigned_to != actor and doc.status in OPEN:
		queue.enqueue(
			doc.assigned_to,
			"Rescheduled",
			f"Your {_kind(doc)} was rescheduled",
			f"New time: {_when(doc.get('due_date'))}.",
			dedup_key=f"resched:{doc.name}:{doc.get('due_date')}:{doc.modified}",
			reference_doctype="CRM Task",
			reference_name=doc.name,
		)

	if doc.status not in OPEN:
		queue.cancel_for_reference("CRM Task", doc.name, "Reminder")
	elif status_changed or schedule_changed or assignee_changed:
		schedule_reminder(doc)


def _before_label(minutes: int) -> str:
	if minutes % 1440 == 0:
		days = minutes // 1440
		return "tomorrow" if days == 1 else f"in {days} days"
	if minutes >= 60 and minutes % 60 == 0:
		return f"in {minutes // 60} h"
	return f"in {minutes} min"


def reminder_offsets_for(doc) -> list[int]:
	"""The follow-up's own reminders if set, else the person's reminder setting."""
	try:
		own = queue.parse_offsets(doc.get("hm_reminder_offsets"))
	except frappe.ValidationError:
		own = None
	return own or queue.get_preferences(doc.assigned_to).reminder_offsets


def schedule_reminder(doc):
	"""(Re)create the pending reminders for an open, scheduled task - one per chosen offset
	(e.g. 15 minutes before and again at the start time)."""
	if doc.status not in OPEN or not doc.get("assigned_to") or not doc.get("due_date"):
		queue.cancel_for_reference("CRM Task", doc.name, "Reminder")
		return
	due = get_datetime(doc.due_date)
	now = timeutil.now_system()
	if due <= now:
		queue.cancel_for_reference("CRM Task", doc.name, "Reminder")
		return
	base = f"remind:{doc.name}:{doc.assigned_to}:{due.isoformat()}"
	plan = []  # (key, title, at)
	missed_early = False
	for minutes in reminder_offsets_for(doc):
		at = due - dt.timedelta(minutes=minutes)
		if minutes == 0:
			plan.append((f"{base}:0", f"{_kind(doc).capitalize()} now", due))
		elif at > now:
			plan.append((f"{base}:{minutes}", f"Upcoming {_kind(doc)} {_before_label(minutes)}", at))
		else:
			missed_early = True
	if missed_early and not any(at <= now + dt.timedelta(minutes=1) for _k, _t, at in plan):
		# Created / moved too late for the early reminders: one "soon" reminder right away.
		plan.append((f"{base}:soon", f"Upcoming {_kind(doc)} soon", now))
	queue.cancel_for_reference("CRM Task", doc.name, "Reminder", keep_keys=[k for k, _t, _a in plan])
	for key, title, at in plan:
		queue.enqueue(
			doc.assigned_to,
			"Reminder",
			title,
			f"Starts at {_when(due)}. Tap to open.",
			dedup_key=key,
			reference_doctype="CRM Task",
			reference_name=doc.name,
			scheduled_for=at,
		)


def on_trial_event(booking, event: str, recipients: list[str]):
	"""event: booked | rescheduled | cancelled | completed"""
	titles = {
		"booked": "New trial booked",
		"rescheduled": "Trial rescheduled",
		"cancelled": "Trial cancelled",
		"completed": "Trial completed",
	}
	actor = frappe.session.user
	for user in {r for r in recipients if r and r != actor}:
		queue.enqueue(
			user,
			"Trial Update",
			titles[event],
			f"{booking.booking_type} on {_when(booking.start_datetime)}. Tap to open.",
			dedup_key=f"trial:{booking.name}:{event}:{user}:{booking.start_datetime}:{booking.modified}",
			reference_doctype="Trial Booking",
			reference_name=booking.name,
		)


# ---------------------------------------------------------------- detailed text (at send time)


def _clip(text: str | None, limit: int) -> str:
	text = " ".join((text or "").split())
	return (text[: limit - 1] + "…") if len(text) > limit else text


def detailed_text(n) -> tuple[str, str] | None:
	"""(title, body) with the customer and notes for a CRM Task notification, or None."""
	if n.reference_doctype != "CRM Task" or not n.reference_name:
		return None
	if not frappe.db.exists("CRM Task", n.reference_name):
		return None
	from hipersign_mobile.api.notes import html_to_text
	from hipersign_mobile.services.party import party_summary

	task = frappe.get_doc("CRM Task", n.reference_name)
	party = None
	if task.reference_doctype and task.reference_docname:
		try:
			party = (party_summary(task.reference_doctype, task.reference_docname) or {}).get("name")
		except Exception:
			party = None
	headline = (n.title or "Hipersign").strip()
	title = _clip(f"{headline} · {party}" if party else headline, 120)

	lines = []
	when = _when(task.get("due_date")) if task.get("due_date") else None
	lines.append(" · ".join(x for x in (when, _clip(task.title, 120)) if x))
	place = task.get("custom_location")
	if place:
		lines.append(f"Place: {_clip(place, 100)}")
	notes = html_to_text(task.get("description"))
	if notes and notes.strip() != (task.title or "").strip():
		lines.append(f"Notes: {_clip(notes, 300)}")
	last = frappe.get_all(
		"Sales Activity History",
		filters={"crm_task": task.name, "event_type": "Note Added"},
		fields=["reason"],
		order_by="event_datetime desc",
		limit=1,
	)
	if last and last[0].reason:
		lines.append(f"Last note: {_clip(last[0].reason, 200)}")
	return title, "\n".join(lines)
