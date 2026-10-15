"""Trainer availability from working hours (no hand-made Trainer Slots needed).

A trainer is free at a time inside working hours unless she already has an active trial
booking or an open follow-up / meeting assigned to her that overlaps it. These "automatic"
times have ids like ``auto:<trainer>:<YYYYMMDDHHMM>:<minutes>``; when one is booked, a real
Trainer Slot is created for it under the trainer lock, so the rest of booking works as before.
"""

from __future__ import annotations

import datetime as dt

import frappe
from frappe.utils import get_datetime, get_time

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
	)


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
	names = [trainer] if trainer else trainers()
	if trainer and trainer not in trainers():
		return []
	step = dt.timedelta(minutes=cfg.minutes)
	out = []
	for t in names:
		taken = busy(t, start, end)
		day = start.date()
		while day <= end.date() and len(out) < limit:
			if day.weekday() in cfg.days:
				cur = dt.datetime.combine(day, cfg.start)
				day_end = dt.datetime.combine(day, cfg.end)
				while cur + step <= day_end:
					if (
						cur >= start
						and cur + step <= end
						and cur > now
						and not _overlaps(cur, cur + step, taken)
					):
						out.append(
							{
								"trainer": t,
								"start": cur,
								"end": cur + step,
								"id": slot_id(t, cur, cfg.minutes),
							}
						)
					cur += step
			day += dt.timedelta(days=1)
	out.sort(key=lambda x: (x["start"], x["trainer"]))
	return out[:limit]


def materialize(value: str) -> str:
	"""Turn an automatic time into a real Trainer Slot (called while booking). Returns its name."""
	from hipersign_mobile.services.booking_service import lock_trainer
	from hipersign_mobile.services.errors import ConflictError

	cfg = config()
	trainer, start, minutes = parse_id(value)
	end = start + dt.timedelta(minutes=minutes)
	if not cfg.enabled:
		raise frappe.ValidationError("Automatic trainer times are switched off. Pick a published slot.")
	if trainer not in trainers():
		raise ConflictError("This trainer is no longer available.")
	if start <= timeutil.now_system():
		raise ConflictError("This time is in the past.")
	if (
		start.weekday() not in cfg.days
		or start.time() < cfg.start
		or end > dt.datetime.combine(start.date(), cfg.end)
		or minutes != cfg.minutes
	):
		raise frappe.ValidationError("This time is outside the trainer's working hours.")
	lock_trainer(trainer)
	if _overlaps(start, end, busy(trainer, start, end)):
		raise ConflictError("The trainer just got busy at this time. Please pick another time.")
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
			"notes": "Created automatically from working hours when booked in Hipersign.",
		}
	)
	slot.flags.hm_auto = True
	slot.insert(ignore_permissions=True)
	return slot.name
