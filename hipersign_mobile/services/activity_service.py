"""CRM Task <-> mobile "activity" adapter and the activity lifecycle.

Decision (approved after the nexus365 audit): follow-ups and meetings live in the
existing Frappe CRM `CRM Task`, extended with `hm_*` custom fields. There is no
second activity record, so the CRM UI, existing WhatsApp reminders and the app
always show the same data.

Status mapping (CRM Task.status is left untouched so the CRM UI keeps working):
    Done                                   -> Completed
    Canceled                               -> Cancelled
    Backlog / Todo / In Progress, rescheduled at least once -> Rescheduled
    Backlog / Todo / In Progress, otherwise -> Pending
Display state (drives the calendar colours):
    completed | cancelled | rescheduled
    active   -> open and scheduled from today onwards (light blue)
    pending  -> open and overdue, or with no date yet (yellow-orange)
"""

from __future__ import annotations

import datetime as dt

import frappe
from frappe.utils import cint, get_datetime

from hipersign_mobile.services import permissions as perms
from hipersign_mobile.services import settings as hm_settings
from hipersign_mobile.services import timeutil
from hipersign_mobile.services.party import party_summary

OPEN_STATUSES = ("Backlog", "Todo", "In Progress")
ACTIVITY_TYPES = ("Follow-up", "Meeting", "Trial")
MOBILE_STATUSES = ("Pending", "In Progress", "Completed", "Rescheduled", "Cancelled")

BASE_FIELDS = [
	"name",
	"title",
	"status",
	"priority",
	"assigned_to",
	"due_date",
	"start_date",
	"description",
	"reference_doctype",
	"reference_docname",
	"modified",
	"hm_activity_type",
	"hm_end_datetime",
	"hm_meeting_link",
	"hm_reschedule_count",
	"hm_completed_at",
	"hm_completed_by",
	"hm_outcome",
	"hm_completion_notes",
	"hm_trial_booking",
]
OPTIONAL_FIELDS = [
	"custom_location",
	"custom_visit_type",
	"hm_reminder_offsets",
]  # present on some sites (e.g. nexus365)


def task_fields() -> list[str]:
	meta = frappe.get_meta("CRM Task")
	return [f for f in BASE_FIELDS if f in ("name", "modified") or meta.has_field(f)] + [
		f for f in OPTIONAL_FIELDS if meta.has_field(f)
	]


def mobile_status(row) -> str:
	status = row.get("status")
	if status == "Done":
		return "Completed"
	if status == "Canceled":
		return "Cancelled"
	if cint(row.get("hm_reschedule_count")) > 0:
		return "Rescheduled"
	if status == "In Progress":
		return "In Progress"
	return "Pending"


def end_of(row) -> dt.datetime | None:
	start = get_datetime(row.get("due_date")) if row.get("due_date") else None
	if not start:
		return None
	end = get_datetime(row.get("hm_end_datetime")) if row.get("hm_end_datetime") else None
	if not end or end <= start:
		end = start + dt.timedelta(minutes=hm_settings.get().default_activity_minutes)
	return end


def display_state(row, today_start: dt.datetime) -> str:
	status = mobile_status(row)
	if status == "Completed":
		return "completed"
	if status == "Cancelled":
		return "cancelled"
	if status == "Rescheduled":
		return "rescheduled"
	start = get_datetime(row.get("due_date")) if row.get("due_date") else None
	if start and start >= today_start:
		return "active"
	return "pending"


def serialize(
	row, today_start: dt.datetime | None = None, with_party: bool = True, cc: str | None = None
) -> dict:
	today_start = today_start or timeutil.user_today_start()
	start = row.get("due_date")
	end = end_of(row)
	is_open = row.get("status") in OPEN_STATUSES
	out = {
		"id": str(row.get("name")),
		"title": row.get("title"),
		"activity_type": row.get("hm_activity_type") or "Follow-up",
		"status": mobile_status(row),
		"crm_status": row.get("status"),
		"display_state": display_state(row, today_start),
		"priority": row.get("priority") or "Medium",
		"assigned_to": row.get("assigned_to"),
		"start": timeutil.to_iso(start),
		"end": timeutil.to_iso(end),
		"is_scheduled": bool(start),
		"is_overdue": bool(is_open and start and get_datetime(start) < timeutil.now_system()),
		"description": row.get("description"),
		"location": row.get("custom_location"),
		"visit_type": row.get("custom_visit_type"),
		"meeting_link": row.get("hm_meeting_link"),
		"reschedule_count": cint(row.get("hm_reschedule_count")),
		"outcome": row.get("hm_outcome"),
		"completion_notes": row.get("hm_completion_notes"),
		"completed_at": timeutil.to_iso(row.get("hm_completed_at")),
		"completed_by": row.get("hm_completed_by"),
		"trial_booking": row.get("hm_trial_booking"),
		# The activity's own reminders (minutes before); null = the person's reminder setting.
		"reminder_offsets": _offsets(row.get("hm_reminder_offsets")),
		"reference": {"doctype": row.get("reference_doctype"), "name": row.get("reference_docname")}
		if row.get("reference_doctype") and row.get("reference_docname")
		else None,
		"modified": timeutil.to_iso(row.get("modified")),
	}
	if with_party:
		out["party"] = party_summary(row.get("reference_doctype"), row.get("reference_docname"), cc=cc)
	return out


def _offsets(value):
	from hipersign_mobile.notifications.queue import parse_offsets

	try:
		return parse_offsets(value)
	except frappe.ValidationError:
		return None


# ---------------------------------------------------------------- queries


def _scope_filters(user: str, caps: set[str], for_user: str | None = None) -> dict:
	"""Activities are private to the assignee unless the caller has team_view."""
	if for_user and for_user != user:
		if "team_view" not in caps:
			raise frappe.PermissionError("You can only see your own activities.")
		return {"assigned_to": for_user}
	return {"assigned_to": user}


def list_activities(
	user: str,
	*,
	activity_types=None,
	status_bucket: str | None = None,
	start: dt.datetime | None = None,
	end: dt.datetime | None = None,
	offset: int = 0,
	limit: int = 20,
	for_user: str | None = None,
	order_by: str = "due_date asc, name asc",
):
	caps = perms.capabilities(user)
	filters = _scope_filters(user, caps, for_user)
	if activity_types:
		types = list(activity_types)
		if "Follow-up" in types:
			# Tasks created before the app existed have no type yet: treat as follow-ups.
			filters_or = [["hm_activity_type", "in", types], ["hm_activity_type", "is", "not set"]]
		else:
			filters_or = [["hm_activity_type", "in", types]]
	else:
		filters_or = None

	if status_bucket == "Completed":
		filters["status"] = "Done"
	elif status_bucket == "Cancelled":
		filters["status"] = "Canceled"
	elif status_bucket == "Rescheduled":
		filters["status"] = ["in", OPEN_STATUSES]
		filters["hm_reschedule_count"] = [">", 0]
	elif status_bucket == "Pending":
		filters["status"] = ["in", OPEN_STATUSES]
		filters["hm_reschedule_count"] = 0
	elif status_bucket == "Open":
		filters["status"] = ["in", OPEN_STATUSES]

	conditions = []
	if start is not None:
		conditions.append(["due_date", ">=", start])
	if end is not None:
		conditions.append(["due_date", "<", end])

	rows = frappe.get_all(
		"CRM Task",
		filters=[[k, *(v if isinstance(v, list) else ["=", v])] for k, v in filters.items()] + conditions,
		or_filters=filters_or,
		fields=task_fields(),
		order_by=order_by,
		start=offset,
		page_length=limit + 1,
	)
	return rows


# ---------------------------------------------------------------- mutations


def create_activity(user: str, payload: dict) -> tuple[frappe.model.document.Document, bool]:
	"""Returns (task, created). Idempotent on payload['idempotency_key']."""
	caps = perms.capabilities(user)
	perms.require("create_activity", user)

	key = (payload.get("idempotency_key") or "").strip()[:140] or None
	if key:
		existing = frappe.db.get_value(
			"CRM Task", {"hm_idempotency_key": key}, ["name", "owner"], as_dict=True
		)
		if existing:
			if existing.owner != user:
				raise frappe.ValidationError("This request key was already used.")
			return frappe.get_doc("CRM Task", existing.name), False

	assignee = payload.get("assigned_to") or user
	if assignee != user:
		if "assign_others" not in caps:
			raise frappe.PermissionError("You can only create activities for yourself.")
		if not frappe.db.get_value("User", assignee, "enabled"):
			raise frappe.ValidationError("The selected assignee is not an active user.")

	ref_doctype = payload.get("reference_doctype")
	ref_name = payload.get("reference_name")
	if ref_doctype not in perms.PARTY_DOCTYPES or not ref_name:
		raise frappe.ValidationError("Link the activity to a Customer, CRM Lead or CRM Deal.")
	perms.require_party(ref_doctype, ref_name, user)

	start = timeutil.parse_client_datetime(payload.get("start"), user)
	end = timeutil.parse_client_datetime(payload["end"], user) if payload.get("end") else None
	if end and end <= start:
		raise frappe.ValidationError("End time must be after the start time.")

	task = frappe.new_doc("CRM Task")
	task.update(
		{
			"title": payload["title"],
			"hm_activity_type": payload.get("activity_type") or "Follow-up",
			"priority": payload.get("priority") or "Medium",
			"status": "Todo",
			"assigned_to": assignee,
			"due_date": start,
			"start_date": start.date(),
			"hm_end_datetime": end,
			"description": payload.get("description"),
			"hm_meeting_link": payload.get("meeting_link"),
			"reference_doctype": ref_doctype,
			"reference_docname": ref_name,
			"hm_idempotency_key": key,
		}
	)
	if payload.get("location") and frappe.get_meta("CRM Task").has_field("custom_location"):
		task.custom_location = payload["location"]
	if payload.get("reminder_offsets") is not None and frappe.get_meta("CRM Task").has_field(
		"hm_reminder_offsets"
	):
		from hipersign_mobile.notifications.queue import offsets_text, parse_offsets

		task.hm_reminder_offsets = offsets_text(parse_offsets(payload["reminder_offsets"]))
	task.flags.hm_reason = payload.get("reason")
	task.insert(ignore_permissions=True)
	return task, True


def set_status(user: str, task_id, status: str, outcome=None, notes=None):
	task = perms.get_task_for(task_id, user, for_update=True)
	target = {
		"Completed": "Done",
		"Cancelled": "Canceled",
		"In Progress": "In Progress",
		"Pending": "Todo",
	}[status]

	if task.status == target:
		# Repeat of an already-applied request (e.g. retry after a timeout): no new history.
		return task, False
	if task.status in ("Done", "Canceled") and target in OPEN_STATUSES:
		if "team_view" not in perms.capabilities(user):
			raise frappe.ValidationError("This activity is already closed.")
	if task.status == "Canceled" and target == "Done":
		raise frappe.ValidationError("A cancelled activity cannot be completed.")

	task.status = target
	if target == "Done":
		if outcome is not None:
			task.hm_outcome = outcome
		if notes is not None:
			task.hm_completion_notes = notes
	task.flags.hm_reason = notes
	task.save(ignore_permissions=True)
	return task, True


def reschedule(user: str, task_id, new_start, new_end=None, reason=None):
	task = perms.get_task_for(task_id, user, for_update=True)
	if task.status not in OPEN_STATUSES:
		raise frappe.ValidationError("Only open activities can be rescheduled.")
	start = timeutil.parse_client_datetime(new_start, user)
	end = timeutil.parse_client_datetime(new_end, user) if new_end else None
	if end and end <= start:
		raise frappe.ValidationError("End time must be after the start time.")
	old_end = end_of(task)
	if task.due_date and get_datetime(task.due_date) == start and (end is None or old_end == end):
		return task, False  # idempotent retry

	if task.hm_trial_booking:
		raise frappe.ValidationError(
			"This activity belongs to a trial booking. Reschedule the booking instead."
		)

	if end is None and task.due_date and task.hm_end_datetime:
		# Keep the original duration when only a new start is given.
		end = start + (get_datetime(task.hm_end_datetime) - get_datetime(task.due_date))
	task.due_date = start
	task.start_date = start.date()
	task.hm_end_datetime = end
	task.flags.hm_reason = reason
	task.save(ignore_permissions=True)
	return task, True


def update_details(user: str, task_id, values: dict):
	"""Edit an open activity from the app: title, type, priority, notes, place, link, customer
	and time. A time change is recorded in the history like a reschedule."""
	task = perms.get_task_for(task_id, user, for_update=True)
	if task.status not in OPEN_STATUSES:
		raise frappe.ValidationError("Only open activities can be edited.")
	changed = False

	def put(field, value):
		nonlocal changed
		if (task.get(field) or None) != (value or None):
			task.set(field, value)
			changed = True

	if "title" in values:
		title = (values.get("title") or "").strip()
		if not title:
			raise frappe.ValidationError("Title is required.")
		put("title", title[:140])
	if "activity_type" in values:
		kind = values.get("activity_type")
		if task.get("hm_trial_booking") or task.get("hm_activity_type") == "Trial":
			if kind and kind != "Trial":
				raise frappe.ValidationError("A trial stays a trial. Change it from the trial booking.")
		elif kind not in ("Follow-up", "Meeting"):
			raise frappe.ValidationError("Type must be Follow-up or Meeting.")
		else:
			put("hm_activity_type", kind)
	if "priority" in values:
		if values.get("priority") not in ("Low", "Medium", "High"):
			raise frappe.ValidationError("Priority must be Low, Medium or High.")
		put("priority", values["priority"])
	if "description" in values:
		put("description", (values.get("description") or "").strip()[:5000] or None)
	if "meeting_link" in values:
		put("hm_meeting_link", (values.get("meeting_link") or "").strip()[:500] or None)
	if "reminder_offsets" in values and frappe.get_meta("CRM Task").has_field("hm_reminder_offsets"):
		from hipersign_mobile.notifications.queue import offsets_text, parse_offsets

		# "" / null = back to the person's own reminder setting
		put("hm_reminder_offsets", offsets_text(parse_offsets(values.get("reminder_offsets"))))
	if "location" in values and frappe.get_meta("CRM Task").has_field("custom_location"):
		put("custom_location", (values.get("location") or "").strip()[:140] or None)
	if values.get("reference_doctype") and values.get("reference_name"):
		ref_dt, ref_name = values["reference_doctype"], values["reference_name"]
		if (ref_dt, ref_name) != (task.reference_doctype, task.reference_docname):
			if ref_dt not in perms.PARTY_DOCTYPES:
				raise frappe.ValidationError("Link the activity to a Customer, CRM Lead or CRM Deal.")
			if task.get("hm_trial_booking"):
				raise frappe.ValidationError("The customer of a trial is changed from the trial booking.")
			perms.require_party(ref_dt, ref_name, user)
			put("reference_doctype", ref_dt)
			put("reference_docname", ref_name)

	if values.get("start"):
		start = timeutil.parse_client_datetime(values["start"], user)
		end = timeutil.parse_client_datetime(values["end"], user) if values.get("end") else None
		if end and end <= start:
			raise frappe.ValidationError("End time must be after the start time.")
		same_start = task.due_date and get_datetime(task.due_date) == start
		same_end = end is None or end_of(task) == end
		if not (same_start and same_end):
			if task.get("hm_trial_booking"):
				raise frappe.ValidationError(
					"This activity belongs to a trial booking. Reschedule the booking instead."
				)
			if end is None and task.due_date and task.hm_end_datetime:
				end = start + (get_datetime(task.hm_end_datetime) - get_datetime(task.due_date))
			task.due_date = start
			task.start_date = start.date()
			task.hm_end_datetime = end
			changed = True

	if changed:
		task.flags.hm_reason = (values.get("reason") or "").strip()[:2000] or None
		task.save(ignore_permissions=True)
	return task, changed


def add_history(task_name, event_type: str, actor: str | None = None, **values):
	doc = frappe.new_doc("Sales Activity History")
	doc.update(
		{
			"crm_task": task_name,
			"event_type": event_type,
			"actor": actor or frappe.session.user,
			"event_datetime": timeutil.now_system(),
			"source": values.pop("source", None) or frappe.flags.hm_source or "Desk",
			"request_id": values.pop("request_id", None) or frappe.flags.hm_request_id,
		}
	)
	doc.update(values)
	doc.insert(ignore_permissions=True)
	return doc


def history_for(task_name) -> list[dict]:
	rows = frappe.get_all(
		"Sales Activity History",
		filters={"crm_task": task_name},
		fields=[
			"event_type",
			"actor",
			"event_datetime",
			"source",
			"old_status",
			"new_status",
			"old_start_datetime",
			"new_start_datetime",
			"old_end_datetime",
			"new_end_datetime",
			"old_assigned_to",
			"new_assigned_to",
			"reason",
		],
		order_by="event_datetime desc, creation desc",
		limit=100,
	)
	for r in rows:
		for k in (
			"event_datetime",
			"old_start_datetime",
			"new_start_datetime",
			"old_end_datetime",
			"new_end_datetime",
		):
			r[k] = timeutil.to_iso(r.get(k))
		r["actor_name"] = frappe.utils.get_fullname(r["actor"]) if r.get("actor") else None
	return rows
