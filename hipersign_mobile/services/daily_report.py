"""Automatic daily work report.

For each employee and day: the work that was due that day - follow-ups, meetings, trials /
trainings (as salesperson or trainer), team tasks and development tasks - with what was done
and what was not, work finished that day that was due earlier, and the calls made against
the call target. Built live for the app; at the time set in Hipersign Mobile Settings it is
saved as a Hipersign Daily Report and sent to the employee (their own) and to the managers
(everyone's summary).
"""

from __future__ import annotations

import datetime as dt

import frappe
from frappe.utils import cint, get_datetime, get_time

from hipersign_mobile.services import permissions as perms
from hipersign_mobile.services import settings as hm_settings
from hipersign_mobile.services import timeutil

DOCTYPE = "Hipersign Daily Report"
OPEN_TASK = ("Backlog", "Todo", "In Progress")
OPEN_WORK = ("Open", "In Progress", "In Review")


def _local(value, tz) -> dt.datetime | None:
	if not value:
		return None
	return get_datetime(value).replace(tzinfo=timeutil.system_tz()).astimezone(tz).replace(tzinfo=None)


def _fmt_time(d: dt.datetime | None) -> str:
	return d.strftime("%I:%M %p").lstrip("0") if d else ""


def _party_name(doctype, name) -> str:
	if not doctype or not name:
		return ""
	from hipersign_mobile.services.party import party_summary

	try:
		return (party_summary(doctype, name) or {}).get("name") or name
	except Exception:
		return name


def build(user: str, day: dt.date) -> dict:
	from hipersign_mobile.services.activity_service import trainer_task_names
	from hipersign_mobile.services import call_targets

	tz = timeutil.user_tz(user)
	start, end = timeutil.user_day_bounds(day, day, user)
	items: list[dict] = []

	# Follow-ups / meetings / trials due that day (own, and trainings given as trainer).
	fields = ["name", "title", "status", "due_date", "hm_activity_type", "reference_doctype", "reference_docname",
		"hm_completed_at", "hm_trial_booking", "hm_outcome"]
	meta = frappe.get_meta("CRM Task")
	fields = [f for f in fields if f == "name" or meta.has_field(f)]
	due = frappe.get_all(
		"CRM Task",
		filters=[["assigned_to", "=", user], ["due_date", ">=", start], ["due_date", "<", end]],
		fields=fields, order_by="due_date asc", limit=300,
	)
	trainer = trainer_task_names(user, start, end)
	seen = {r.name for r in due}
	if trainer - seen:
		due += frappe.get_all(
			"CRM Task",
			filters=[["name", "in", list(trainer - seen)], ["due_date", ">=", start], ["due_date", "<", end]],
			fields=fields, limit=100,
		)
	# Finished that day but due on an earlier day.
	early = []
	if meta.has_field("hm_completed_at"):
		early = frappe.get_all(
			"CRM Task",
			filters=[["assigned_to", "=", user], ["status", "=", "Done"], ["hm_completed_at", ">=", start],
				["hm_completed_at", "<", end], ["due_date", "<", start]],
			fields=fields, limit=100,
		)
	for r, late in [(x, False) for x in due] + [(x, True) for x in early]:
		kind = r.get("hm_activity_type") or "Follow-up"
		if kind == "Trial":
			kind = "Trial / Training"
		at = _local(r.due_date, tz)
		if r.status == "Done":
			result = "Done" + (f" (was due {at.strftime('%d %b')})" if late and at else "")
		elif r.status == "Canceled":
			result = "Cancelled"
		else:
			result = "Not done"
		items.append({
			"kind": kind,
			"title": r.title or kind,
			"party": _party_name(r.reference_doctype, r.reference_docname),
			"at": _fmt_time(at) if not late else (at.strftime("%d %b") if at else ""),
			"result": result,
			"done": r.status == "Done",
			"open": r.status in OPEN_TASK,
			"reference_doctype": "CRM Task",
			"reference_name": str(r.name),
		})

	# Team tasks and development tasks: due that day, finished that day, or still open and late.
	from hipersign_mobile.services import development as dev

	for k in dev.KINDS.values():
		if not frappe.db.table_exists(k.doctype):
			continue
		label = "Development" if k.doctype == dev.DOCTYPE else "Task"
		rows = frappe.get_all(
			k.doctype,
			filters=[[k.person, "=", user]],
			or_filters=[
				["deadline", "between", [start, end - dt.timedelta(seconds=1)]],
				["completed_on", "between", [start, end - dt.timedelta(seconds=1)]],
			],
			fields=["name", "title", "status", "deadline", "completed_on"] + (["team"] if k.doctype == dev.TEAM_DOCTYPE else []),
			limit=200,
		)
		rows += frappe.get_all(
			k.doctype,
			filters=[[k.person, "=", user], ["status", "in", OPEN_WORK], ["deadline", "<", start]],
			fields=["name", "title", "status", "deadline", "completed_on"] + (["team"] if k.doctype == dev.TEAM_DOCTYPE else []),
			limit=100,
		)
		done_seen = set()
		for r in rows:
			if r.name in done_seen:
				continue
			done_seen.add(r.name)
			deadline = _local(r.deadline, tz)
			if r.status == "Done":
				result = "Done"
			elif r.status == "Cancelled":
				result = "Cancelled"
			elif deadline and deadline.date() < day:
				result = f"Not done - overdue since {deadline.strftime('%d %b')}"
			else:
				result = "Not done" if r.status == "Open" else f"Not done ({r.status.lower()})"
			items.append({
				"kind": (r.get("team") or label) if label == "Task" else label,
				"title": r.title,
				"party": "",
				"at": _fmt_time(deadline) if deadline and deadline.date() == day else (deadline.strftime("%d %b") if deadline else ""),
				"result": result,
				"done": r.status == "Done",
				"open": r.status in OPEN_WORK,
				"reference_doctype": k.doctype,
				"reference_name": r.name,
			})

	made, connected, _last = call_targets.calls_between(user, start, end)
	target = call_targets.target_for(user)
	done = sum(1 for i in items if i["done"])
	not_done = sum(1 for i in items if i["open"])
	return {
		"user": user,
		"name": frappe.utils.get_fullname(user),
		"date": str(day),
		"items": items,
		"done": done,
		"not_done": not_done,
		"cancelled": sum(1 for i in items if i["result"] == "Cancelled"),
		"calls_made": made,
		"calls_connected": connected,
		"call_target": cint(target.daily_target) if target else None,
	}


def has_content(r: dict) -> bool:
	return bool(r["items"] or r["calls_made"])


def employees() -> list[str]:
	"""Everyone the report is for: app users (any Hipersign role) and active employees with a login."""
	users = set(frappe.get_all("Has Role", filters={"role": ["in", perms.MOBILE_ROLES], "parenttype": "User"}, pluck="parent"))
	if frappe.db.table_exists("Employee"):
		users |= {u for u in frappe.get_all("Employee", filters={"status": "Active", "user_id": ["is", "set"]}, pluck="user_id") if u}
	return frappe.get_all(
		"User", filters={"name": ["in", list(users - {"Administrator", "Guest"}) or [""]], "enabled": 1}, pluck="name",
		order_by="full_name asc",
	)


def save(r: dict) -> str:
	name = frappe.db.get_value(DOCTYPE, {"user": r["user"], "report_date": r["date"]}, "name")
	doc = frappe.get_doc(DOCTYPE, name) if name else frappe.new_doc(DOCTYPE)
	doc.update({
		"user": r["user"],
		"user_name": r["name"],
		"report_date": r["date"],
		"done_count": r["done"],
		"not_done_count": r["not_done"],
		"calls_made": r["calls_made"],
		"calls_connected": r["calls_connected"],
		"call_target": r["call_target"] or 0,
	})
	doc.set("items", [])
	for i in r["items"]:
		doc.append("items", {k: i[k] for k in ("kind", "title", "party", "at", "result", "reference_doctype", "reference_name")})
	doc.flags.ignore_permissions = True
	doc.save() if name else doc.insert()
	return doc.name


def summary_line(r: dict) -> str:
	parts = [f"{r['done']} done", f"{r['not_done']} not done"]
	if r["calls_made"] or r["call_target"]:
		parts.append(f"{r['calls_made']} calls" + (f" / {r['call_target']}" if r["call_target"] else ""))
	return " · ".join(parts)


def process(now: dt.datetime | None = None):
	"""Every minute: at the report time, save and send the day's reports (once a day)."""
	s = hm_settings.get()
	if not cint(s.get("enable_daily_report")) or not frappe.db.table_exists(DOCTYPE):
		return
	try:
		at = get_time(s.get("daily_report_time") or "18:30:00")
	except Exception:
		at = dt.time(18, 30)
	now = now or timeutil.now_system()
	if not (dt.datetime.combine(now.date(), at) <= now < dt.datetime.combine(now.date(), at) + dt.timedelta(minutes=15)):
		return
	day = now.date()
	key = f"hipersign_mobile:daily_report:{day}"
	if frappe.cache.get_value(key):
		return
	frappe.cache.set_value(key, 1, expires_in_sec=6 * 3600)
	send_all(day)


def send_all(day: dt.date):
	from hipersign_mobile.notifications import queue

	reports = []
	for user in employees():
		try:
			r = build(user, day)
		except Exception:
			frappe.log_error(title=f"Hipersign daily report: {user}")
			continue
		if not has_content(r):
			continue
		name = save(r)
		reports.append((r, name))
		queue.enqueue(
			user, "Daily Report", f"Your day - {day.strftime('%d %b')}", summary_line(r),
			dedup_key=f"dr:{day}:{user}", reference_doctype=DOCTYPE, reference_name=name,
		)
	if not reports:
		return
	managers = {
		u for u in frappe.get_all("Has Role", filters={"role": ["in", (perms.ROLE_MANAGER, perms.ROLE_CEO, perms.ROLE_HR)], "parenttype": "User"}, pluck="parent")
	}
	done = sum(r["done"] for r, _ in reports)
	not_done = sum(r["not_done"] for r, _ in reports)
	behind = [r["name"] for r, _ in reports if r["not_done"]]
	body = f"{len(reports)} people · {done} done · {not_done} not done"
	if behind:
		body += " · pending: " + ", ".join(behind[:5]) + ("..." if len(behind) > 5 else "")
	for m in managers:
		if not frappe.db.get_value("User", m, "enabled"):
			continue
		queue.enqueue(
			m, "Daily Report", f"Team report - {day.strftime('%d %b')}", body,
			dedup_key=f"dr-team:{day}:{m}", reference_doctype=DOCTYPE, reference_name=None,
		)
	frappe.db.commit()
