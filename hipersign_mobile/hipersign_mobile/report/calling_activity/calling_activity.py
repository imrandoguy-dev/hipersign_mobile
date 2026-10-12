"""Calling Activity: what each caller did per day.

New call      = the number had never been reached before (no earlier connected call).
Follow-up     = an earlier call to that number connected.
Connected     = the call lasted more than 0 seconds (from the phone's call history).
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import add_days, cint, flt, getdate


def execute(filters=None):
	filters = frappe._dict(filters or {})
	from_date = getdate(filters.get("from_date") or add_days(frappe.utils.today(), -6))
	to_date = getdate(filters.get("to_date") or frappe.utils.today())
	if from_date > to_date:
		frappe.throw(_("From date must be before To date."))
	group_by = filters.get("group_by") or "Day and Caller"
	by_day = group_by in ("Day and Caller", "Day")
	by_caller = group_by in ("Day and Caller", "Caller")

	rows = _rows(from_date, to_date, filters.get("caller"), by_day, by_caller)
	columns = _columns(by_day, by_caller)
	return columns, rows, None, _chart(from_date, to_date, filters.get("caller")), _summary(rows)


def _rows(from_date, to_date, caller, by_day, by_caller):
	keys = []
	if by_day:
		keys.append("date(call_datetime)")
	if by_caller:
		keys.append("caller")
	group = ", ".join(keys)
	select_keys = []
	if by_day:
		select_keys.append("date(call_datetime) as day")
	if by_caller:
		select_keys.append("caller")
	params = {"from": from_date, "to": add_days(to_date, 1), "caller": caller}
	cond = "call_datetime >= %(from)s and call_datetime < %(to)s"
	if caller:
		cond += " and caller = %(caller)s"
	data = frappe.db.sql(
		f"""select {", ".join(select_keys)},
			sum(direction = 'Outgoing') as total,
			sum(direction = 'Outgoing' and connected = 1) as connected,
			sum(direction = 'Outgoing' and call_type = 'New') as new_calls,
			sum(direction = 'Outgoing' and call_type = 'New' and connected = 1) as new_connected,
			sum(direction = 'Outgoing' and call_type = 'Follow-up') as followups,
			sum(direction = 'Outgoing' and call_type = 'Follow-up' and connected = 1) as followups_connected,
			sum(case when direction = 'Outgoing' then duration_seconds else 0 end) as talk_time,
			sum(direction = 'Incoming' and connected = 1) as incoming,
			sum(direction = 'Missed') as missed
		from `tabMobile Call Log`
		where {cond}
		group by {group}
		order by {"day desc" if by_day else "total desc"}{", total desc" if by_day and by_caller else ""}""",
		params,
		as_dict=True,
	)

	leads = _new_leads(from_date, to_date, caller, by_day, by_caller)
	names = {}
	for r in data:
		for k in ("total", "connected", "new_calls", "new_connected", "followups", "followups_connected",
				  "talk_time", "incoming", "missed"):
			r[k] = cint(r.get(k))
		r["not_connected"] = r["total"] - r["connected"]
		r["connect_rate"] = flt(r["connected"] * 100.0 / r["total"], 1) if r["total"] else 0
		r["avg_call"] = int(r["talk_time"] / r["connected"]) if r["connected"] else 0
		r["new_leads"] = leads.get((r.get("day"), r.get("caller")), 0)
		if r.get("caller"):
			if r["caller"] not in names:
				names[r["caller"]] = frappe.utils.get_fullname(r["caller"])
			r["caller_name"] = names[r["caller"]]
	return data


def _new_leads(from_date, to_date, caller, by_day, by_caller):
	"""CRM Leads each caller created (owner) in the period, keyed like the rows."""
	params = {"from": from_date, "to": add_days(to_date, 1), "caller": caller}
	cond = "creation >= %(from)s and creation < %(to)s"
	if caller:
		cond += " and owner = %(caller)s"
	out = {}
	for r in frappe.db.sql(
		f"select date(creation) as day, owner, count(*) as n from `tabCRM Lead` where {cond} group by date(creation), owner",
		params,
		as_dict=True,
	):
		key = (r.day if by_day else None, r.owner if by_caller else None)
		out[key] = out.get(key, 0) + cint(r.n)
	return out


def _columns(by_day, by_caller):
	cols = []
	if by_day:
		cols.append({"fieldname": "day", "label": _("Date"), "fieldtype": "Date", "width": 110})
	if by_caller:
		cols += [
			{"fieldname": "caller", "label": _("Caller"), "fieldtype": "Link", "options": "User", "width": 170, "hidden": 1},
			{"fieldname": "caller_name", "label": _("Caller"), "fieldtype": "Data", "width": 160},
		]
	cols += [
		{"fieldname": "total", "label": _("Calls Made"), "fieldtype": "Int", "width": 100},
		{"fieldname": "connected", "label": _("Connected"), "fieldtype": "Int", "width": 100},
		{"fieldname": "not_connected", "label": _("Not Connected"), "fieldtype": "Int", "width": 115},
		{"fieldname": "connect_rate", "label": _("Connect %"), "fieldtype": "Percent", "width": 95},
		{"fieldname": "new_calls", "label": _("New Calls"), "fieldtype": "Int", "width": 95},
		{"fieldname": "new_connected", "label": _("New - Connected"), "fieldtype": "Int", "width": 125},
		{"fieldname": "followups", "label": _("Follow-up Calls"), "fieldtype": "Int", "width": 120},
		{"fieldname": "followups_connected", "label": _("Follow-up - Connected"), "fieldtype": "Int", "width": 155},
		{"fieldname": "talk_time", "label": _("Talk Time"), "fieldtype": "Duration", "width": 110},
		{"fieldname": "avg_call", "label": _("Avg Call"), "fieldtype": "Duration", "width": 100},
		{"fieldname": "incoming", "label": _("Incoming Answered"), "fieldtype": "Int", "width": 135},
		{"fieldname": "missed", "label": _("Missed"), "fieldtype": "Int", "width": 80},
		{"fieldname": "new_leads", "label": _("New Leads"), "fieldtype": "Int", "width": 95},
	]
	return cols


def _chart(from_date, to_date, caller):
	params = {"from": from_date, "to": add_days(to_date, 1), "caller": caller}
	cond = "direction = 'Outgoing' and call_datetime >= %(from)s and call_datetime < %(to)s"
	if caller:
		cond += " and caller = %(caller)s"
	per_day = {
		r.day: r
		for r in frappe.db.sql(
			f"""select date(call_datetime) as day, sum(call_type = 'New') as new_calls,
			sum(call_type = 'Follow-up') as followups, sum(connected = 1) as connected
			from `tabMobile Call Log` where {cond} group by date(call_datetime)""",
			params,
			as_dict=True,
		)
	}
	labels, new, follow, connected = [], [], [], []
	day = from_date
	while day <= to_date:
		r = per_day.get(day) or {}
		labels.append(day.strftime("%d %b"))
		new.append(cint(r.get("new_calls")))
		follow.append(cint(r.get("followups")))
		connected.append(cint(r.get("connected")))
		day = add_days(day, 1)
	return {
		"data": {
			"labels": labels,
			"datasets": [
				{"name": _("New Calls"), "values": new, "chartType": "bar"},
				{"name": _("Follow-up Calls"), "values": follow, "chartType": "bar"},
				{"name": _("Connected"), "values": connected, "chartType": "line"},
			],
		},
		"type": "axis-mixed",
		"barOptions": {"stacked": 1},
		"colors": ["#1783C1", "#0A3C6E", "#28A745"],
	}


def _summary(rows):
	total = sum(r["total"] for r in rows)
	connected = sum(r["connected"] for r in rows)
	talk = sum(r["talk_time"] for r in rows)
	return [
		{"value": total, "label": _("Calls Made"), "datatype": "Int", "indicator": "Blue"},
		{"value": connected, "label": _("Connected"), "datatype": "Int", "indicator": "Green"},
		{
			"value": flt(connected * 100.0 / total, 1) if total else 0,
			"label": _("Connect %"),
			"datatype": "Percent",
			"indicator": "Green" if total and connected * 2 >= total else "Orange",
		},
		{"value": sum(r["new_calls"] for r in rows), "label": _("New Calls"), "datatype": "Int", "indicator": "Blue"},
		{"value": sum(r["followups"] for r in rows), "label": _("Follow-up Calls"), "datatype": "Int", "indicator": "Blue"},
		{"value": round(talk / 3600.0, 1), "label": _("Talk Time (hours)"), "datatype": "Float", "indicator": "Grey"},
		{"value": sum(r["new_leads"] for r in rows), "label": _("New Leads"), "datatype": "Int", "indicator": "Purple"},
	]
