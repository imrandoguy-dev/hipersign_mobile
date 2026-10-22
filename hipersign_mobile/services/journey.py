"""Where a customer / lead / deal stands: lead -> meeting -> trial -> training -> quotation ->
customer (order / invoice) -> development -> support.

Everything about the same company is looked at together: a customer, the CRM deal that became
it and the lead that became the deal. The current stage is the furthest step that is still
going on (a running training beats a sent quotation, open development work beats an order).
"""

from __future__ import annotations

import datetime as dt

import frappe
from frappe.utils import get_datetime, getdate

from hipersign_mobile.services import timeutil

ACTIVE_TRIAL = ("Pending Confirmation", "Booked", "Rescheduled")
OPEN_WORK = ("Open", "In Progress", "In Review")
OPEN_TASK = ("Backlog", "Todo", "In Progress")


def _has(doctype: str, field: str | None = None) -> bool:
	if not frappe.db.exists("DocType", doctype):
		return False
	return not field or frappe.get_meta(doctype).has_field(field)


def related(doctype: str, name: str) -> dict[str, set[str]]:
	"""The customer(s), deal(s) and lead(s) that are the same company."""
	out = {"Customer": set(), "CRM Deal": set(), "CRM Lead": set()}
	out[doctype].add(name)
	for _ in range(2):  # lead -> deal -> customer and back
		if _has("CRM Deal", "lead"):
			for lead in list(out["CRM Lead"]):
				out["CRM Deal"] |= set(frappe.get_all("CRM Deal", filters={"lead": lead}, pluck="name"))
			for deal in list(out["CRM Deal"]):
				lead = frappe.db.get_value("CRM Deal", deal, "lead")
				if lead:
					out["CRM Lead"].add(lead)
		for field in ("erpnext_customer",):
			if _has("CRM Deal", field):
				for deal in list(out["CRM Deal"]):
					c = frappe.db.get_value("CRM Deal", deal, field)
					if c:
						out["Customer"].add(c)
				for c in list(out["Customer"]):
					out["CRM Deal"] |= set(frappe.get_all("CRM Deal", filters={field: c}, pluck="name"))
		if _has("Customer", "crm_deal"):
			for c in list(out["Customer"]):
				d = frappe.db.get_value("Customer", c, "crm_deal")
				if d:
					out["CRM Deal"].add(d)
			for deal in list(out["CRM Deal"]):
				out["Customer"] |= set(frappe.get_all("Customer", filters={"crm_deal": deal}, pluck="name"))
	return {k: {v for v in vals if v} for k, vals in out.items()}


def _trials(rel) -> list:
	or_filters = []
	if rel["Customer"]:
		or_filters.append(["customer", "in", list(rel["Customer"])])
	if rel["CRM Lead"]:
		or_filters.append(["lead", "in", list(rel["CRM Lead"])])
	if not or_filters:
		return []
	return frappe.get_all(
		"Trial Booking",
		or_filters=or_filters,
		fields=["name", "booking_type", "status", "start_datetime", "end_datetime", "trainer", "trial_series",
			"trial_days", "trial_start_date", "trial_end_date"],
		order_by="start_datetime asc",
		limit=200,
	)


def _quotations(rel) -> list:
	if not _has("Quotation"):
		return []
	or_filters = []
	if rel["Customer"]:
		or_filters.append(["party_name", "in", list(rel["Customer"])])
	if rel["CRM Deal"]:
		or_filters.append(["party_name", "in", list(rel["CRM Deal"])])
	leads = _erp_leads(rel)
	if leads:
		or_filters.append(["party_name", "in", list(leads)])
	if not or_filters:
		return []
	return frappe.get_all(
		"Quotation", or_filters=or_filters, filters={"docstatus": ["<", 2]},
		fields=["name", "status", "docstatus", "transaction_date", "grand_total", "currency", "valid_till"],
		order_by="transaction_date desc", limit=50,
	)


def _erp_leads(rel) -> set[str]:
	"""ERPNext Leads made for quotations to these CRM leads (see quotations.erp_lead_for)."""
	if not _has("Lead", "hm_crm_lead") or not rel["CRM Lead"]:
		return set()
	return set(frappe.get_all("Lead", filters={"hm_crm_lead": ["in", list(rel["CRM Lead"])]}, pluck="name"))


def _count(doctype, customers, extra=None) -> int:
	if not customers or not _has(doctype):
		return 0
	f = {"customer": ["in", list(customers)], "docstatus": 1}
	f.update(extra or {})
	return frappe.db.count(doctype, f)


def _work(rel) -> list:
	from hipersign_mobile.services import development as dev

	out = []
	if not rel["Customer"]:
		return out
	for k in dev.KINDS.values():
		if _has(k.doctype, "customer"):
			for r in frappe.get_all(
				k.doctype, filters={"customer": ["in", list(rel["Customer"])]},
				fields=["name", "title", "status", "deadline"] + (["team"] if k.doctype == dev.TEAM_DOCTYPE else []),
				order_by="deadline desc", limit=50,
			):
				r.kind = "Development" if k.doctype == dev.DOCTYPE else (r.get("team") or "Task")
				out.append(r)
	return out


def journey(doctype: str, name: str) -> dict:
	rel = related(doctype, name)
	now = timeutil.now_system()
	today = now.date()
	steps: list[dict] = []

	def step(key, label, state, detail=None, at=None):
		steps.append({"key": key, "label": label, "state": state, "detail": detail, "at": timeutil.to_iso(at) if at else None})

	# Lead / deal
	lead_status = None
	for lead in rel["CRM Lead"]:
		lead_status = frappe.db.get_value("CRM Lead", lead, "status")
	deal_status = None
	for deal in rel["CRM Deal"]:
		deal_status = frappe.db.get_value("CRM Deal", deal, "status")
	if rel["CRM Lead"] or rel["CRM Deal"]:
		step("lead", "Lead", "done", deal_status and f"Deal: {deal_status}" or (lead_status and f"Lead: {lead_status}"))

	# Meetings / follow-ups
	refs = [(dt_, n) for dt_ in ("Customer", "CRM Lead", "CRM Deal") for n in rel[dt_]]
	meetings = []
	if refs:
		meetings = frappe.get_all(
			"CRM Task",
			or_filters=[["reference_docname", "in", [n for _, n in refs]]],
			filters={"hm_activity_type": "Meeting"} if frappe.get_meta("CRM Task").has_field("hm_activity_type") else {},
			fields=["name", "status", "due_date"], order_by="due_date desc", limit=50,
		)
	if meetings:
		upcoming = [m for m in meetings if m.status in OPEN_TASK and m.due_date and get_datetime(m.due_date) >= now]
		done = [m for m in meetings if m.status == "Done"]
		step("meeting", "Meeting", "active" if upcoming else ("done" if done else "pending"),
			f"Next {frappe.utils.format_datetime(upcoming[-1].due_date, 'd MMM, h:mm a')}" if upcoming else f"{len(done)} done",
			upcoming[-1].due_date if upcoming else (done[0].due_date if done else None))

	# Trials and trainings
	trials = _trials(rel)
	for kind, label in (("ERP Trial", "Trial"), ("Training", "Training")):
		rows = [t for t in trials if (t.booking_type == kind or (kind == "ERP Trial" and t.booking_type == "Other"))]
		if not rows:
			continue
		series = {}
		for t in rows:
			series.setdefault(t.trial_series or t.name, []).append(t)
		state, detail, at = "done", None, None
		for _s, sessions in series.items():
			first = sessions[0]
			start = getdate(first.trial_start_date or first.start_datetime)
			end = getdate(first.trial_end_date or first.end_datetime)
			active = [x for x in sessions if x.status in ACTIVE_TRIAL]
			if active and start <= today <= end:
				state = "active"
				n = max(1, (today - start).days + 1)
				detail = f"Day {n} of {first.trial_days or (end - start).days + 1} · with {frappe.utils.get_fullname(first.trainer)}"
				at = first.start_datetime
			elif active and today < start and state != "active":
				state = "upcoming"
				detail = f"Starts {start.strftime('%d %b')} · with {frappe.utils.get_fullname(first.trainer)}"
				at = first.start_datetime
			elif state == "done" and not detail:
				done = [x for x in sessions if x.status == "Completed"]
				cancelled = all(x.status == "Cancelled" for x in sessions)
				state = "cancelled" if cancelled else "done"
				detail = "Cancelled" if cancelled else f"{len(done)} session{'s' if len(done) != 1 else ''} done"
				at = sessions[-1].start_datetime
		step("trial" if kind == "ERP Trial" else "training", label, state, detail, at)

	# Quotation
	quotes = _quotations(rel)
	if quotes:
		q = quotes[0]
		ordered = any(x.status in ("Ordered", "Partially Ordered") for x in quotes)
		lost = all(x.status == "Lost" for x in quotes)
		state = "done" if ordered else ("cancelled" if lost else "active")
		step("quotation", "Quotation", state,
			f"{q.name} · {frappe.utils.fmt_money(q.grand_total, currency=q.currency)} · {q.status}", q.transaction_date)

	# Customer (won): order or invoice
	orders = _count("Sales Order", rel["Customer"])
	invoices = _count("Sales Invoice", rel["Customer"])
	if orders or invoices:
		step("customer", "Customer", "done",
			", ".join(x for x in (orders and f"{orders} order{'s' if orders > 1 else ''}", invoices and f"{invoices} invoice{'s' if invoices > 1 else ''}") if x))
	elif rel["Customer"] and not quotes and not trials:
		step("customer", "Customer", "done", None)

	# Development / tasks for the customer
	work = _work(rel)
	if work:
		open_ = [w for w in work if w.status in OPEN_WORK]
		late = [w for w in open_ if w.deadline and get_datetime(w.deadline) < now]
		step("development", "Development", "active" if open_ else "done",
			(f"{len(open_)} open" + (f", {len(late)} late" if late else "") + f" · {open_[0].title}") if open_ else f"{len(work)} done",
			open_[0].deadline if open_ else None)

	order = ["development", "training", "trial", "quotation", "customer", "meeting", "lead"]
	current = None
	for key in order:  # furthest step still going on
		s = next((x for x in steps if x["key"] == key and x["state"] in ("active", "upcoming")), None)
		if s:
			current = s
			break
	if not current:
		for key in ("customer", "development", "quotation", "training", "trial", "meeting", "lead"):
			s = next((x for x in steps if x["key"] == key and x["state"] == "done"), None)
			if s:
				current = s
				break
	stage = None
	if current:
		label = current["label"]
		names = {
			"development": "In development", "training": "In training", "trial": "On trial",
			"quotation": "Quotation sent", "customer": "Customer", "meeting": "Meeting", "lead": "Lead",
		}
		if current["state"] == "upcoming":
			label = {"training": "Training booked", "trial": "Trial booked", "meeting": "Meeting booked"}.get(current["key"], label)
		elif current["state"] == "active" or current["key"] in names:
			label = names.get(current["key"], label)
		if current["key"] == "lead" and (deal_status or lead_status):
			label = deal_status or lead_status
		stage = {"key": current["key"], "label": label, "detail": current["detail"], "state": current["state"]}
	return {"stage": stage, "steps": steps, "quotations": [
		{"id": q.name, "status": q.status, "date": str(q.transaction_date), "total": q.grand_total, "currency": q.currency,
		 "total_text": frappe.utils.fmt_money(q.grand_total, currency=q.currency)}
		for q in quotes[:10]
	]}
