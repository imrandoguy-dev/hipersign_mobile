from __future__ import annotations

import re

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
	try:
		from hipersign_mobile.services import journey

		data["journey"] = journey.journey(doctype, name)
	except Exception:
		frappe.log_error(title=f"Hipersign customer stage: {doctype} {name}")
		data["journey"] = None
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
			"hm_address": optional_str(address, 500),
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



_PHONE_IN_TEXT = re.compile(r"(?:\+|00)?\d[\d\s().-]{7,}\d")


@mobile_api(methods=("POST",))
def resolve_shared(text: str | None = None):
	"""A business shared to Hipersign from Google Search / Maps -> its phone number,
	name and address (Google Places), plus the ERP party if the number is already known."""
	me = frappe.session.user
	perms.require("customers", me)
	limit_per_user("resolve_shared", 30, 60)
	text = (text or "").strip()
	if not text:
		raise frappe.ValidationError("Nothing was shared.")
	cc = hm_settings.get().default_country_code

	place, error = None, None
	# A shared text that already contains a phone number (e.g. a contact card) needs no Google.
	phone = None
	for candidate in _PHONE_IN_TEXT.findall(google_places.URL_RE.sub(" ", text)):
		phone = normalize_phone(candidate, cc)
		if phone:
			break
	if not phone:
		if not google_places.enabled():
			error = "Google lookup is switched off in Hipersign Mobile Settings."
		else:
			query = google_places.query_from_shared(text)
			if not query:
				error = "Could not read a business name from what was shared."
			else:
				try:
					place = google_places.place_with_phone(query)
				except google_places.GoogleLookupError as exc:
					error = str(exc)
				if place and place.get("phone"):
					phone = normalize_phone(place["phone"], cc)
				elif place:
					error = "Google has no phone number for this business."
				elif not error:
					error = "Google did not find this business."
	party = _visible_summary(_party_for_phone(phone), me) if phone else None
	google = None
	if place:
		google = {k: place.get(k) for k in ("place_id", "name", "address", "maps_url")}
	return {"phone": phone, "party": party, "google": google, "error": error}


@mobile_api(methods=("GET",))
def phone_directory():
	"""Numbers of the customers, leads and deals this user may see, for the caller pop-up.
	Key = last 9 digits (works for +91 and +971 numbers alike)."""
	me = frappe.session.user
	perms.require("customers", me)
	limit_per_user("phone_directory", 10, 60)
	caps = perms.capabilities(me)
	own_only = hm_settings.get().customer_scope == "Own Records" and "team_view" not in caps
	allowed = {dt: perms.own_party_names(dt, me) for dt in perms.PARTY_DOCTYPES} if own_only else None

	entries: dict[str, dict] = {}

	def add(raw, doctype, name, label, kind, status=None):
		digits = "".join(ch for ch in str(raw or "") if ch.isdigit())
		if len(digits) < 9 or (allowed is not None and name not in allowed[doctype]):
			return
		key = digits[-9:]
		if key in entries:  # customer wins over deal, deal over lead
			return
		entries[key] = {"k": key, "n": (label or name)[:80], "t": kind, "d": doctype, "i": name, "s": status}

	for r in frappe.db.sql(
		"select name, customer_name, mobile_no from `tabCustomer` where disabled=0", as_dict=True
	):
		add(r.mobile_no, "Customer", r.name, r.customer_name, "Customer")
	for r in frappe.db.sql(
		"""select cp.phone, dl.link_name, c.customer_name from `tabContact Phone` cp
		join `tabDynamic Link` dl on dl.parent = cp.parent and dl.parenttype = 'Contact' and dl.link_doctype = 'Customer'
		join `tabCustomer` c on c.name = dl.link_name and c.disabled = 0
		where cp.parenttype = 'Contact'""",
		as_dict=True,
	):
		add(r.phone, "Customer", r.link_name, r.customer_name, "Customer")
	for r in frappe.db.sql(
		"select name, organization, lead_name, mobile_no, phone, status from `tabCRM Deal`", as_dict=True
	):
		for num in (r.mobile_no, r.phone):
			add(num, "CRM Deal", r.name, r.organization or r.lead_name, "Deal", r.status)
	for r in frappe.db.sql(
		"""select cc.parent, cp.phone, d.organization, d.lead_name, d.status from `tabCRM Contacts` cc
		join `tabContact Phone` cp on cp.parent = cc.contact and cp.parenttype = 'Contact'
		join `tabCRM Deal` d on d.name = cc.parent
		where cc.parenttype = 'CRM Deal'""",
		as_dict=True,
	):
		add(r.phone, "CRM Deal", r.parent, r.organization or r.lead_name, "Deal", r.status)
	for r in frappe.db.sql(
		"""select name, organization, lead_name, first_name, mobile_no, phone, status
		from `tabCRM Lead` where converted = 0""",
		as_dict=True,
	):
		for num in (r.mobile_no, r.phone):
			add(num, "CRM Lead", r.name, r.organization or r.lead_name or r.first_name, "Lead", r.status)

	items = list(entries.values())[:50000]
	return {"items": items, "count": len(items), "generated_at": frappe.utils.now()}


@mobile_api(methods=("GET",))
def edit_form(doctype: str | None = None, name: str | None = None):
	"""Fields the app may edit for this customer / lead / deal, with current values and choices."""
	from hipersign_mobile.services import party_edit

	me = frappe.session.user
	perms.require("customers", me)
	doctype = choice(doctype, perms.PARTY_DOCTYPES, "doctype")
	name = require_str(name, "name")
	perms.require_party(doctype, name, me)
	return {"fields": party_edit.form(doctype, name)}


@mobile_api(methods=("POST",))
def update_party(doctype: str | None = None, name: str | None = None, values=None):
	"""Save edits from the app. `values` = {field key: new value} (only changed fields)."""
	import json

	from hipersign_mobile.services import party_edit

	me = frappe.session.user
	perms.require("customers", me)
	limit_per_user("party_edit", 30, 60)
	doctype = choice(doctype, perms.PARTY_DOCTYPES, "doctype")
	name = require_str(name, "name")
	perms.require_party(doctype, name, me)
	if isinstance(values, str):
		try:
			values = json.loads(values)
		except ValueError:
			raise frappe.ValidationError("values must be JSON.")
	if not isinstance(values, dict) or not values:
		raise frappe.ValidationError("Nothing to save.")
	changed = party_edit.save(doctype, name, values)
	frappe.local.hm_party_cache = {}
	data = _detail(doctype, name, None)
	data["changed"] = changed
	return data
