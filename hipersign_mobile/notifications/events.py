"""Business events -> notifications.

Push payloads stay generic (no customer names, numbers or notes); the app
fetches the authorised details after the user taps the notification.
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
	if doc.get("assigned_to") and doc.assigned_to != actor:
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


def schedule_reminder(doc):
	"""(Re)create the single pending reminder for an open, scheduled task."""
	if doc.status not in OPEN or not doc.get("assigned_to") or not doc.get("due_date"):
		queue.cancel_for_reference("CRM Task", doc.name, "Reminder")
		return
	due = get_datetime(doc.due_date)
	now = timeutil.now_system()
	if due <= now:
		queue.cancel_for_reference("CRM Task", doc.name, "Reminder")
		return
	minutes = queue.get_preferences(doc.assigned_to).reminder_minutes
	at = max(due - dt.timedelta(minutes=minutes), now)
	key = f"remind:{doc.name}:{doc.assigned_to}:{due.isoformat()}:{minutes}"
	queue.cancel_for_reference("CRM Task", doc.name, "Reminder", keep_key=key)
	label = f"in {minutes} min" if minutes and at > now else "soon"
	queue.enqueue(
		doc.assigned_to,
		"Reminder",
		f"Upcoming {_kind(doc)} {label}",
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
