from __future__ import annotations

import frappe

from hipersign_mobile.api._common import (
	choice,
	limit_per_user,
	mobile_api,
	optional_str,
	page_args,
	page_result,
	require_str,
)
from hipersign_mobile.services import activity_service as svc
from hipersign_mobile.services import google_places
from hipersign_mobile.services import permissions as perms
from hipersign_mobile.services import settings as hm_settings
from hipersign_mobile.services import timeutil
from hipersign_mobile.services.party import party_summary
from hipersign_mobile.services.phone import normalize_phone

KINDS = ("all", "customer", "lead", "deal")


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


def _deal_names(query: str, restrict: set[str] | None, offset: int, limit: int) -> list[str]:
	filters = {}
	if restrict is not None:
		if not restrict:
			return []
		filters["name"] = ["in", list(restrict)]
	or_filters = None
	if query:
		like = f"%{query}%"
		or_filters = [
			["organization", "like", like],
			["lead_name", "like", like],
			["first_name", "like", like],
			["email", "like", like],
			["name", "like", like],
		]
		digits = _phone_digits(query)
		if digits:
			matches = _names_by_phone("CRM Deal", ["mobile_no", "phone"], digits)
			# Deals whose linked contact (CRM Contacts table) has this number.
			contacts = frappe.db.sql(
				f"select parent from `tabContact Phone` where parenttype='Contact' and {_STRIP.format(col='phone')} like %(q)s limit 200",
				{"q": f"%{digits}%"},
				pluck=True,
			)
			if contacts:
				matches += frappe.get_all(
					"CRM Contacts",
					filters={"contact": ["in", contacts], "parenttype": "CRM Deal"},
					pluck="parent",
				)
			if matches:
				or_filters.append(["name", "in", list(set(matches))])
	return frappe.get_all(
		"CRM Deal",
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

	finders = {
		"customer": ("Customer", _customer_names),
		"lead": ("CRM Lead", _lead_names),
		"deal": ("CRM Deal", _deal_names),
	}

	def restrict(doctype):
		return perms.own_party_names(doctype, me) if own_only else None

	if kind != "all":
		doctype, finder = finders[kind]
		names = finder(query, restrict(doctype), offset, limit + 1)
		results = [(doctype, n) for n in names]
	else:
		# Merged list: customers, then leads, then deals. The cursor walks the combined sequence.
		window = offset + limit + 1
		merged = []
		for doctype, finder in finders.values():
			if len(merged) >= window:
				break
			merged += [(doctype, n) for n in finder(query, restrict(doctype), 0, window - len(merged))]
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


# ---------------------------------------------------------------- unknown numbers / Google


def _party_for_phone(e164: str) -> tuple[str, str] | None:
	"""(doctype, name) of the customer or open lead that has this number, if any."""
	digits = _phone_digits(e164)
	if not digits:
		return None
	for doctype, finder in (("Customer", _customer_names), ("CRM Lead", _lead_names), ("CRM Deal", _deal_names)):
		names = finder(digits, None, 0, 1)
		if names:
			return doctype, names[0]
	return None


def _visible_summary(found: tuple[str, str] | None, me: str) -> dict | None:
	if not found:
		return None
	doctype, name = found
	if not perms.can_access_party(doctype, name, me):
		return {"kind": "Hidden", "doctype": doctype, "id": name, "name": "Already in ERP (another salesperson's)"}
	return party_summary(doctype, name)


@mobile_api(methods=("GET",))
def lookup_number(phone: str | None = None):
	"""Who is this number? ERPNext first; if unknown, Google Maps (when switched on)."""
	me = frappe.session.user
	perms.require("customers", me)
	limit_per_user("lookup_number", 60, 60)
	e164 = normalize_phone(phone, hm_settings.get().default_country_code)
	if not e164:
		raise frappe.ValidationError("Enter a valid phone number.")
	party = _visible_summary(_party_for_phone(e164), me)
	google, google_error = None, None
	if not party and google_places.enabled():
		try:
			google = google_places.lookup(e164)
		except google_places.GoogleLookupError as exc:
			google_error = str(exc)
	return {
		"phone": e164,
		"party": party,
		"google": google,
		"google_enabled": google_places.enabled(),
		"google_error": google_error,
	}


def _lead_status() -> str | None:
	if frappe.db.exists("CRM Lead Status", "New"):
		return "New"
	rows = frappe.get_all("CRM Lead Status", pluck="name", order_by="position asc", limit=1)
	return rows[0] if rows else None


def _lead_source(place_found: bool) -> str | None:
	if not place_found:
		return None
	source = (hm_settings.get().google_lead_source or "").strip()
	if not source:
		return None
	if not frappe.db.exists("CRM Lead Source", source):
		frappe.get_doc({"doctype": "CRM Lead Source", "source_name": source}).insert(ignore_permissions=True)
	return source


@mobile_api(methods=("POST",))
def create_lead(
	phone: str | None = None,
	name: str | None = None,
	address: str | None = None,
	maps_url: str | None = None,
	place_id: str | None = None,
):
	"""New CRM Lead for a number that is not in ERPNext (business name from Google or typed).
	Never duplicates: an existing customer/lead with the number or Google place is returned."""
	me = frappe.session.user
	perms.require("customers", me)
	limit_per_user("create_lead", 20, 60)
	e164 = normalize_phone(phone, hm_settings.get().default_country_code)
	if not e164:
		raise frappe.ValidationError("Enter a valid phone number.")
	name = require_str(name, "Name", 140)

	found = _party_for_phone(e164)
	if not found and place_id:
		existing = frappe.db.get_value("CRM Lead", {"hm_google_place_id": place_id}, "name")
		found = ("CRM Lead", existing) if existing else None
	if found:
		return {"party": _visible_summary(found, me), "created": False}

	lead = frappe.get_doc(
		{
			"doctype": "CRM Lead",
			"first_name": name,
			"organization": name,
			"mobile_no": e164,
			"lead_owner": me,
			"status": _lead_status(),
			"source": _lead_source(bool(place_id)),
			"hm_google_place_id": optional_str(place_id, 140),
			"hm_google_maps_url": optional_str(maps_url, 500),
		}
	).insert(ignore_permissions=True)

	if address or maps_url:
		parts = []
		if address:
			parts.append(frappe.utils.escape_html(address))
		if maps_url:
			parts.append(f'<a href="{frappe.utils.escape_html(maps_url)}" target="_blank">Open in Google Maps</a>')
		frappe.get_doc(
			{
				"doctype": "Comment",
				"comment_type": "Info",
				"reference_doctype": "CRM Lead",
				"reference_name": lead.name,
				"content": "Found on Google Maps: " + " - ".join(parts),
			}
		).insert(ignore_permissions=True)
	frappe.local.hm_party_cache = {}
	return {"party": party_summary("CRM Lead", lead.name), "created": True}
