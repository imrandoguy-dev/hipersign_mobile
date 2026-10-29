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
	"hm_meeting_group",
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
	if row.get("hm_meeting_group"):
		out["meeting_group"] = row.get("hm_meeting_group")
		out["people"] = group_people(row.get("hm_meeting_group"))
	else:
		out["people"] = [{"id": row.get("assigned_to"), "name": _full_name(row.get("assigned_to")), "status": out["status"]}] if row.get("assigned_to") else []
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

	base = [[k, *(v if isinstance(v, list) else ["=", v])] for k, v in filters.items() if k != "assigned_to"]
	person = filters["assigned_to"]
	rows = frappe.get_all(
		"CRM Task",
		filters=base + [["assigned_to", "=", person]] + conditions,
		or_filters=filters_or,
		fields=task_fields(),
		order_by=order_by,
		page_length=offset + limit + 1,
	)
	# Trials / trainings the person gives as trainer (booked by someone else) are on her
	# calendar too - same activity record, so both see the same status.
	trainer_tasks = trainer_task_names(person, start, end)
	if trainer_tasks:
		seen = {r.name for r in rows}
		extra = frappe.get_all(
			"CRM Task",
			filters=base + [["name", "in", list(trainer_tasks - seen)]] + conditions,
			or_filters=filters_or,
			fields=task_fields(),
			order_by=order_by,
			page_length=offset + limit + 1,
		) if trainer_tasks - seen else []
		if extra:
			rows = _sorted(rows + extra, order_by)
	return rows[offset : offset + limit + 1]


def trainer_task_names(user: str, start=None, end=None) -> set:
	"""Calendar activities of trial bookings where `user` is the trainer."""
	filters = [["trainer", "=", user], ["crm_task", "is", "set"], ["booked_by", "!=", user]]
	if start is not None:
		filters.append(["end_datetime", ">=", start - dt.timedelta(days=1)])
	if end is not None:
		filters.append(["start_datetime", "<", end])
	names = set()
	for t in frappe.get_all("Trial Booking", filters=filters, pluck="crm_task", limit=1000):
		try:
			names.add(int(t))
		except (TypeError, ValueError):
			continue
	return names


def _sorted(rows, order_by: str):
	"""Re-sort merged rows the way the query ordered them (due_date asc/desc, then name)."""
	desc = "due_date desc" in (order_by or "")
	far = dt.datetime.max if not desc else dt.datetime.min
	rows.sort(
		key=lambda r: (get_datetime(r.due_date) if r.get("due_date") else far, int(r.name)),
		reverse=desc,
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

	kind = payload.get("activity_type") or "Follow-up"
	people = parse_people(payload.get("people"))
	if not people:
		people = [payload.get("assigned_to") or user]
	for person in people:
		_check_person(person, user, caps, kind)
	assignee = user if user in people else people[0]

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
	others = [p for p in people if p != assignee]
	if others:
		task.db_set("hm_meeting_group", str(task.name), update_modified=False)
		for person in others:
			_copy_for(task, person, key)
	return task, True


# ---------------------------------------------------------------- several people, one meeting
# Each person gets their own copy (so it is on their calendar with their own reminders);
# the copies share hm_meeting_group, and edits / reschedules / cancel are applied to all.

SHARED_FIELDS = (
	"title",
	"hm_activity_type",
	"priority",
	"description",
	"hm_meeting_link",
	"custom_location",
	"reference_doctype",
	"reference_docname",
	"due_date",
	"start_date",
	"hm_end_datetime",
)


def parse_people(value) -> list[str]:
	if not value:
		return []
	if isinstance(value, str):
		value = value.strip()
		if value.startswith("["):
			value = frappe.parse_json(value)
		else:
			value = value.split(",")
	out = []
	for v in value:
		v = (v.get("id") if isinstance(v, dict) else str(v or "")).strip()
		if v and v not in out:
			out.append(v)
	if len(out) > 50:
		raise frappe.ValidationError("A meeting can have up to 50 people.")
	return out


def _check_person(person: str, user: str, caps: set, kind: str):
	if person == user:
		return
	# Anyone can invite colleagues to a meeting; follow-ups for others need a manager.
	if kind != "Meeting" and "assign_others" not in caps:
		raise frappe.PermissionError("You can only create follow-ups for yourself.")
	if not frappe.db.get_value("User", person, "enabled"):
		raise frappe.ValidationError(f"{person} is not an active user.")


def _full_name(user):
	from frappe.utils import get_fullname

	return get_fullname(user) if user else None


def group_people(group) -> list[dict]:
	rows = frappe.get_all(
		"CRM Task",
		filters={"hm_meeting_group": group},
		fields=["name", "assigned_to", "status", "hm_reschedule_count"],
		order_by="creation asc",
	)
	return [
		{"id": r.assigned_to, "name": _full_name(r.assigned_to), "status": mobile_status(r)}
		for r in rows
		if r.assigned_to
	]


def siblings(task, open_only: bool = True) -> list:
	group = task.get("hm_meeting_group")
	if not group:
		return []
	filters = {"hm_meeting_group": group, "name": ["!=", task.name]}
	if open_only:
		filters["status"] = ["in", list(OPEN_STATUSES)]
	return [frappe.get_doc("CRM Task", n) for n in frappe.get_all("CRM Task", filters=filters, pluck="name")]


def _copy_for(task, person: str, key=None):
	if frappe.db.exists("CRM Task", {"hm_meeting_group": task.hm_meeting_group, "assigned_to": person}):
		return None
	doc = frappe.new_doc("CRM Task")
	for f in SHARED_FIELDS + ("hm_reminder_offsets",):
		if doc.meta.has_field(f):
			doc.set(f, task.get(f))
	doc.update(
		{
			"status": task.status if task.status in OPEN_STATUSES else "Todo",
			"assigned_to": person,
			"hm_meeting_group": task.hm_meeting_group,
			"hm_idempotency_key": f"{key}:{person}"[:140] if key else None,
		}
	)
	doc.insert(ignore_permissions=True)
	return doc


def _sync_siblings(task, reason=None):
	for other in siblings(task):
		changed = False
		for f in SHARED_FIELDS:
			if other.meta.has_field(f) and other.get(f) != task.get(f):
				other.set(f, task.get(f))
				changed = True
		if changed:
			other.flags.hm_reason = reason
			other.save(ignore_permissions=True)


def set_people(user: str, task, people: list[str], reason=None) -> bool:
	"""Add / remove people from a meeting. Removed people's copies are deleted."""
	if not people:
		raise frappe.ValidationError("Choose at least one person.")
	caps = perms.capabilities(user)
	kind = task.get("hm_activity_type") or "Follow-up"
	current = {d.assigned_to: d for d in [task, *siblings(task, open_only=False)] if d.assigned_to}
	added = [p for p in people if p not in current]
	removed = [p for p in current if p not in people]
	if not added and not removed:
		return False
	for p in added:
		_check_person(p, user, caps, kind)
	if removed and user not in (task.owner,) and "team_view" not in caps and set(removed) - {user}:
		raise frappe.PermissionError("Only the person who set up this meeting can remove others.")
	if not task.get("hm_meeting_group"):
		task.db_set("hm_meeting_group", str(task.name), update_modified=False)
	for p in added:
		_copy_for(task, p)
	for p in removed:
		doc = current[p]
		frappe.db.delete("Sales Activity History", {"crm_task": doc.name})
		frappe.delete_doc("CRM Task", doc.name, ignore_permissions=True, force=True)
	return True


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
	if target in ("Done", "Canceled"):
		# One meeting: done / cancelled for everyone in it.
		for other in siblings(task):
			other.status = target
			if target == "Done":
				other.hm_outcome = task.get("hm_outcome")
				other.hm_completion_notes = task.get("hm_completion_notes")
			other.flags.hm_reason = notes
			other.save(ignore_permissions=True)
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
	_sync_siblings(task, reason)
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

	reason = (values.get("reason") or "").strip()[:2000] or None
	if changed:
		task.flags.hm_reason = reason
		task.save(ignore_permissions=True)
		_sync_siblings(task, reason)
	if values.get("people") is not None and not task.get("hm_trial_booking"):
		if set_people(user, task, parse_people(values["people"]), reason):
			changed = True
			if frappe.db.exists("CRM Task", task.name):
				task.reload()
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
