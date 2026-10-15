"""Trial bookings against trainer availability.

Concurrency: every booking change for a trainer first takes a row lock on that
trainer's User row (SELECT ... FOR UPDATE). Two salespeople booking the same
trainer at the same moment are therefore serialized; the second one re-checks
overlaps after the first commits and gets CONFLICT. A slot-level unique key
would not be enough, because different slots of the same trainer can overlap.
"""

from __future__ import annotations

import datetime as dt

import frappe
from frappe.utils import cint, get_datetime, getdate

from hipersign_mobile.services import permissions as perms
from hipersign_mobile.services import timeutil, trainer_hours
from hipersign_mobile.services.errors import ConflictError
from hipersign_mobile.services.party import party_summary

ACTIVE_BOOKING_STATUSES = ("Pending Confirmation", "Booked", "Rescheduled")
BOOKING_TYPES = ("ERP Trial", "Training", "Other")
SLOT_TYPE_FOR = {
	"ERP Trial": ("Trial", "Both"),
	"Training": ("Training", "Both"),
	"Other": ("Trial", "Training", "Both"),
}


def lock_trainer(trainer: str):
	frappe.db.sql("select name from `tabUser` where name=%s for update", trainer)


def overlapping_bookings(trainer: str, start, end, exclude: str | None = None) -> list[str]:
	filters = [
		["trainer", "=", trainer],
		["status", "in", ACTIVE_BOOKING_STATUSES],
		["start_datetime", "<", end],
		["end_datetime", ">", start],
	]
	if exclude:
		filters.append(["name", "!=", exclude])
	return frappe.get_all("Trial Booking", filters=filters, pluck="name")


def _load_slot_for_booking(slot_name: str, booking_type: str, exclude_booking: str | None = None):
	if not slot_name or not frappe.db.exists("Trainer Slot", slot_name):
		raise frappe.DoesNotExistError("Trainer slot not found.")
	trainer = frappe.db.get_value("Trainer Slot", slot_name, "trainer")
	lock_trainer(trainer)
	slot = frappe.db.get_value(
		"Trainer Slot",
		slot_name,
		[
			"name",
			"trainer",
			"status",
			"slot_type",
			"start_datetime",
			"end_datetime",
			"location",
			"meeting_link",
		],
		as_dict=True,
		for_update=True,
	)
	if not frappe.db.get_value("User", slot.trainer, "enabled"):
		raise ConflictError("This trainer is no longer available.")
	if slot.status != "Available":
		raise ConflictError("This slot has just been taken. Please pick another time.")
	if get_datetime(slot.start_datetime) <= timeutil.now_system():
		raise ConflictError("This slot is in the past.")
	if slot.slot_type not in SLOT_TYPE_FOR[booking_type]:
		raise frappe.ValidationError(f"This slot is for {slot.slot_type} only.")
	if overlapping_bookings(slot.trainer, slot.start_datetime, slot.end_datetime, exclude=exclude_booking):
		raise ConflictError("The trainer already has a booking at this time.")
	return slot


def _party(customer, lead, user):
	if not customer and not lead:
		raise frappe.ValidationError("Select a customer or a lead for the trial.")
	if customer:
		perms.require_party("Customer", customer, user)
		return "Customer", customer
	perms.require_party("CRM Lead", lead, user)
	return "CRM Lead", lead


def book(
	user: str,
	*,
	customer=None,
	lead=None,
	trainer_slot=None,
	booking_type="ERP Trial",
	notes=None,
	idempotency_key=None,
	series: dict | None = None,
):
	"""series: trial period fields for multi-day trials (trial_series, session_no, trial_days,
	trial_start_date, trial_end_date)."""
	perms.require("trial_booking", user)
	if booking_type not in BOOKING_TYPES:
		raise frappe.ValidationError("Invalid booking type.")
	key = (idempotency_key or "").strip()[:140] or None
	if key:
		existing = frappe.db.get_value(
			"Trial Booking", {"idempotency_key": key}, ["name", "booked_by"], as_dict=True
		)
		if existing:
			if existing.booked_by != user:
				raise frappe.ValidationError("This request key was already used.")
			return frappe.get_doc("Trial Booking", existing.name), False

	ref_doctype, ref_name = _party(customer, lead, user)
	if str(trainer_slot or "").startswith(trainer_hours.PREFIX):
		trainer_slot = trainer_hours.materialize(trainer_slot)  # free time from working hours
	slot = _load_slot_for_booking(trainer_slot, booking_type)
	summary = party_summary(ref_doctype, ref_name) or {}

	booking = frappe.get_doc(
		{
			"doctype": "Trial Booking",
			"customer": customer if ref_doctype == "Customer" else None,
			"lead": lead if ref_doctype == "CRM Lead" else (lead or None),
			"party_name": summary.get("name") or ref_name,
			"trainer_slot": slot.name,
			"trainer": slot.trainer,
			"booking_type": booking_type,
			"status": "Booked",
			"start_datetime": slot.start_datetime,
			"end_datetime": slot.end_datetime,
			"booked_by": user,
			"notes": notes,
			"idempotency_key": key,
			**(series or {}),
		}
	)
	booking.flags.hm_service = True
	booking.insert(ignore_permissions=True)
	if not booking.get("trial_series"):
		start_day = get_datetime(slot.start_datetime).date()
		booking.db_set(
			{
				"trial_series": booking.name,
				"session_no": 1,
				"trial_days": booking.get("trial_days") or 1,
				"trial_start_date": start_day,
				"trial_end_date": start_day + dt.timedelta(days=(booking.get("trial_days") or 1) - 1),
			},
			update_modified=False,
		)
	frappe.db.set_value("Trainer Slot", slot.name, "status", "Reserved")

	# One calendar activity for the booking (never two records for one appointment).
	trainer_name = frappe.utils.get_fullname(slot.trainer)
	task = frappe.get_doc(
		{
			"doctype": "CRM Task",
			"title": f"{booking_type}: {booking.party_name} with {trainer_name}"[:140],
			"hm_activity_type": "Trial",
			"status": "Todo",
			"priority": "Medium",
			"assigned_to": user,
			"due_date": slot.start_datetime,
			"start_date": get_datetime(slot.start_datetime).date(),
			"hm_end_datetime": slot.end_datetime,
			"hm_meeting_link": slot.meeting_link,
			"reference_doctype": ref_doctype,
			"reference_docname": ref_name,
			"hm_trial_booking": booking.name,
			"description": notes,
		}
	)
	if slot.location and task.meta.has_field("custom_location"):
		task.custom_location = slot.location
	task.insert(ignore_permissions=True)
	booking.db_set("crm_task", task.name, update_modified=False)

	from hipersign_mobile.notifications.events import on_trial_event

	on_trial_event(booking, "booked", [slot.trainer])
	return booking, True


def _get_booking_for(user: str, booking_id: str, for_update: bool = False):
	if not booking_id or not frappe.db.exists("Trial Booking", booking_id):
		raise frappe.DoesNotExistError("Booking not found.")
	b = frappe.get_doc("Trial Booking", booking_id)
	caps = perms.capabilities(user)
	if not ("team_view" in caps or b.booked_by == user or b.trainer == user):
		raise frappe.DoesNotExistError("Booking not found.")
	if for_update:
		lock_trainer(b.trainer)
		b.reload()
	return b


def _sync_task(booking, status: str | None = None, reason: str | None = None):
	if not booking.crm_task or not frappe.db.exists("CRM Task", booking.crm_task):
		return
	task = frappe.get_doc("CRM Task", booking.crm_task)
	task.due_date = booking.start_datetime
	task.start_date = get_datetime(booking.start_datetime).date()
	task.hm_end_datetime = booking.end_datetime
	if status:
		task.status = status
	task.flags.hm_reason = reason
	task.save(ignore_permissions=True)


def reschedule(user: str, booking_id: str, new_slot: str, reason: str | None = None):
	booking = _get_booking_for(user, booking_id, for_update=True)
	if booking.status not in ACTIVE_BOOKING_STATUSES:
		raise frappe.ValidationError("Only active bookings can be rescheduled.")
	if booking.trainer_slot == new_slot:
		return booking, False
	if str(new_slot or "").startswith(trainer_hours.PREFIX):
		new_slot = trainer_hours.materialize(new_slot)
	new_trainer = frappe.db.get_value("Trainer Slot", new_slot, "trainer")
	if new_trainer and new_trainer != booking.trainer:
		lock_trainer(new_trainer)
	slot = _load_slot_for_booking(new_slot, booking.booking_type, exclude_booking=booking.name)

	old_slot, old_trainer = booking.trainer_slot, booking.trainer
	booking.update(
		{
			"trainer_slot": slot.name,
			"trainer": slot.trainer,
			"start_datetime": slot.start_datetime,
			"end_datetime": slot.end_datetime,
			"status": "Rescheduled",
		}
	)
	booking.flags.hm_service = True
	booking.save(ignore_permissions=True)
	frappe.db.set_value("Trainer Slot", old_slot, "status", "Available")
	frappe.db.set_value("Trainer Slot", slot.name, "status", "Reserved")
	_sync_task(booking, reason=reason)
	if booking.get("trial_series"):
		_renumber(booking.trial_series)  # sessions stay numbered in time order

	from hipersign_mobile.notifications.events import on_trial_event

	on_trial_event(booking, "rescheduled", [old_trainer, slot.trainer])
	return booking, True


def cancel(user: str, booking_id: str, reason: str | None = None):
	booking = _get_booking_for(user, booking_id, for_update=True)
	if booking.status == "Cancelled":
		return booking, False
	if booking.status == "Completed":
		raise frappe.ValidationError("A completed booking cannot be cancelled.")
	booking.status = "Cancelled"
	booking.cancellation_reason = reason
	booking.flags.hm_service = True
	booking.save(ignore_permissions=True)
	if frappe.db.get_value("Trainer Slot", booking.trainer_slot, "status") == "Reserved":
		frappe.db.set_value("Trainer Slot", booking.trainer_slot, "status", "Available")
	_sync_task(booking, status="Canceled", reason=reason)
	if booking.get("trial_series"):
		_renumber(booking.trial_series)

	from hipersign_mobile.notifications.events import on_trial_event

	on_trial_event(booking, "cancelled", [booking.trainer, booking.booked_by])
	return booking, True


def complete(user: str, booking_id: str, notes: str | None = None):
	booking = _get_booking_for(user, booking_id, for_update=True)
	if booking.status == "Completed":
		return booking, False
	if booking.status == "Cancelled":
		raise frappe.ValidationError("A cancelled booking cannot be completed.")
	booking.status = "Completed"
	booking.completion_notes = notes
	booking.flags.hm_service = True
	booking.save(ignore_permissions=True)
	_sync_task(booking, status="Done", reason=notes)

	from hipersign_mobile.notifications.events import on_trial_event

	on_trial_event(booking, "completed", [booking.trainer, booking.booked_by])
	return booking, True


def available_slots(
	start, end, trainer: str | None = None, booking_type: str | None = None, limit: int = 200
):
	now = timeutil.now_system()
	filters = [
		["status", "=", "Available"],
		["start_datetime", ">=", max(start, now)],
		["start_datetime", "<", end],
	]
	if trainer:
		filters.append(["trainer", "=", trainer])
	if booking_type:
		filters.append(["slot_type", "in", SLOT_TYPE_FOR.get(booking_type, ("Both",))])
	slots = frappe.get_all(
		"Trainer Slot",
		filters=filters,
		fields=[
			"name",
			"trainer",
			"trainer_name",
			"slot_type",
			"start_datetime",
			"end_datetime",
			"location",
			"meeting_link",
		],
		order_by="start_datetime asc",
		limit=limit,
	)
	# Advisory only: hide slots that overlap an existing booking; book() re-checks under lock.
	out = []
	for s in slots:
		if overlapping_bookings(s.trainer, s.start_datetime, s.end_datetime):
			continue
		if not frappe.db.get_value("User", s.trainer, "enabled"):
			continue
		out.append(
			{
				"id": s.name,
				"trainer": s.trainer,
				"trainer_name": s.trainer_name or frappe.utils.get_fullname(s.trainer),
				"slot_type": s.slot_type,
				"start": timeutil.to_iso(s.start_datetime),
				"end": timeutil.to_iso(s.end_datetime),
				"location": s.location,
				"meeting_link": s.meeting_link,
			}
		)
	# Free times from working hours (no hand-made slot needed).
	if not booking_type or "Both" in SLOT_TYPE_FOR.get(booking_type, ("Both",)):
		published = {}
		for o in out:
			published.setdefault(o["trainer"], []).append(
				(
					get_datetime(timeutil.parse_client_datetime(o["start"])),
					get_datetime(timeutil.parse_client_datetime(o["end"])),
				)
			)
		for f in trainer_hours.free_times(start, end, trainer=trainer, limit=limit):
			if any(f["start"] < pe and f["end"] > ps for ps, pe in published.get(f["trainer"], [])):
				continue
			out.append(
				{
					"id": f["id"],
					"trainer": f["trainer"],
					"trainer_name": frappe.utils.get_fullname(f["trainer"]),
					"slot_type": "Both",
					"start": timeutil.to_iso(f["start"]),
					"end": timeutil.to_iso(f["end"]),
					"location": None,
					"meeting_link": None,
				}
			)
		out.sort(key=lambda o: (o["start"], o["trainer_name"] or ""))
	return out[:limit]


def serialize(b) -> dict:
	return {
		"id": b.name,
		"status": b.status,
		"booking_type": b.booking_type,
		"party_name": b.party_name,
		"customer": b.customer,
		"lead": b.lead,
		"party": party_summary("Customer", b.customer) if b.customer else party_summary("CRM Lead", b.lead),
		"trainer": b.trainer,
		"trainer_name": frappe.utils.get_fullname(b.trainer) if b.trainer else None,
		"trainer_slot": b.trainer_slot,
		"start": timeutil.to_iso(b.start_datetime),
		"end": timeutil.to_iso(b.end_datetime),
		"booked_by": b.booked_by,
		"activity_id": str(b.crm_task) if b.crm_task else None,
		"notes": b.notes,
		"cancellation_reason": b.cancellation_reason,
		"completion_notes": b.completion_notes,
		"modified": timeutil.to_iso(b.modified),
		"trial": trial_info(b),
	}


def expire_past_slots():
	frappe.db.sql(
		"""update `tabTrainer Slot` set status='Expired'
		where status='Available' and end_datetime < %s""",
		timeutil.now_system(),
	)


# ---------------------------------------------------------------- multi-day trials


def series_sessions(series: str) -> list:
	return frappe.get_all(
		"Trial Booking",
		filters={"trial_series": series},
		fields=["name", "status", "start_datetime", "end_datetime", "session_no", "trainer"],
		order_by="start_datetime asc",
	)


def trial_holiday_list() -> str | None:
	"""Holiday List used to count trial days (Settings, else the default company's)."""
	name = frappe.db.get_single_value("Hipersign Mobile Settings", "trial_holiday_list")
	if name:
		return name
	company = frappe.defaults.get_global_default("company") or frappe.db.get_value("Company", {}, "name")
	return frappe.get_cached_value("Company", company, "default_holiday_list") if company else None


def _holiday_dates(start: dt.date, end: dt.date) -> set:
	hl = trial_holiday_list()
	if not hl:
		return set()
	return {
		getdate(d)
		for d in frappe.get_all(
			"Holiday", filters={"parent": hl, "holiday_date": ["between", [start, end]]}, pluck="holiday_date"
		)
	}


def is_trial_day(d: dt.date, holidays: set) -> bool:
	return d.weekday() != 6 and d not in holidays  # not Sunday, not a holiday


def trial_days_from(start: dt.date, n: int) -> list[dt.date]:
	"""The first n trial days from `start` (skipping Sundays and holidays)."""
	holidays = _holiday_dates(start, start + dt.timedelta(days=n * 2 + 31))
	out, d = [], start
	while len(out) < n:
		if is_trial_day(d, holidays):
			out.append(d)
		d += dt.timedelta(days=1)
		if (d - start).days > 366:
			break
	return out


def _count_trial_days(a: dt.date, b: dt.date, holidays: set) -> int:
	"""Trial days in [a, b] inclusive."""
	n, d = 0, a
	while d <= b:
		if is_trial_day(d, holidays):
			n += 1
		d += dt.timedelta(days=1)
	return n


def trial_info(b, today: dt.date | None = None) -> dict | None:
	"""Countdown of a trial period in trial days (Sundays and holidays don't count):
	day N of D, days left, sessions."""
	series = b.get("trial_series")
	days = cint(b.get("trial_days")) or 1
	if not series:
		return None
	start = (
		getdate(b.trial_start_date) if b.get("trial_start_date") else get_datetime(b.start_datetime).date()
	)
	end = getdate(b.trial_end_date) if b.get("trial_end_date") else start + dt.timedelta(days=days - 1)
	today = today or dt.datetime.now(timeutil.user_tz()).date()
	holidays = _holiday_dates(min(start, today), max(end, today))
	today_off = not is_trial_day(today, holidays)
	if today < start:
		phase, day_no, left = "upcoming", 0, days
	elif today > end:
		phase, day_no, left = "ended", days, 0
	else:
		done = _count_trial_days(start, today, holidays)  # includes today if it's a trial day
		phase = "running"
		day_no = done  # on a Sunday / holiday: the last trial day reached (paused)
		left = max(days - done, 0)
	sessions = series_sessions(series)
	return {
		"series": series,
		"days": days,
		"start_date": str(start),
		"end_date": str(end),
		"phase": phase,  # upcoming | running | ended
		"day_no": day_no,
		"days_left": left,
		"starts_in": (start - today).days if phase == "upcoming" else 0,
		"today_off": today_off and phase == "running",  # Sunday / holiday: the trial pauses
		"sessions": [
			{
				"id": x.name,
				"status": x.status,
				"session_no": cint(x.session_no) or None,
				"start": timeutil.to_iso(x.start_datetime),
				"end": timeutil.to_iso(x.end_datetime),
			}
			for x in sessions
		],
		"sessions_done": sum(1 for x in sessions if x.status == "Completed"),
		"sessions_active": sum(1 for x in sessions if x.status in ACTIVE_BOOKING_STATUSES),
	}


def _same_time_on(day: dt.date, first_start: dt.datetime, trainer: str, booking_type: str):
	"""The free slot id for the same time on `day`, or (None, reason)."""
	from hipersign_mobile.services import trainer_hours

	when = dt.datetime.combine(day, first_start.time())
	windows = trainer_hours.working_windows(trainer, day, day)
	if day not in windows:
		return None, "Trainer is not working that day"
	free = available_slots(
		dt.datetime.combine(day, dt.time.min),
		dt.datetime.combine(day, dt.time.max),
		trainer=trainer,
		booking_type=booking_type,
	)
	for f in free:
		if get_datetime(timeutil.parse_client_datetime(f["start"])) == when:
			return f["id"], None
	return None, "Trainer is busy at this time"


def book_series(user: str, *, trial_days: int, trainer_slot: str, idempotency_key=None, **kw):
	"""Book a trial: the trial runs `trial_days` trial days (Sundays and holidays don't count)
	for the countdown, and this books its first session only. More sessions - each at its own
	time - are added later with add_session, only when they are needed."""
	trial_days = max(1, min(cint(trial_days), 60))
	first, created = book(user, trainer_slot=trainer_slot, idempotency_key=idempotency_key, **kw)
	if created:
		start_day = get_datetime(first.start_datetime).date()
		trial_dates = trial_days_from(start_day, trial_days)
		first.db_set(
			{
				"trial_series": first.name,
				"session_no": 1,
				"trial_days": trial_days,
				"trial_start_date": start_day,
				"trial_end_date": trial_dates[-1] if trial_dates else start_day,
			},
			update_modified=False,
		)
		first.reload()
	return first, created, []


def _series_fields(first) -> dict:
	return {
		"trial_series": first.trial_series or first.name,
		"trial_days": first.trial_days,
		"trial_start_date": first.trial_start_date,
		"trial_end_date": first.trial_end_date,
	}


def _renumber(series: str):
	n = 0
	for x in series_sessions(series):
		if x.status != "Cancelled":
			n += 1
		frappe.db.set_value(
			"Trial Booking",
			x.name,
			"session_no",
			n if x.status != "Cancelled" else 0,
			update_modified=False,
		)


def add_session(user: str, booking_id: str, trainer_slot: str, idempotency_key=None):
	"""One more session in an existing trial (e.g. a day the trainer was busy)."""
	first = _get_booking_for(user, booking_id)
	series = first.trial_series or first.name
	head = frappe.get_doc("Trial Booking", series) if frappe.db.exists("Trial Booking", series) else first
	booking, created = book(
		user,
		customer=head.customer,
		lead=head.lead if not head.customer else None,
		trainer_slot=trainer_slot,
		booking_type=head.booking_type,
		notes=head.notes,
		idempotency_key=idempotency_key,
		series=_series_fields(head),
	)
	_renumber(series)
	booking.reload()
	return booking, created


def set_trial_days(user: str, booking_id: str, days: int):
	"""Change the trial length (trial days; Sundays and holidays don't count) for the whole trial."""
	b = _get_booking_for(user, booking_id)
	days = cint(days)
	if days < 1 or days > 60:
		raise frappe.ValidationError("Trial length must be between 1 and 60 days.")
	series = b.trial_series or b.name
	start = (
		getdate(b.trial_start_date)
		if b.trial_start_date
		else get_datetime(
			frappe.db.get_value("Trial Booking", series, "start_datetime") or b.start_datetime
		).date()
	)
	end = trial_days_from(start, days)[-1]  # Sundays and holidays don't count
	names = [x.name for x in series_sessions(series)] or [b.name]
	for name in names:
		frappe.db.set_value(
			"Trial Booking",
			name,
			{"trial_series": series, "trial_days": days, "trial_start_date": start, "trial_end_date": end},
		)
	return frappe.get_doc("Trial Booking", b.name)
