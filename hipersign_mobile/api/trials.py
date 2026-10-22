from __future__ import annotations

import datetime as dt

import frappe
from frappe.utils import cint

from hipersign_mobile.api._common import (
	choice,
	limit_per_user,
	mobile_api,
	optional_str,
	page_args,
	page_result,
)
from hipersign_mobile.services import booking_service as svc
from hipersign_mobile.services import permissions as perms
from hipersign_mobile.services import timeutil

STATUSES = ("Active", "Pending Confirmation", "Booked", "Rescheduled", "Completed", "Cancelled", "All")


@mobile_api(methods=("GET",))
def list_bookings(
	status: str | None = None,
	start_date: str | None = None,
	end_date: str | None = None,
	cursor: str | int | None = None,
	page_size: str | int | None = None,
):
	me = frappe.session.user
	if not perms.capabilities(me) & {"trial_booking", "manage_trainer_slots"}:
		raise frappe.PermissionError("You are not allowed to do this.")  # trainers see the trials they give
	status = choice(status, STATUSES, "status", default="Active")
	offset, limit = page_args(cursor, page_size)
	filters = []
	mine = None
	if "team_view" not in perms.capabilities(me):
		mine = [["booked_by", "=", me], ["trainer", "=", me]]  # booked by me, or I'm the trainer
	if status == "Active":
		filters.append(["status", "in", svc.ACTIVE_BOOKING_STATUSES])
	elif status != "All":
		filters.append(["status", "=", status])
	if start_date:
		filters.append(
			[
				"start_datetime",
				">=",
				timeutil.user_day_bounds(
					timeutil.parse_client_date(start_date), timeutil.parse_client_date(start_date), me
				)[0],
			]
		)
	if end_date:
		filters.append(
			[
				"start_datetime",
				"<",
				timeutil.user_day_bounds(
					timeutil.parse_client_date(end_date), timeutil.parse_client_date(end_date), me
				)[1],
			]
		)
	order = (
		"start_datetime asc"
		if status in ("Active", "Booked", "Rescheduled", "Pending Confirmation")
		else "start_datetime desc"
	)
	# One card per customer: all their sessions (of any trial) are grouped; the card shows
	# the next session (or, when none is upcoming, the latest one).
	rows = frappe.get_all(
		"Trial Booking",
		filters=filters,
		or_filters=mine,
		fields=["name", "trial_series", "start_datetime", "customer", "lead"],
		order_by=order,
		limit=1000,
	)
	now = timeutil.now_system()
	groups: dict[str, list] = {}
	for r in rows:
		key = f"C:{r.customer}" if r.customer else (f"L:{r.lead}" if r.lead else (r.trial_series or r.name))
		groups.setdefault(key, []).append(r)
	reps = []
	for _series, sessions in groups.items():
		upcoming = sorted(
			(x for x in sessions if x.start_datetime and frappe.utils.get_datetime(x.start_datetime) >= now),
			key=lambda x: x.start_datetime,
		)
		rep = upcoming[0] if upcoming else max(sessions, key=lambda x: x.start_datetime or now)
		reps.append(rep)
	descending = order.endswith("desc")
	reps.sort(key=lambda x: x.start_datetime or now, reverse=descending)
	page = reps[offset : offset + limit + 1]
	items = [svc.serialize(frappe.get_doc("Trial Booking", r.name)) for r in page[:limit]]
	return page_result(items, offset, limit, len(page) > limit)


@mobile_api(methods=("GET",))
def check_time(trainer: str | None = None, start: str | None = None, end: str | None = None):
	"""An exact time chosen by hand (e.g. 10:15 - 11:30) for a trainer. Returns it as a
	bookable slot when the trainer is free then; the booking re-checks under lock."""
	from hipersign_mobile.services import trainer_hours

	me = frappe.session.user
	perms.require("trial_booking", me)
	if not trainer or trainer not in trainer_hours.trainers():
		raise frappe.ValidationError("Choose a trainer.")
	if not start or not end:
		raise frappe.ValidationError("Choose the start and end time.")
	s = timeutil.parse_client_datetime(start, me)
	e = timeutil.parse_client_datetime(end, me)
	return trainer_hours.exact_time(trainer, s, e)


@mobile_api(methods=("GET",))
def get_trainers():
	perms.require("trial_booking")
	users = frappe.get_all(
		"Has Role",
		filters={"role": perms.ROLE_TRAINER, "parenttype": "User"},
		pluck="parent",
	)
	enabled = frappe.get_all(
		"User",
		filters={"name": ["in", users or [""]], "enabled": 1},
		fields=["name", "full_name"],
		order_by="full_name asc",
	)
	return {"items": [{"id": u.name, "name": u.full_name or u.name} for u in enabled]}


@mobile_api(methods=("GET",))
def get_available_slots(
	start_datetime: str | None = None,
	end_datetime: str | None = None,
	trainer: str | None = None,
	booking_type: str | None = None,
	start_date: str | None = None,
	end_date: str | None = None,
):
	me = frappe.session.user
	perms.require("trial_booking", me)
	if start_datetime:
		start = timeutil.parse_client_datetime(start_datetime, me)
	else:
		d = (
			timeutil.parse_client_date(start_date)
			if start_date
			else dt.datetime.now(timeutil.user_tz(me)).date()
		)
		start = timeutil.user_day_bounds(d, d, me)[0]
	if end_datetime:
		end = timeutil.parse_client_datetime(end_datetime, me)
	else:
		d = (
			timeutil.parse_client_date(end_date)
			if end_date
			else (dt.datetime.now(timeutil.user_tz(me)).date() + dt.timedelta(days=14))
		)
		end = timeutil.user_day_bounds(d, d, me)[1]
	if end <= start:
		raise frappe.ValidationError("End must be after start.")
	if (end - start).days > 62:
		raise frappe.ValidationError("Search range is limited to 62 days.")
	if booking_type:
		booking_type = choice(booking_type, svc.BOOKING_TYPES, "booking_type")
	return {"items": svc.available_slots(start, end, trainer=trainer, booking_type=booking_type)}


@mobile_api(methods=("GET",))
def get_booking_detail(booking_id: str):
	b = svc._get_booking_for(frappe.session.user, booking_id)
	return svc.serialize(b)


@mobile_api(methods=("POST",))
def book_trial(
	trainer_slot: str,
	customer: str | None = None,
	lead: str | None = None,
	booking_type: str | None = None,
	notes: str | None = None,
	idempotency_key: str | None = None,
	trial_days: str | int | None = None,
):
	me = frappe.session.user
	limit_per_user("book_trial", 30, 60)
	booking_type = choice(booking_type, svc.BOOKING_TYPES, "booking_type", default="ERP Trial")
	skipped = []
	try:
		booking, created, skipped = svc.book_series(
			me,
			trial_days=cint(trial_days) or 1,
			customer=customer,
			lead=lead,
			trainer_slot=trainer_slot,
			booking_type=booking_type,
			notes=optional_str(notes, 2000),
			idempotency_key=idempotency_key,
		)
	except svc.ConflictError as exc:
		# Re-raise with alternatives so the app can offer another slot immediately.
		frappe.db.rollback()
		alternatives = _alternatives(trainer_slot, booking_type)
		raise svc.ConflictError(f"{exc} {_alt_hint(alternatives)}".strip())
	# skipped: days of the trial with no session (trainer busy) - book them with add_session.
	return {"created": created, "booking": svc.serialize(booking), "skipped": skipped}


@mobile_api(methods=("POST",))
def add_trial_session(booking_id: str, trainer_slot: str, idempotency_key: str | None = None):
	"""Add a session to an existing trial (a day the trainer was busy, or an extra day)."""
	me = frappe.session.user
	perms.require("trial_booking", me)
	booking, created = svc.add_session(me, booking_id, trainer_slot, idempotency_key)
	return {"created": created, "booking": svc.serialize(booking)}


@mobile_api(methods=("POST",))
def set_trial_days(booking_id: str, days: str | int):
	"""Change how many days the trial runs (for all its sessions)."""
	me = frappe.session.user
	perms.require("trial_booking", me)
	return {"booking": svc.serialize(svc.set_trial_days(me, booking_id, days))}


def _alternatives(slot_name, booking_type):
	if str(slot_name or "").startswith("auto:"):
		from hipersign_mobile.services import trainer_hours

		try:
			start = trainer_hours.parse_id(slot_name)[1]
		except frappe.ValidationError:
			return []
	else:
		start = frappe.db.get_value("Trainer Slot", slot_name, "start_datetime")
	if not start:
		return []
	start = frappe.utils.get_datetime(start)
	return svc.available_slots(
		start - dt.timedelta(hours=2), start + dt.timedelta(days=3), booking_type=booking_type, limit=5
	)


def _alt_hint(alts):
	return f"{len(alts)} other slot(s) are free nearby." if alts else ""


@mobile_api(methods=("POST",))
def reschedule_booking(booking_id: str, new_slot_id: str, reason: str | None = None):
	booking, changed = svc.reschedule(
		frappe.session.user, booking_id, new_slot_id, optional_str(reason, 2000)
	)
	return {"changed": changed, "booking": svc.serialize(booking)}


@mobile_api(methods=("POST",))
def cancel_booking(booking_id: str, reason: str | None = None):
	booking, changed = svc.cancel(frappe.session.user, booking_id, optional_str(reason, 2000))
	return {"changed": changed, "booking": svc.serialize(booking)}


@mobile_api(methods=("POST",))
def complete_booking(booking_id: str, completion_notes: str | None = None):
	booking, changed = svc.complete(frappe.session.user, booking_id, optional_str(completion_notes, 2000))
	return {"changed": changed, "booking": svc.serialize(booking)}
