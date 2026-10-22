"""Daily work report in the app: my day (any date), and the team's day for managers / HR."""

from __future__ import annotations

import datetime as dt

import frappe

from hipersign_mobile.api._common import mobile_api
from hipersign_mobile.services import daily_report as dr
from hipersign_mobile.services import permissions as perms
from hipersign_mobile.services import timeutil


def _day(date: str | None, user: str) -> dt.date:
	return timeutil.parse_client_date(date) if date else dt.datetime.now(timeutil.user_tz(user)).date()


@mobile_api(methods=("GET",))
def get_report(date: str | None = None, user: str | None = None):
	me = frappe.session.user
	perms.require("daily_report", me)
	target = user or me
	if target != me and "team_reports" not in perms.capabilities(me):
		raise frappe.PermissionError("You can only see your own report.")
	return dr.build(target, _day(date, me))


@mobile_api(methods=("GET",))
def team_report(date: str | None = None):
	"""Everyone's summary for a day (people with no work and no calls are left out)."""
	me = frappe.session.user
	perms.require("team_reports", me)
	day = _day(date, me)
	out = []
	for user in dr.employees():
		try:
			r = dr.build(user, day)
		except Exception:
			frappe.log_error(title=f"Hipersign daily report: {user}")
			continue
		if dr.has_content(r):
			r.pop("items")
			out.append(r)
	out.sort(key=lambda r: (-r["not_done"], r["name"]))
	return {"date": str(day), "items": out}
