from __future__ import annotations

import datetime as dt

import frappe

from hipersign_mobile.api._common import mobile_api
from hipersign_mobile.services import activity_service as svc
from hipersign_mobile.services import permissions as perms
from hipersign_mobile.services import settings as hm_settings
from hipersign_mobile.services import timeutil

MAX_RANGE_DAYS = 62
STATE_ORDER = {"active": 0, "rescheduled": 1, "pending": 2, "completed": 3, "cancelled": 4}


@mobile_api(methods=("GET",))
def get_my_day(start_date: str | None = None, end_date: str | None = None, user: str | None = None):
	"""Activities between two local dates (inclusive) for the signed-in user.

	Also returns overdue open work (kept visible until its status changes) and
	open work that has no date yet, so nothing assigned is ever invisible.
	`user` (another employee) is only honoured for roles with team_view.
	"""
	me = frappe.session.user
	perms.require("calendar", me)
	start_d = (
		timeutil.parse_client_date(start_date) if start_date else dt.datetime.now(timeutil.user_tz(me)).date()
	)
	end_d = timeutil.parse_client_date(end_date) if end_date else start_d
	if end_d < start_d:
		raise frappe.ValidationError("end_date must be on or after start_date.")
	if (end_d - start_d).days > MAX_RANGE_DAYS:
		raise frappe.ValidationError(f"Date range is limited to {MAX_RANGE_DAYS} days.")

	range_start, range_end = timeutil.user_day_bounds(start_d, end_d, me)
	today_start = timeutil.user_today_start(me)
	cc = hm_settings.get().default_country_code
	target = user or None

	rows = svc.list_activities(me, start=range_start, end=range_end, limit=500, for_user=target)
	items = [svc.serialize(r, today_start, cc=cc) for r in rows[:500]]

	# Everything still open whose time has passed (earlier days and earlier today), newest first.
	overdue = svc.list_activities(
		me,
		status_bucket="Open",
		end=timeutil.now_system(),
		limit=200,
		for_user=target,
		order_by="due_date desc",
	)
	unscheduled_rows = frappe.get_all(
		"CRM Task",
		filters={
			"assigned_to": target or me,
			"status": ["in", svc.OPEN_STATUSES],
			"due_date": ["is", "not set"],
		},
		fields=svc.task_fields(),
		order_by="creation desc",
		limit=50,
	)

	items.sort(key=lambda a: (a["start"] or "", STATE_ORDER.get(a["display_state"], 9)))
	return {
		"range": {"start_date": str(start_d), "end_date": str(end_d)},
		"items": items,
		"overdue": [svc.serialize(r, today_start, cc=cc) for r in overdue[:200]],
		"unscheduled": [svc.serialize(r, today_start, cc=cc) for r in unscheduled_rows],
		"counts": _counts(items),
		"synced_at": timeutil.to_iso(timeutil.now_system()),
	}


@mobile_api(methods=("GET",))
def get_month_markers(month: str):
	"""Per-day counts for a month view ("2026-10"): {date: {state: count}}."""
	me = frappe.session.user
	perms.require("calendar", me)
	try:
		first = dt.date.fromisoformat(f"{month[:7]}-01")
	except ValueError:
		raise frappe.ValidationError("month must look like 2026-10.")
	nxt = (first.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
	start, end = timeutil.user_day_bounds(first, nxt - dt.timedelta(days=1), me)
	today_start = timeutil.user_today_start(me)
	rows = svc.list_activities(me, start=start, end=end, limit=1000)
	tz = timeutil.user_tz(me)
	markers: dict[str, dict[str, int]] = {}
	for r in rows[:1000]:
		local = (
			frappe.utils.get_datetime(r.due_date).replace(tzinfo=timeutil.system_tz()).astimezone(tz).date()
		)
		state = svc.display_state(r, today_start)
		day = markers.setdefault(str(local), {})
		day[state] = day.get(state, 0) + 1
	return {"month": first.strftime("%Y-%m"), "days": markers}


def _counts(items):
	out = {}
	for a in items:
		out[a["display_state"]] = out.get(a["display_state"], 0) + 1
	return out
