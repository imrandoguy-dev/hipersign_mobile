"""Trainer availability from each trainer's shift (no hand-made Trainer Slots needed).

Working hours per day come from the trainer's Employee record: the active Shift Assignment
for that date, else the Employee's Default Shift, else the hours in Hipersign Mobile Settings.
Days off: the Employee's (or company's) Holiday List, else the working days in Settings, plus
approved Leave Applications. The lunch break (Shift Type's own, else Settings) is never offered.
Within those hours she is free unless she already has an active trial booking or an open
follow-up / meeting assigned to her that overlaps. These "automatic"
times have ids like ``auto:<trainer>:<YYYYMMDDHHMM>:<minutes>``; when one is booked, a real
Trainer Slot is created for it under the trainer lock, so the rest of booking works as before.
"""

from __future__ import annotations

import datetime as dt

import frappe
from frappe.utils import get_datetime, get_time, getdate

from hipersign_mobile.services import permissions as perms
from hipersign_mobile.services import settings as hm_settings
from hipersign_mobile.services import timeutil

DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
OPEN_TASK = ("Backlog", "Todo", "In Progress")
ACTIVE_BOOKING = ("Pending Confirmation", "Booked", "Rescheduled")
PREFIX = "auto:"


def config() -> frappe._dict:
	s = hm_settings.get()
	days = {d.strip()[:3].title() for d in str(s.trainer_working_days or "").split(",") if d.strip()}
	return frappe._dict(
		enabled=bool(s.auto_trainer_slots),
		days={DAY_NAMES.index(d) for d in days if d in DAY_NAMES} or set(range(6)),
		start=get_time(s.trainer_day_start or "09:30:00"),
		end=get_time(s.trainer_day_end or "18:00:00"),
		minutes=max(15, min(int(s.trial_slot_minutes or 60), 480)),
		lunch=_break(s.get("lunch_break_start"), s.get("lunch_break_end")),
	)


def _break(start, end):
	if not start or not end:
		return None
	a, b = get_time(start), get_time(end)
	return (a, b) if b > a else None


# ---------------------------------------------------------------- shifts (HRMS)


def _has(doctype: str) -> bool:
	return bool(frappe.db.exists("DocType", doctype))


def employee_for(user: str):
	meta = frappe.get_meta("Employee")
	fields = ["name", "company"] + [f for f in ("default_shift", "holiday_list") if meta.has_field(f)]
	emp = frappe.db.get_value("Employee", {"user_id": user, "status": "Active"}, fields, as_dict=True)
	if emp:
		emp.setdefault("default_shift", None)
		emp.setdefault("holiday_list", None)
	return emp


def shift_for(employee, day: dt.date) -> str | None:
	"""Shift Type name for this employee on this date (active assignment, else default shift)."""
	if not employee or not _has("Shift Assignment"):
		return employee.default_shift if employee else None
	rows = frappe.db.sql(
		"""select shift_type from `tabShift Assignment`
		where employee = %s and docstatus = 1 and status = 'Active'
		and start_date <= %s and (end_date is null or end_date >= %s)
		order by start_date desc, creation desc limit 1""",
		(employee.name, day, day),
	)
	return rows[0][0] if rows else employee.default_shift


def _shift_hours(shift: str):
	"""(start, end, lunch) of a Shift Type; lunch may be None."""
	if not shift or not _has("Shift Type"):
		return None
	meta = frappe.get_meta("Shift Type")
	fields = ["start_time", "end_time"] + [f for f in ("hm_break_start", "hm_break_end") if meta.has_field(f)]
	row = frappe.db.get_value("Shift Type", shift, fields, as_dict=True)
	if not row or not row.start_time or not row.end_time:
		return None
	return (
		get_time(row.start_time),
		get_time(row.end_time),
		_break(row.get("hm_break_start"), row.get("hm_break_end")),
	)


def holiday_list_for(employee) -> str | None:
	if not employee:
		return None
	return employee.holiday_list or (
		frappe.get_cached_value("Company", employee.company, "default_holiday_list")
		if employee.company
		else None
	)


def _holidays(holiday_list: str | None, start: dt.date, end: dt.date) -> set:
	if not holiday_list:
		return set()
	return set(
		frappe.get_all(
			"Holiday",
			filters={"parent": holiday_list, "holiday_date": ["between", [start, end]]},
			pluck="holiday_date",
		)
	)


def _leave_days(employee, start: dt.date, end: dt.date) -> set:
	if not employee or not _has("Leave Application"):
		return set()
	out = set()
	for la in frappe.get_all(
		"Leave Application",
		filters={
			"employee": employee.name,
			"status": "Approved",
			"docstatus": 1,
			"from_date": ["<=", end],
			"to_date": [">=", start],
		},
		fields=["from_date", "to_date"],
	):
		d = max(getdate(la.from_date), start)
		while d <= min(getdate(la.to_date), end):
			out.add(d)
			d += dt.timedelta(days=1)
	return out


def working_windows(trainer: str, start: dt.date, end: dt.date, cfg=None) -> dict:
	"""{date: (window_start, window_end, lunch_or_None)} for the days the trainer works."""
	cfg = cfg or config()
	emp = employee_for(trainer) if _has("Employee") else None
	hl = holiday_list_for(emp)
	off = _holidays(hl, start, end) | _leave_days(emp, start, end)
	out = {}
	day = start
	while day <= end:
		if day not in off and (hl or day.weekday() in cfg.days):
			hours = _shift_hours(shift_for(emp, day)) if emp else None
			if hours:
				s_time, e_time, lunch = hours
				lunch = lunch or cfg.lunch
			else:
				s_time, e_time, lunch = cfg.start, cfg.end, cfg.lunch
			w_start = dt.datetime.combine(day, s_time)
			w_end = dt.datetime.combine(day, e_time)
			if w_end <= w_start:
				w_end += dt.timedelta(days=1)  # night shift ends the next day
			lunch_range = None
			if lunch:
				ls, le = dt.datetime.combine(day, lunch[0]), dt.datetime.combine(day, lunch[1])
				if ls < w_end and le > w_start:
					lunch_range = (ls, le)
			out[day] = (w_start, w_end, lunch_range)
		day += dt.timedelta(days=1)
	return out


def trainers() -> list[str]:
	users = frappe.get_all(
		"Has Role", filters={"role": perms.ROLE_TRAINER, "parenttype": "User"}, pluck="parent"
	)
	if not users:
		return []
	return frappe.get_all(
		"User", filters={"name": ["in", users], "enabled": 1}, pluck="name", order_by="full_name asc"
	)


def busy(trainer: str, start: dt.datetime, end: dt.datetime) -> list[tuple[dt.datetime, dt.datetime]]:
	"""Times the trainer is taken: her trials and open activities assigned to her."""
	out = []
	for b in frappe.get_all(
		"Trial Booking",
		filters=[
			["trainer", "=", trainer],
			["status", "in", ACTIVE_BOOKING],
			["start_datetime", "<", end],
			["end_datetime", ">", start],
		],
		fields=["start_datetime", "end_datetime"],
	):
		out.append((get_datetime(b.start_datetime), get_datetime(b.end_datetime)))
	default = dt.timedelta(minutes=hm_settings.get().default_activity_minutes)
	for t in frappe.get_all(
		"CRM Task",
		filters=[
			["assigned_to", "=", trainer],
			["status", "in", OPEN_TASK],
			["due_date", "<", end],
			["due_date", ">=", start - dt.timedelta(days=1)],
		],
		fields=["due_date", "hm_end_datetime", "hm_trial_booking"],
	):
		if t.hm_trial_booking:
			continue  # already counted via its booking
		s = get_datetime(t.due_date)
		e = get_datetime(t.hm_end_datetime) if t.hm_end_datetime else s + default
		if e > start:
			out.append((s, e))
	# Hand-made slots that are reserved / unavailable also block the time.
	for sl in frappe.get_all(
		"Trainer Slot",
		filters=[
			["trainer", "=", trainer],
			["status", "in", ("Reserved", "Unavailable")],
			["start_datetime", "<", end],
			["end_datetime", ">", start],
		],
		fields=["start_datetime", "end_datetime"],
	):
		out.append((get_datetime(sl.start_datetime), get_datetime(sl.end_datetime)))
	return out


def _overlaps(s, e, ranges) -> bool:
	return any(s < re and e > rs for rs, re in ranges)


def slot_id(trainer: str, start: dt.datetime, minutes: int) -> str:
	return f"{PREFIX}{trainer}:{start.strftime('%Y%m%d%H%M')}:{minutes}"


def parse_id(value: str) -> tuple[str, dt.datetime, int]:
	try:
		body = value[len(PREFIX) :]
		trainer, stamp, minutes = body.rsplit(":", 2)
		return trainer, dt.datetime.strptime(stamp, "%Y%m%d%H%M"), int(minutes)
	except Exception:
		raise frappe.ValidationError("This time is no longer valid. Please pick another time.")


def free_times(
	start: dt.datetime, end: dt.datetime, trainer: str | None = None, limit: int = 200
) -> list[dict]:
	cfg = config()
	if not cfg.enabled:
		return []
	now = timeutil.now_system()
	start = max(start, now)
	if end <= start:
		return []
	all_trainers = trainers()
	if trainer and trainer not in all_trainers:
		return []
	names = [trainer] if trainer else all_trainers
	step = dt.timedelta(minutes=cfg.minutes)
	out = []
	for t in names:
		taken = busy(t, start - dt.timedelta(days=1), end)
		windows = working_windows(t, (start - dt.timedelta(days=1)).date(), end.date(), cfg)
		for _day, (w_start, w_end, lunch) in sorted(windows.items()):
			blocked = taken + ([lunch] if lunch else [])
			cur = w_start
			while cur + step <= w_end:
				if (
					cur >= start
					and cur + step <= end
					and cur > now
					and not _overlaps(cur, cur + step, blocked)
				):
					out.append(
						{"trainer": t, "start": cur, "end": cur + step, "id": slot_id(t, cur, cfg.minutes)}
					)
				cur += step
	out.sort(key=lambda x: (x["start"], x["trainer"]))
	return out[:limit]


MIN_MINUTES, MAX_MINUTES = 15, 600


def _check_exact(trainer: str, start: dt.datetime, end: dt.datetime, minutes: int):
	from hipersign_mobile.services.errors import ConflictError

	if end <= start:
		raise frappe.ValidationError("The end time must be after the start time.")
	if not MIN_MINUTES <= minutes <= MAX_MINUTES:
		raise frappe.ValidationError("A session must be between 15 minutes and 10 hours.")
	if start <= timeutil.now_system():
		raise ConflictError("This time is in the past.")
	if start.date() != (end - dt.timedelta(minutes=1)).date():
		raise frappe.ValidationError("The session must start and end on the same day.")
	if _overlaps(start, end, busy(trainer, start, end)):
		name = frappe.utils.get_fullname(trainer)
		raise ConflictError(f"{name} is busy at this time (another trial, training, meeting or follow-up).")


def exact_time(trainer: str, start: dt.datetime, end: dt.datetime) -> dict:
	"""A hand-picked time for a trainer, as a slot the app can book (advisory check)."""
	start, end = get_datetime(start).replace(second=0, microsecond=0), get_datetime(end).replace(second=0, microsecond=0)
	minutes = int((end - start).total_seconds() // 60)
	_check_exact(trainer, start, end, minutes)
	note = None
	windows = working_windows(trainer, start.date(), start.date())
	inside = [w for w in windows.values() if w[0] <= start and end <= w[1]]
	if not inside:
		note = "Outside the trainer's working hours"
	elif inside[0][2] and _overlaps(start, end, [inside[0][2]]):
		note = "During the trainer's lunch break"
	return {
		"id": slot_id(trainer, start, minutes),
		"trainer": trainer,
		"trainer_name": frappe.utils.get_fullname(trainer),
		"slot_type": "Both",
		"start": timeutil.to_iso(start),
		"end": timeutil.to_iso(end),
		"location": None,
		"meeting_link": None,
		"note": note,
	}


def materialize(value: str) -> str:
	"""Turn an automatic time into a real Trainer Slot (called while booking). Returns its name."""
	from hipersign_mobile.services.booking_service import lock_trainer
	from hipersign_mobile.services.errors import ConflictError

	# Any exact time (picked from the free list or typed by hand) can be booked, as long as
	# the trainer is free then; working hours only shape the suggestions.
	trainer, start, minutes = parse_id(value)
	end = start + dt.timedelta(minutes=minutes)
	if trainer not in trainers():
		raise ConflictError("This trainer is no longer available.")
	lock_trainer(trainer)
	_check_exact(trainer, start, end, minutes)
	slot = frappe.get_doc(
		{
			"doctype": "Trainer Slot",
			"trainer": trainer,
			"trainer_name": frappe.utils.get_fullname(trainer),
			"status": "Available",
			"slot_type": "Both",
			"start_datetime": start,
			"end_datetime": end,
			"duration_minutes": minutes,
			"notes": "Exact time booked in Hipersign.",
		}
	)
	slot.flags.hm_auto = True
	slot.insert(ignore_permissions=True)
	return slot.name
