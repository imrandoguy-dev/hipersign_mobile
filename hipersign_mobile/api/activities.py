from __future__ import annotations

import frappe

from hipersign_mobile.api._common import choice, mobile_api, optional_str, page_args, page_result, require_str
from hipersign_mobile.services import activity_service as svc
from hipersign_mobile.services import booking_service, timeutil
from hipersign_mobile.services import permissions as perms
from hipersign_mobile.services import settings as hm_settings

LIST_BUCKETS = ("Pending", "Completed", "Rescheduled", "Cancelled", "Open")


def _list(types, status, start_date, end_date, cursor, page_size, cap):
	me = frappe.session.user
	perms.require(cap, me)
	bucket = choice(status, LIST_BUCKETS, "status", default="Pending")
	start = end = None
	if start_date:
		start = timeutil.user_day_bounds(
			timeutil.parse_client_date(start_date), timeutil.parse_client_date(start_date), me
		)[0]
	if end_date:
		end = timeutil.user_day_bounds(
			timeutil.parse_client_date(end_date), timeutil.parse_client_date(end_date), me
		)[1]
	offset, limit = page_args(cursor, page_size)
	order = "due_date desc, name desc" if bucket in ("Completed", "Cancelled") else "due_date asc, name asc"
	rows = svc.list_activities(
		me,
		activity_types=types,
		status_bucket=bucket,
		start=start,
		end=end,
		offset=offset,
		limit=limit,
		order_by=order,
	)
	today_start = timeutil.user_today_start(me)
	cc = hm_settings.get().default_country_code
	items = [svc.serialize(r, today_start, cc=cc) for r in rows[:limit]]
	return page_result(items, offset, limit, len(rows) > limit)


@mobile_api(methods=("GET",))
def get_followups(
	status: str | None = None,
	start_date: str | None = None,
	end_date: str | None = None,
	cursor: str | int | None = None,
	page_size: str | int | None = None,
):
	return _list(["Follow-up"], status, start_date, end_date, cursor, page_size, "followups")


@mobile_api(methods=("GET",))
def get_meetings(
	status: str | None = None,
	start_date: str | None = None,
	end_date: str | None = None,
	cursor: str | int | None = None,
	page_size: str | int | None = None,
):
	return _list(["Meeting", "Trial"], status, start_date, end_date, cursor, page_size, "meetings")


@mobile_api(methods=("GET",))
def get_detail(activity_id: str | int):
	me = frappe.session.user
	task = perms.get_task_for(activity_id, me)
	row = frappe.db.get_value("CRM Task", task.name, svc.task_fields(), as_dict=True)
	data = svc.serialize(row)
	data["history"] = svc.history_for(task.name)
	if row.get("hm_trial_booking"):
		data["booking"] = booking_service.serialize(frappe.get_doc("Trial Booking", row.hm_trial_booking))
	return data


@mobile_api(methods=("POST",))
def create_activity(
	title: str | None = None,
	activity_type: str | None = None,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
	start: str | None = None,
	end: str | None = None,
	priority: str | None = None,
	description: str | None = None,
	location: str | None = None,
	meeting_link: str | None = None,
	assigned_to: str | None = None,
	idempotency_key: str | None = None,
	# Convenience aliases matching the spec's request example
	lead: str | None = None,
	customer: str | None = None,
	deal: str | None = None,
	start_datetime: str | None = None,
	end_datetime: str | None = None,
):
	me = frappe.session.user
	if not reference_doctype:
		reference_doctype, reference_name = (
			("Customer", customer)
			if customer
			else ("CRM Deal", deal)
			if deal
			else ("CRM Lead", lead)
			if lead
			else (None, None)
		)
	payload = {
		"title": require_str(title, "Title"),
		"activity_type": choice(
			activity_type, ("Follow-up", "Meeting"), "activity_type", default="Follow-up"
		),
		"reference_doctype": reference_doctype,
		"reference_name": reference_name,
		"start": start or start_datetime,
		"end": end or end_datetime,
		"priority": choice(priority, ("Low", "Medium", "High"), "priority", default="Medium"),
		"description": optional_str(description, 5000),
		"location": optional_str(location, 140),
		"meeting_link": optional_str(meeting_link, 500),
		"assigned_to": assigned_to,
		"idempotency_key": idempotency_key,
	}
	if not payload["start"]:
		raise frappe.ValidationError("Start time is required.")
	task, created = svc.create_activity(me, payload)
	row = frappe.db.get_value("CRM Task", task.name, svc.task_fields(), as_dict=True)
	return {"created": created, "activity": svc.serialize(row)}


@mobile_api(methods=("POST",))
def update_status(activity_id: str | int, status: str, outcome: str | None = None, notes: str | None = None):
	me = frappe.session.user
	status = choice(status, ("Completed", "Cancelled", "In Progress", "Pending"), "status")
	outcome = optional_str(outcome, 140)
	notes = optional_str(notes, 2000)
	task = perms.get_task_for(activity_id, me)

	if task.get("hm_trial_booking") and status in ("Completed", "Cancelled"):
		# Keep the booking and its calendar activity as one appointment.
		if status == "Completed":
			_booking, changed = booking_service.complete(me, task.hm_trial_booking, notes)
		else:
			_booking, changed = booking_service.cancel(me, task.hm_trial_booking, notes)
		task.reload()
		if outcome and changed:
			task.db_set("hm_outcome", outcome)
	else:
		task, changed = svc.set_status(me, activity_id, status, outcome, notes)

	row = frappe.db.get_value("CRM Task", task.name, svc.task_fields(), as_dict=True)
	return {"changed": changed, "activity": svc.serialize(row)}


@mobile_api(methods=("POST",))
def update_activity(
	activity_id: str | int,
	title: str | None = None,
	activity_type: str | None = None,
	priority: str | None = None,
	description: str | None = None,
	location: str | None = None,
	meeting_link: str | None = None,
	start: str | None = None,
	end: str | None = None,
	reference_doctype: str | None = None,
	reference_name: str | None = None,
):
	"""Edit an open follow-up / meeting. Only the fields sent are changed."""
	me = frappe.session.user
	sent = {
		k: v
		for k, v in {
			"title": title,
			"activity_type": activity_type,
			"priority": priority,
			"description": description,
			"location": location,
			"meeting_link": meeting_link,
			"start": start,
			"end": end,
			"reference_doctype": reference_doctype,
			"reference_name": reference_name,
		}.items()
		if k in frappe.form_dict or v is not None
	}
	task, changed = svc.update_details(me, activity_id, sent)
	row = frappe.db.get_value("CRM Task", task.name, svc.task_fields(), as_dict=True)
	data = svc.serialize(row)
	data["history"] = svc.history_for(task.name)[:5]
	return {"changed": changed, "activity": data}


@mobile_api(methods=("POST",))
def delete_activity(activity_id: str | int, reason: str | None = None):
	"""Delete an unwanted follow-up / meeting. Your own (assigned to you or created by you);
	managers can delete their team's. Trials are cancelled from the trial booking instead."""
	me = frappe.session.user
	task = perms.get_task_for(activity_id, me)
	caps = perms.capabilities(me)
	if task.get("hm_trial_booking"):
		raise frappe.ValidationError("This is a trial booking. Cancel the trial instead.")
	if me not in (task.assigned_to, task.owner) and "team_view" not in caps:
		raise frappe.PermissionError("You can only delete your own activities.")
	when = frappe.utils.format_datetime(task.due_date, "d MMM yyyy, h:mm a") if task.due_date else "no date"
	kind = (task.get("hm_activity_type") or "Follow-up").lower()
	ref_dt, ref_name, title, name = task.reference_doctype, task.reference_docname, task.title, task.name
	frappe.db.delete("Sales Activity History", {"crm_task": name})
	frappe.delete_doc("CRM Task", name, ignore_permissions=True, force=True)
	# Leave a trace on the customer / lead / deal timeline.
	if ref_dt and ref_name and frappe.db.exists(ref_dt, ref_name):
		why = optional_str(reason, 500)
		frappe.get_doc(
			{
				"doctype": "Comment",
				"comment_type": "Info",
				"reference_doctype": ref_dt,
				"reference_name": ref_name,
				"content": frappe.utils.escape_html(
					f'Deleted {kind} "{title}" ({when}) from Hipersign' + (f": {why}" if why else "")
				),
			}
		).insert(ignore_permissions=True)
	return {"deleted": str(name)}


@mobile_api(methods=("POST",))
def reschedule_activity(
	activity_id: str | int,
	new_start_datetime: str | None = None,
	new_end_datetime: str | None = None,
	reason: str | None = None,
	new_start: str | None = None,
	new_end: str | None = None,
):
	me = frappe.session.user
	start = new_start_datetime or new_start
	if not start:
		raise frappe.ValidationError("New start time is required.")
	task, changed = svc.reschedule(
		me, activity_id, start, new_end_datetime or new_end, optional_str(reason, 2000)
	)
	row = frappe.db.get_value("CRM Task", task.name, svc.task_fields(), as_dict=True)
	data = svc.serialize(row)
	data["history"] = svc.history_for(task.name)[:5]
	return {"changed": changed, "activity": data}


@mobile_api(methods=("POST",))
def add_note(activity_id: str | int, note: str):
	me = frappe.session.user
	task = perms.get_task_for(activity_id, me)
	note = require_str(note, "Note", 2000)
	svc.add_history(task.name, "Note Added", reason=note)
	# Mirror to Frappe CRM so the note shows on the Lead / Deal timeline too.
	if task.reference_doctype in ("CRM Lead", "CRM Deal") and frappe.db.exists(
		task.reference_doctype, task.reference_docname
	):
		frappe.get_doc(
			{
				"doctype": "FCRM Note",
				"title": (task.title or "Note")[:140],
				"content": frappe.utils.escape_html(note),
				"reference_doctype": task.reference_doctype,
				"reference_docname": task.reference_docname,
			}
		).insert(ignore_permissions=True)
	return {"history": svc.history_for(task.name)[:5]}


@mobile_api(methods=("POST",))
def log_contact(activity_id: str | int, channel: str):
	"""Optional: record that the user tapped Call / WhatsApp. Never completes the activity."""
	me = frappe.session.user
	task = perms.get_task_for(activity_id, me)
	channel = choice(channel, ("Call", "WhatsApp"), "channel")
	svc.add_history(task.name, "Contact Attempt", reason=channel)
	return {"logged": True}
