"""Daily call targets for cold callers.

A manager sets, per caller (Call Target): calls per day, the times to check the target
(e.g. an hour and two hours before leaving) and "alert when no calls for N minutes".
- At each check time the caller gets a big (alarm-style) alert: "34 more calls needed".
- During working hours, when no call was made for N minutes and nothing is scheduled right
  now (no follow-up / meeting / training going on), the caller is alerted with what is left.
Calls are counted from the phone's call log synced by the app (Mobile Call Log, outgoing).
"""

from __future__ import annotations

import datetime as dt

import frappe
from frappe.utils import cint, get_datetime, get_time

from hipersign_mobile.services import timeutil

DOCTYPE = "Call Target"
DAY_NAMES = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
WINDOW = dt.timedelta(minutes=10)  # an alert is still sent if the scheduler was late by up to this


def parse_times(value) -> list[dt.time]:
	out = []
	for part in str(value or "").replace(";", ",").split(","):
		part = part.strip()
		if not part:
			continue
		try:
			out.append(get_time(part if part.count(":") == 2 else part + ":00"))
		except Exception:
			raise frappe.ValidationError(f"'{part}' is not a time. Use e.g. 16:30,17:30.")
	return sorted(set(out))


def working_days(t) -> set[int]:
	days = {d.strip()[:3].title() for d in str(t.working_days or "").split(",") if d.strip()}
	return {DAY_NAMES.index(d) for d in days if d in DAY_NAMES} or set(range(6))


def target_for(user: str):
	if not frappe.db.table_exists(DOCTYPE):
		return None
	name = frappe.db.get_value(DOCTYPE, {"user": user, "enabled": 1}, "name")
	return frappe.get_doc(DOCTYPE, name) if name else None


def calls_between(user: str, start: dt.datetime, end: dt.datetime, connected_only: bool = False) -> tuple[int, int, dt.datetime | None]:
	"""(outgoing calls, connected calls, last call time) in [start, end) - system time."""
	rows = frappe.get_all(
		"Mobile Call Log",
		filters=[
			["caller", "=", user],
			["direction", "=", "Outgoing"],
			["call_datetime", ">=", start],
			["call_datetime", "<", end],
		],
		fields=["connected", "call_datetime"],
		limit=5000,
	)
	made = len(rows)
	connected = sum(1 for r in rows if cint(r.connected))
	last = max((get_datetime(r.call_datetime) for r in rows), default=None)
	return made, connected, last


def progress(user: str, day: dt.date | None = None, t=None) -> dict | None:
	"""Today's target progress for the caller (None when no target is set)."""
	t = t or target_for(user)
	if not t:
		return None
	tz = timeutil.user_tz(user)
	day = day or dt.datetime.now(tz).date()
	start, end = timeutil.user_day_bounds(day, day, user)
	made, connected, last = calls_between(user, start, end)
	counted = connected if cint(t.count_connected_only) else made
	target = max(cint(t.daily_target), 0)
	return {
		"target": target,
		"made": made,
		"connected": connected,
		"counted": counted,
		"remaining": max(target - counted, 0),
		"reached": counted >= target > 0,
		"connected_only": bool(cint(t.count_connected_only)),
		"last_call": timeutil.to_iso(last),
		"check_times": [x.strftime("%H:%M") for x in parse_times(t.check_times)],
		"idle_minutes": cint(t.idle_minutes),
		"work_start": str(t.work_start or "09:30:00")[:5],
		"work_end": str(t.work_end or "18:00:00")[:5],
		"working_day": day.weekday() in working_days(t),
	}


def _busy_now(user: str, now: dt.datetime) -> bool:
	"""Something on the caller's calendar right now (a follow-up, meeting or training)."""
	from hipersign_mobile.services import settings as hm_settings

	default = dt.timedelta(minutes=cint(hm_settings.get().default_activity_minutes) or 30)
	for r in frappe.get_all(
		"CRM Task",
		filters=[
			["assigned_to", "=", user],
			["status", "in", ("Backlog", "Todo", "In Progress")],
			["due_date", "<=", now],
			["due_date", ">=", now - dt.timedelta(hours=10)],
		],
		fields=["due_date", "hm_end_datetime"],
	):
		s = get_datetime(r.due_date)
		e = get_datetime(r.hm_end_datetime) if r.hm_end_datetime else s + default
		if s <= now < e:
			return True
	return bool(
		frappe.db.exists(
			"Trial Booking",
			{
				"trainer": user,
				"status": ["in", ("Booked", "Rescheduled", "Pending Confirmation")],
				"start_datetime": ["<=", now],
				"end_datetime": [">", now],
			},
		)
	)


def _lunch(now: dt.datetime) -> bool:
	from hipersign_mobile.services import settings as hm_settings

	s = hm_settings.get()
	if not s.get("lunch_break_start") or not s.get("lunch_break_end"):
		return False
	return get_time(s.lunch_break_start) <= now.time() < get_time(s.lunch_break_end)


def _send(user, key, title, body):
	from hipersign_mobile.notifications import queue

	queue.enqueue(user, "Call Target", title, body, dedup_key=key[:140], reference_doctype=DOCTYPE, reference_name=user)


def process(now: dt.datetime | None = None):
	"""Every minute."""
	if not frappe.db.table_exists(DOCTYPE):
		return
	for name in frappe.get_all(DOCTYPE, filters={"enabled": 1}, pluck="name", limit=500):
		try:
			check(frappe.get_doc(DOCTYPE, name), now)
		except Exception:
			frappe.log_error(title=f"Hipersign call target: {name}")


def check(t, now: dt.datetime | None = None):
	user = t.user
	if not frappe.db.get_value("User", user, "enabled"):
		return
	now_sys = now or timeutil.now_system()
	tz = timeutil.user_tz(user)
	local = now_sys.replace(tzinfo=timeutil.system_tz()).astimezone(tz).replace(tzinfo=None)
	day = local.date()
	if day.weekday() not in working_days(t):
		return
	p = progress(user, day, t)
	if not p or p["target"] <= 0:
		return
	unit = "connected calls" if p["connected_only"] else "calls"

	# 1) Check times: "34 more calls needed".
	for at in parse_times(t.check_times):
		when = dt.datetime.combine(day, at)
		if when <= local < when + WINDOW:
			if p["reached"]:
				title, body = "Call target reached", f"{p['counted']} {unit} today (target {p['target']}). Well done!"
			else:
				title = f"{p['remaining']} more {unit} needed"
				body = f"Today: {p['counted']} of {p['target']} {unit}. {p['remaining']} more to reach your target."
			_send(user, f"ct:{user}:{day}:{at.strftime('%H%M')}", title, body)

	# 2) No calls for a while.
	idle = cint(t.idle_minutes)
	if idle <= 0 or p["reached"]:
		return
	start = dt.datetime.combine(day, get_time(t.work_start or "09:30:00"))
	end = dt.datetime.combine(day, get_time(t.work_end or "18:00:00"))
	if not (start <= local < end) or _lunch(local):
		return
	last = (
		get_datetime(timeutil.parse_client_datetime(p["last_call"])).replace(tzinfo=timeutil.system_tz()).astimezone(tz).replace(tzinfo=None)
		if p["last_call"]
		else None
	)
	since = max(last or start, start)
	gap = local - since
	if gap < dt.timedelta(minutes=idle):
		return
	if _busy_now(user, now_sys):
		return
	n = int(gap.total_seconds() // (idle * 60))  # alert again every `idle` minutes while it lasts
	mins = int(gap.total_seconds() // 60)
	took = f"{mins // 60} h {mins % 60} min" if mins >= 60 else f"{mins} min"
	_send(
		user,
		f"ct-idle:{user}:{since.strftime('%Y%m%d%H%M')}:{n}",
		f"No calls for {took}",
		f"Nothing scheduled right now. {p['remaining']} more {unit} needed today ({p['counted']} of {p['target']}).",
	)
