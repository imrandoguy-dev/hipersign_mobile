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


def call_stats(user: str, start: dt.datetime, end: dt.datetime) -> frappe._dict:
	"""Outgoing calls in [start, end): made, connected, new, new connected, last call time."""
	rows = frappe.get_all(
		"Mobile Call Log",
		filters=[["caller", "=", user], ["direction", "=", "Outgoing"], ["call_datetime", ">=", start], ["call_datetime", "<", end]],
		fields=["connected", "call_type", "call_datetime"],
		limit=5000,
	)
	new = [r for r in rows if (r.call_type or "New") == "New"]
	return frappe._dict(
		made=len(rows),
		connected=sum(1 for r in rows if cint(r.connected)),
		new=len(new),
		new_connected=sum(1 for r in new if cint(r.connected)),
		last=max((get_datetime(r.call_datetime) for r in rows), default=None),
	)


def meetings_assigned(user: str, start: dt.datetime, end: dt.datetime) -> int:
	"""Meetings the person booked in [start, end): from the app (CRM Task meetings they created)
	and from the daily report (Scheduled Meeting assigned by them)."""
	n = 0
	if frappe.get_meta("CRM Task").has_field("hm_activity_type"):
		n += frappe.db.count(
			"CRM Task",
			{"owner": user, "hm_activity_type": "Meeting", "status": ["!=", "Canceled"], "creation": ["between", [start, end - dt.timedelta(seconds=1)]]},
		)
	if frappe.db.exists("DocType", "Scheduled Meeting") and frappe.get_meta("Scheduled Meeting").has_field("assigned_by"):
		n += frappe.db.count(
			"Scheduled Meeting", {"assigned_by": user, "creation": ["between", [start, end - dt.timedelta(seconds=1)]]}
		)
	return n


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
	st = call_stats(user, start, end)
	connected_only, new_only = bool(cint(t.count_connected_only)), bool(cint(t.get("count_new_only")))
	counted = (st.new_connected if connected_only else st.new) if new_only else (st.connected if connected_only else st.made)
	target = max(cint(t.daily_target), 0)
	meeting_target = max(cint(t.get("meeting_target")), 0)
	meetings = meetings_assigned(user, start, end)
	return {
		"target": target,
		"made": st.made,
		"connected": st.connected,
		"new_connected": st.new_connected,
		"counted": counted,
		"remaining": max(target - counted, 0),
		"reached": counted >= target > 0,
		"connected_only": connected_only,
		"new_only": new_only,
		"unit": count_unit(connected_only, new_only),
		"meeting_target": meeting_target,
		"meetings": meetings,
		"meetings_remaining": max(meeting_target - meetings, 0),
		"last_call": timeutil.to_iso(st.last),
		"check_times": [x.strftime("%H:%M") for x in parse_times(t.check_times)],
		"idle_minutes": cint(t.idle_minutes),
		"work_start": str(t.work_start or "09:30:00")[:5],
		"work_end": str(t.work_end or "18:00:00")[:5],
		"working_day": day.weekday() in working_days(t),
	}


def count_unit(connected_only: bool, new_only: bool) -> str:
	if new_only:
		return "new connected calls" if connected_only else "new calls"
	return "connected calls" if connected_only else "calls"


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
	if not p or (p["target"] <= 0 and p["meeting_target"] <= 0):
		return
	unit = p["unit"]
	meet = ""
	if p["meeting_target"]:
		meet = (
			f" Meetings: {p['meetings']} of {p['meeting_target']}"
			+ (f" - {p['meetings_remaining']} more to assign." if p["meetings_remaining"] else " - done.")
		)

	# 1) Check times: "34 more calls needed".
	for at in parse_times(t.check_times):
		when = dt.datetime.combine(day, at)
		if when <= local < when + WINDOW:
			if p["target"] <= 0 or p["reached"]:
				if p["meetings_remaining"]:
					title = f"{p['meetings_remaining']} more meeting{'s' if p['meetings_remaining'] > 1 else ''} to assign"
					body = (f"{p['counted']} {unit} today." if p["target"] else "") + meet
				else:
					title = "Target reached"
					body = (f"{p['counted']} {unit} today (target {p['target']})." if p["target"] else "") + meet + " Well done!"
			else:
				title = f"{p['remaining']} more {unit} needed"
				body = f"Today: {p['counted']} of {p['target']} {unit}. {p['remaining']} more to reach your target." + meet
			_send(user, f"ct:{user}:{day}:{at.strftime('%H%M')}", title, body)

	# 2) No calls for a while.
	st = idle_state(t, p, local, now_sys)
	if not st:
		return
	idle = cint(t.idle_minutes)
	n = int(st["minutes"] // idle)  # alert again every `idle` minutes while it lasts
	_send(
		user,
		f"ct-idle:{user}:{st['since']}:{n}",
		f"No calls for {st['text']}",
		f"Nothing scheduled right now. {p['remaining']} more {unit} needed today ({p['counted']} of {p['target']}).",
	)


def idle_state(t, p: dict, local: dt.datetime, now_sys: dt.datetime) -> dict | None:
	"""{minutes, text, since} when the caller has made no call for the set time during working
	hours (target not reached, nothing on the calendar right now), else None."""
	idle = cint(t.idle_minutes)
	if idle <= 0 or p["target"] <= 0 or p["reached"] or not p.get("working_day", True):
		return None
	day = local.date()
	start = dt.datetime.combine(day, get_time(t.work_start or "09:30:00"))
	end = dt.datetime.combine(day, get_time(t.work_end or "18:00:00"))
	if not (start <= local < end) or _lunch(local):
		return None
	tz = timeutil.user_tz(t.user)
	last = (
		get_datetime(timeutil.parse_client_datetime(p["last_call"])).replace(tzinfo=timeutil.system_tz()).astimezone(tz).replace(tzinfo=None)
		if p["last_call"]
		else None
	)
	since = max(last or start, start)
	gap = local - since
	if gap < dt.timedelta(minutes=idle) or _busy_now(t.user, now_sys):
		return None
	mins = int(gap.total_seconds() // 60)
	return {
		"minutes": mins,
		"text": f"{mins // 60} h {mins % 60} min" if mins >= 60 else f"{mins} min",
		"since": since.strftime("%Y%m%d%H%M"),
	}


def live_progress(user: str) -> dict | None:
	"""Today's progress plus the "no calls" state, for the app's calendar banner."""
	t = target_for(user)
	if not t:
		return None
	now_sys = timeutil.now_system()
	tz = timeutil.user_tz(user)
	local = now_sys.replace(tzinfo=timeutil.system_tz()).astimezone(tz).replace(tzinfo=None)
	p = progress(user, local.date(), t)
	st = idle_state(t, p, local, now_sys) if p else None
	if p is not None:
		p["idle"] = bool(st)
		p["idle_for"] = st["text"] if st else None
	return p
