"""Development tasks: work assigned to developers, with a deadline and alerts.

Alerts (push to the Hipersign app + the bell / email in ERPNext):
- when a task is assigned to a developer;
- before the deadline, at the times chosen on the task (default: Hipersign Mobile Settings);
- at the deadline itself, if it isn't done: "deadline crossed" - to the developer and to
  whoever assigned it;
- every morning after that (Settings > Daily Overdue Alert At) until it is done or cancelled;
- when the developer marks it done, or moves the deadline, whoever assigned it is told.

Each alert is sent once (remembered on the task, keyed by the deadline - moving the deadline
starts the reminders again).
"""

from __future__ import annotations

import datetime as dt
import json

import frappe
from frappe.utils import cint, get_datetime, get_time

from hipersign_mobile.services import settings as hm_settings
from hipersign_mobile.services import timeutil

DOCTYPE = "Development Task"
OPEN = ("Open", "In Progress", "In Review")
STATUSES = ("Open", "In Progress", "In Review", "Done", "Cancelled")
PRIORITIES = ("Low", "Medium", "High", "Urgent")


# ---------------------------------------------------------------- helpers


def reminder_offsets(doc) -> list[int]:
	from hipersign_mobile.notifications.queue import parse_offsets

	for value in (doc.get("reminders"), hm_settings.get().dev_default_reminders):
		try:
			got = parse_offsets(value)
		except frappe.ValidationError:
			got = None
		if got:
			return [m for m in got if m > 0]
	return []


def _when(value) -> str:
	d = get_datetime(value)
	return d.strftime("%a %d %b, %I:%M %p").replace(" 0", " ")


def _late_text(deadline, now) -> str:
	late = now - get_datetime(deadline)
	days = late.days
	if days >= 1:
		return f"{days} day{'s' if days > 1 else ''} late"
	hours = late.seconds // 3600
	return f"{hours} h late" if hours else "just now"


def _sent(doc) -> set:
	try:
		return set(json.loads(doc.get("alerts_sent") or "[]"))
	except ValueError:
		return set()


def _mark(doc, keys: set):
	keys = sorted(keys)[-300:]
	frappe.db.set_value(DOCTYPE, doc.name, "alerts_sent", json.dumps(keys), update_modified=False)
	doc.alerts_sent = json.dumps(keys)


def notify(doc, users, key: str, title: str, body: str):
	"""Push + bell (+ email per the user's ERPNext notification settings), once per key and user."""
	from hipersign_mobile.notifications import queue

	sent = _sent(doc)
	if key in sent:
		return False
	users = [u for u in dict.fromkeys(users) if u and u not in ("Guest",)]
	for user in users:
		try:
			queue.enqueue(
				user,
				"Development",
				title,
				body,
				dedup_key=f"dev:{doc.name}:{key}:{user}"[:140],
				reference_doctype=DOCTYPE,
				reference_name=doc.name,
			)
		except Exception:
			frappe.log_error(title="Hipersign development alert (push)")
	try:
		from frappe.desk.doctype.notification_log.notification_log import enqueue_create_notification

		if users:
			enqueue_create_notification(
				users,
				{
					"type": "Alert",
					"document_type": DOCTYPE,
					"document_name": doc.name,
					"subject": f"{title}: {body}"[:500],
					"email_content": body,
					"from_user": frappe.session.user if frappe.session.user not in users else None,
				},
			)
	except Exception:
		frappe.log_error(title="Hipersign development alert (bell)")
	sent.add(key)
	_mark(doc, sent)
	return True


# ---------------------------------------------------------------- events from the controller


def on_saved(doc, before=None):
	me = frappe.session.user
	if before is None or before.developer != doc.developer:
		if doc.developer and doc.developer != me and doc.status in OPEN:
			who = frappe.utils.get_fullname(doc.assigned_by or me)
			notify(
				doc,
				[doc.developer],
				f"assigned:{doc.developer}:{doc.modified}",
				"New development task",
				f"{doc.title} - deadline {_when(doc.deadline)} (from {who})",
			)
	if before is None:
		return
	if before.status != doc.status and doc.status == "Done" and doc.assigned_by and doc.assigned_by != me:
		notify(doc, [doc.assigned_by], f"done:{doc.modified}", "Development done", f"{doc.title} - by {doc.developer_name or doc.developer}")
	if str(before.deadline) != str(doc.deadline) and doc.status in OPEN:
		others = [u for u in (doc.developer, doc.assigned_by) if u and u != me]
		if others:
			notify(
				doc,
				others,
				f"moved:{doc.deadline}",
				"Deadline changed",
				f"{doc.title} - now {_when(doc.deadline)} (was {_when(before.deadline)})",
			)


# ---------------------------------------------------------------- scheduler


def process_alerts(now: dt.datetime | None = None):
	"""Every minute: reminders before the deadline, deadline crossed, morning overdue alerts."""
	now = now or timeutil.now_system()
	if not frappe.db.table_exists(DOCTYPE):
		return
	s = hm_settings.get()
	try:
		morning = get_time(s.dev_morning_alert_time or "09:30:00")
	except Exception:
		morning = dt.time(9, 30)
	horizon = now + dt.timedelta(days=8)  # reminders at most a week ahead
	for name in frappe.get_all(
		DOCTYPE,
		filters={"status": ["in", OPEN], "deadline": ["<", horizon]},
		pluck="name",
		limit=2000,
	):
		try:
			_check(frappe.get_doc(DOCTYPE, name), now, morning)
		except Exception:
			frappe.log_error(title=f"Hipersign development alerts: {name}")


def _check(doc, now: dt.datetime, morning: dt.time):
	deadline = get_datetime(doc.deadline)
	tag = deadline.strftime("%Y%m%d%H%M")
	if now < deadline:
		for minutes in reminder_offsets(doc):
			at = deadline - dt.timedelta(minutes=minutes)
			# Only the reminder that is due now (not old ones when a task is created late).
			if at <= now < at + dt.timedelta(minutes=30):
				left = _left_text(deadline - now)
				notify(doc, [doc.developer], f"rem:{tag}:{minutes}", "Deadline coming up", f"{doc.title} - due {_when(deadline)} ({left} left)")
		return

	# Past the deadline and not done.
	if not cint(doc.deadline_crossed):
		frappe.db.set_value(DOCTYPE, doc.name, "deadline_crossed", 1, update_modified=False)
	notify(
		doc,
		[doc.developer, doc.assigned_by],
		f"crossed:{tag}",
		"Deadline crossed",
		f"{doc.title} - was due {_when(deadline)}, still {doc.status.lower()} ({doc.developer_name or doc.developer})",
	)
	today_morning = dt.datetime.combine(now.date(), morning)
	if now >= today_morning and deadline < today_morning:
		notify(
			doc,
			[doc.developer, doc.assigned_by],
			f"daily:{now.date().isoformat()}",
			"Development overdue",
			f"{doc.title} - {_late_text(deadline, now)} (due {_when(deadline)}, {doc.developer_name or doc.developer})",
		)


def _left_text(delta: dt.timedelta) -> str:
	minutes = int(delta.total_seconds() // 60)
	if minutes >= 1440:
		days = round(minutes / 1440)
		return f"{days} day{'s' if days > 1 else ''}"
	if minutes >= 60:
		return f"{minutes // 60} h"
	return f"{max(minutes, 1)} min"


# ---------------------------------------------------------------- for the app


def serialize(doc, me: str | None = None) -> dict:
	from hipersign_mobile.api.notes import html_to_text

	me = me or frappe.session.user
	now = timeutil.now_system()
	deadline = get_datetime(doc.deadline) if doc.deadline else None
	late = bool(deadline and doc.status in OPEN and now > deadline)
	return {
		"id": doc.name,
		"title": doc.title,
		"description": html_to_text(doc.description) if doc.get("description") else None,
		"developer": doc.developer,
		"developer_name": doc.developer_name or frappe.utils.get_fullname(doc.developer),
		"assigned_by": doc.assigned_by,
		"assigned_by_name": frappe.utils.get_fullname(doc.assigned_by) if doc.assigned_by else None,
		"status": doc.status,
		"priority": doc.priority,
		"deadline": timeutil.to_iso(doc.deadline),
		"late": late,
		"late_text": _late_text(deadline, now) if late else None,
		"reminders": reminder_offsets(doc),
		"own_reminders": bool((doc.get("reminders") or "").strip()),
		"customer": doc.customer,
		"project": doc.project_name,
		"completed_on": timeutil.to_iso(doc.completed_on),
		"deadline_changes": cint(doc.deadline_changes),
		"can_edit": can_edit(doc, me),
		"is_mine": doc.developer == me,
		"modified": timeutil.to_iso(doc.modified),
	}


def can_edit(doc, user: str) -> bool:
	"""Who may change title / developer / deadline: whoever assigned it, and managers."""
	from hipersign_mobile.services import permissions as perms

	return "assign_development" in perms.capabilities(user) or doc.assigned_by == user
