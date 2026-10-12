from __future__ import annotations

import frappe

from hipersign_mobile.api._common import choice, mobile_api, page_args, page_result
from hipersign_mobile.services import activity_service as svc
from hipersign_mobile.services import permissions as perms
from hipersign_mobile.services import settings as hm_settings
from hipersign_mobile.services import timeutil
from hipersign_mobile.services.party import party_summary

KINDS = ("all", "customer", "lead")


def _phone_digits(query: str) -> str | None:
	"""Last 9 digits of a phone-like query, so +91 / 0 / spaces don't matter."""
	digits = "".join(ch for ch in query if ch.isdigit())
	return digits[-9:] if len(digits) >= 5 else None


_STRIP = "replace(replace(replace(replace(replace({col}, ' ', ''), '-', ''), '+', ''), '(', ''), ')', '')"


def _names_by_phone(table: str, columns: list[str], digits: str) -> list[str]:
	cond = " or ".join(f"{_STRIP.format(col=c)} like %(q)s" for c in columns)
	return frappe.db.sql(
		f"select name from `tab{table}` where {cond} limit 200", {"q": f"%{digits}%"}, pluck=True
	)


def _customer_names(query: str, restrict: set[str] | None, offset: int, limit: int) -> list[str]:
	filters = {"disabled": 0}
	if restrict is not None:
		if not restrict:
			return []
		filters["name"] = ["in", list(restrict)]
	or_filters = None
	if query:
		like = f"%{query}%"
		or_filters = [["customer_name", "like", like], ["name", "like", like], ["email_id", "like", like]]
		digits = _phone_digits(query)
		if digits:
			matches = _names_by_phone("Customer", ["mobile_no"], digits)
			# Customers whose linked Contact has a matching phone number.
			contacts = frappe.db.sql(
				f"select parent from `tabContact Phone` where parenttype='Contact' and {_STRIP.format(col='phone')} like %(q)s limit 200",
				{"q": f"%{digits}%"},
				pluck=True,
			)
			if contacts:
				matches += frappe.get_all(
					"Dynamic Link",
					filters={"parent": ["in", contacts], "parenttype": "Contact", "link_doctype": "Customer"},
					pluck="link_name",
				)
			if matches:
				or_filters.append(["name", "in", list(set(matches))])
	return frappe.get_all(
		"Customer",
		filters=filters,
		or_filters=or_filters,
		pluck="name",
		order_by="customer_name asc",
		start=offset,
		page_length=limit,
	)


def _lead_names(query: str, restrict: set[str] | None, offset: int, limit: int) -> list[str]:
	filters = {"converted": 0}
	if restrict is not None:
		if not restrict:
			return []
		filters["name"] = ["in", list(restrict)]
	or_filters = None
	if query:
		like = f"%{query}%"
		or_filters = [
			["lead_name", "like", like],
			["first_name", "like", like],
			["organization", "like", like],
			["email", "like", like],
			["name", "like", like],
		]
		digits = _phone_digits(query)
		if digits:
			matches = _names_by_phone("CRM Lead", ["mobile_no", "phone"], digits)
			if matches:
				or_filters.append(["name", "in", matches])
	return frappe.get_all(
		"CRM Lead",
		filters=filters,
		or_filters=or_filters,
		pluck="name",
		order_by="modified desc",
		start=offset,
		page_length=limit,
	)


@mobile_api(methods=("GET",))
def search(
	query: str | None = None,
	kind: str | None = None,
	cursor: str | int | None = None,
	page_size: str | int | None = None,
):
	"""Customers and leads the user may see (per Customer Visibility setting)."""
	me = frappe.session.user
	perms.require("customers", me)
	kind = choice(kind, KINDS, "kind", default="all")
	query = (query or "").strip()[:100]
	offset, limit = page_args(cursor, page_size)
	caps = perms.capabilities(me)
	own_only = hm_settings.get().customer_scope == "Own Records" and "team_view" not in caps
	cc = hm_settings.get().default_country_code

	results = []
	if kind == "customer":
		names = _customer_names(
			query, perms.own_party_names("Customer", me) if own_only else None, offset, limit + 1
		)
		results = [("Customer", n) for n in names]
	elif kind == "lead":
		names = _lead_names(
			query, perms.own_party_names("CRM Lead", me) if own_only else None, offset, limit + 1
		)
		results = [("CRM Lead", n) for n in names]
	else:
		# Merged list: customers first, then leads. The cursor walks the combined sequence.
		cust_restrict = perms.own_party_names("Customer", me) if own_only else None
		lead_restrict = perms.own_party_names("CRM Lead", me) if own_only else None
		window = offset + limit + 1
		customers = _customer_names(query, cust_restrict, 0, window)
		leads = (
			_lead_names(query, lead_restrict, 0, max(window - len(customers), 0))
			if len(customers) < window
			else []
		)
		merged = [("Customer", n) for n in customers] + [("CRM Lead", n) for n in leads]
		results = merged[offset : offset + limit + 1]

	items = [party_summary(dt, name, cc=cc) for dt, name in results[:limit]]
	return page_result([i for i in items if i], offset, limit, len(results) > limit)


@mobile_api(methods=("GET",))
def get_detail(doctype: str | None = None, name: str | None = None, customer_id: str | None = None):
	return _detail(doctype, name, customer_id)


def _detail(doctype, name, customer_id):
	me = frappe.session.user
	perms.require("customers", me)
	if customer_id and not name:
		doctype, name = "Customer", customer_id
	doctype = choice(doctype, perms.PARTY_DOCTYPES, "doctype")
	perms.require_party(doctype, name, me)
	data = party_summary(doctype, name, full=True)
	today_start = timeutil.user_today_start(me)
	caps = perms.capabilities(me)
	filters = {"reference_doctype": doctype, "reference_docname": name}
	if "team_view" not in caps:
		filters["assigned_to"] = me  # other employees' activities stay private
	rows = frappe.get_all(
		"CRM Task", filters=filters, fields=svc.task_fields(), order_by="due_date desc", limit=30
	)
	activities = [svc.serialize(r, today_start, with_party=False) for r in rows]
	upcoming = sorted(
		(a for a in activities if a["status"] in ("Pending", "In Progress", "Rescheduled") and a["start"]),
		key=lambda a: a["start"],
	)
	data["activities"] = activities
	data["next_activity"] = upcoming[0] if upcoming else None
	return data


@mobile_api(methods=("GET",))
def get_activities(doctype: str | None = None, name: str | None = None, customer_id: str | None = None):
	return {"items": _detail(doctype, name, customer_id)["activities"]}
