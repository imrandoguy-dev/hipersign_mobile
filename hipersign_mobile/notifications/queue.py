"""Notification queue: one Mobile Notification row per intended push.

- deduplication_key is unique, so the same event can never produce two pushes
- delivery happens in background jobs, never inside the business transaction
- transient failures retry with exponential backoff, bounded by settings
- invalid tokens deactivate the device instead of retrying forever
"""

from __future__ import annotations

import datetime as dt

import frappe
from frappe.utils import add_to_date, cint, get_datetime

from hipersign_mobile.notifications import fcm
from hipersign_mobile.services import settings as hm_settings
from hipersign_mobile.services import timeutil

PREF_FIELD = {
	"Assignment": "notify_assignments",
	"Rescheduled": "notify_assignments",
	"Reminder": "notify_reminders",
	"Trial Update": "notify_trials",
}


def get_preferences(user: str) -> frappe._dict:
	s = hm_settings.get()
	row = frappe.db.get_value(
		"Mobile User Preference",
		user,
		["reminder_minutes", "notify_assignments", "notify_reminders", "notify_trials"],
		as_dict=True,
	)
	if not row:
		return frappe._dict(
			reminder_minutes=s.default_reminder_minutes,
			notify_assignments=1,
			notify_reminders=1,
			notify_trials=1,
		)
	row.reminder_minutes = (
		cint(row.reminder_minutes) if row.reminder_minutes is not None else s.default_reminder_minutes
	)
	return row


def enqueue(
	recipient: str,
	event_type: str,
	title: str,
	body: str,
	dedup_key: str,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
	scheduled_for: dt.datetime | None = None,
) -> str | None:
	if not recipient or recipient in ("Guest", "Administrator"):
		return None
	pref_field = PREF_FIELD.get(event_type)
	if pref_field and not cint(get_preferences(recipient).get(pref_field)):
		return None

	dedup_key = dedup_key[:140]
	existing = frappe.db.get_value("Mobile Notification", {"deduplication_key": dedup_key}, "name")
	if existing:
		return existing

	doc = frappe.get_doc(
		{
			"doctype": "Mobile Notification",
			"recipient": recipient,
			"event_type": event_type,
			"title": title[:140],
			"body": body,
			"reference_doctype": reference_doctype,
			"reference_name": str(reference_name) if reference_name is not None else None,
			"scheduled_for": scheduled_for,
			"status": "Queued",
			"deduplication_key": dedup_key,
		}
	)
	try:
		# The unique deduplication_key settles races between concurrent requests.
		doc.insert(ignore_permissions=True)
	except (frappe.UniqueValidationError, frappe.DuplicateEntryError):
		return frappe.db.get_value("Mobile Notification", {"deduplication_key": dedup_key}, "name")

	if not scheduled_for or get_datetime(scheduled_for) <= timeutil.now_system():
		frappe.enqueue(
			"hipersign_mobile.notifications.queue.deliver",
			queue="short",
			notification=doc.name,
			enqueue_after_commit=True,
			now=bool(frappe.flags.in_test and frappe.flags.hm_deliver_now),
		)
	return doc.name


def cancel_for_reference(
	reference_doctype: str, reference_name, event_type: str | None = None, keep_key: str | None = None
):
	filters = {
		"reference_doctype": reference_doctype,
		"reference_name": str(reference_name),
		"status": "Queued",
	}
	if event_type:
		filters["event_type"] = event_type
	if keep_key:
		filters["deduplication_key"] = ["!=", keep_key]
	for name in frappe.get_all("Mobile Notification", filters=filters, pluck="name"):
		frappe.db.set_value("Mobile Notification", name, "status", "Cancelled", update_modified=False)


def _still_relevant(n) -> bool:
	if not frappe.db.get_value("User", n.recipient, "enabled"):
		return False
	if n.reference_doctype == "CRM Task" and n.event_type in ("Reminder", "Assignment", "Rescheduled"):
		task = frappe.db.get_value("CRM Task", n.reference_name, ["status", "assigned_to"], as_dict=True)
		if not task or task.assigned_to != n.recipient:
			return False
		if n.event_type == "Reminder" and task.status not in ("Backlog", "Todo", "In Progress"):
			return False
	return True


def _backoff_minutes(attempt: int) -> int:
	return min(2 ** max(attempt - 1, 0), 30)  # 1, 2, 4, 8, 16, 30 ...


def deliver(notification: str):
	"""Deliver one notification. Safe to call concurrently and repeatedly."""
	row = frappe.db.sql(
		"select name from `tabMobile Notification` where name=%s and status='Queued' for update",
		notification,
	)
	if not row:
		return
	n = frappe.get_doc("Mobile Notification", notification)
	s = hm_settings.get()
	now = timeutil.now_system()

	def finish(status, code=None, summary=None, message_id=None, next_at=None):
		frappe.db.set_value(
			"Mobile Notification",
			n.name,
			{
				"status": status,
				"attempt_count": cint(n.attempt_count) + 1,
				"last_attempt_at": now,
				"next_attempt_at": next_at,
				"last_error_code": code,
				"last_error_summary": summary,
				"provider_message_id": message_id,
			},
			update_modified=False,
		)
		frappe.db.commit()

	if not _still_relevant(n):
		frappe.db.set_value("Mobile Notification", n.name, "status", "Cancelled", update_modified=False)
		frappe.db.commit()
		return
	if not s.enable_push:
		return finish(
			"Failed", "PUSH_DISABLED", "Push notifications are disabled in Hipersign Mobile Settings."
		)

	devices = frappe.get_all(
		"Mobile Device",
		filters={"user": n.recipient, "is_active": 1},
		fields=["name"],
		order_by="last_seen_at desc",
		limit=10,
	)
	if not devices:
		return finish("Failed", "NO_DEVICE", "The user has no active device registered.")

	data = {
		"notification_id": n.name,
		"event_type": n.event_type,
		"reference_doctype": n.reference_doctype,
		"reference_name": n.reference_name,
		"route": route_for(n.reference_doctype, n.reference_name),
	}
	title, body = n.title or "Hipersign", n.body or ""
	if s.push_show_details:
		try:
			from hipersign_mobile.notifications.events import detailed_text

			detailed = detailed_text(n)
			if detailed:
				title, body = detailed
		except Exception:
			frappe.log_error(title="Hipersign push: could not build the detailed text")
	results = []
	for device in devices:
		device_doc = frappe.get_doc("Mobile Device", device.name)
		token = device_doc.get_password("push_token", raise_exception=False)
		if not token:
			continue
		result = fcm.send(token, title, body, data)
		if result.invalid_token:
			frappe.db.set_value("Mobile Device", device.name, "is_active", 0, update_modified=False)
		results.append(result)

	ok = [r for r in results if r.ok]
	if ok:
		return finish("Sent", message_id=ok[0].message_id)
	if not results:
		return finish("Failed", "NO_TOKEN", "No device has a push token.")
	last = results[-1]
	attempts = cint(n.attempt_count) + 1
	if any(r.transient for r in results) and attempts < s.max_push_attempts:
		return finish(
			"Queued",
			last.error_code,
			last.error_summary,
			next_at=add_to_date(now, minutes=_backoff_minutes(attempts)),
		)
	return finish("Failed", last.error_code, last.error_summary)


def route_for(doctype, name) -> str | None:
	if doctype == "CRM Task" and name:
		return f"/activity/{name}"
	if doctype == "Trial Booking" and name:
		return f"/trial/{name}"
	return None


def process_due_notifications():
	"""Scheduler (every minute): send due reminders and retry transient failures."""
	now = timeutil.now_system()
	names = frappe.db.sql(
		"""select name from `tabMobile Notification`
		where status='Queued'
		and (scheduled_for is null or scheduled_for <= %(now)s)
		and (next_attempt_at is null or next_attempt_at <= %(now)s)
		and creation <= %(settle)s
		order by coalesce(scheduled_for, creation) asc
		limit 300""",
		{"now": now, "settle": now - dt.timedelta(seconds=20)},
		pluck=True,
	)
	for name in names:
		try:
			deliver(name)
		except Exception:
			frappe.db.rollback()
			frappe.log_error(
				title="Hipersign Mobile: notification delivery failed",
				reference_doctype="Mobile Notification",
				reference_name=name,
			)


def purge_old_notifications():
	days = hm_settings.get().notification_retention_days
	frappe.db.delete(
		"Mobile Notification",
		{"creation": ["<", add_to_date(timeutil.now_system(), days=-days)], "status": ["!=", "Queued"]},
	)
