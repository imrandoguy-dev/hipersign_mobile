"""Automatic WhatsApp template messages (through the Frappe WhatsApp app).

Rules live in Hipersign Mobile Settings > WhatsApp Rules: "after a call" (connected / not
connected / any) or "new lead created" -> an approved WhatsApp template. Each send creates a
WhatsApp Message (type Outgoing, template) linked to the customer / lead / deal; Frappe
WhatsApp does the actual sending to Meta when that record is inserted.

Sending happens in a background job after the call / lead is saved, so a WhatsApp problem
never blocks the salesperson; failures go to the Error Log.
"""

from __future__ import annotations

import json
import re

import frappe
from frappe.utils import add_to_date, cint, get_datetime, now_datetime

from hipersign_mobile.services import settings as hm_settings
from hipersign_mobile.services.party import party_summary
from hipersign_mobile.services.phone import normalize_phone

AFTER_ANY = "After any call"
AFTER_NOT_CONNECTED = "After call - not connected"
AFTER_CONNECTED = "After call - connected"
NEW_LEAD = "New lead created"

PARTY_FILTER = {
	"Customers, Leads and Deals": None,
	"": None,
	None: None,
	"Leads only": "CRM Lead",
	"Deals only": "CRM Deal",
	"Customers only": "Customer",
}

# Calls older than this when they reach the server (e.g. history uploaded the next morning)
# don't trigger a message - it would arrive hours after the call.
MAX_CALL_AGE_HOURS = 2
TEMPLATE_FIELD = "whatsapp_template"
RULE_DOCTYPE = "Hipersign WhatsApp Rule"


def available() -> bool:
	return "frappe_whatsapp" in frappe.get_installed_apps() and bool(
		frappe.db.exists("DocType", "WhatsApp Message")
	)


def _all_rules() -> list:
	s = frappe.get_cached_doc("Hipersign Mobile Settings")
	if not cint(s.get("enable_whatsapp_rules")):
		return []
	return list(s.get("whatsapp_rules") or [])


def _rules(trigger_names: set[str]) -> list:
	return [
		r for r in _all_rules() if cint(r.enabled) and r.trigger in trigger_names and r.get(TEMPLATE_FIELD)
	]


# ---------------------------------------------------------------- triggers


def after_call(call_log) -> int:
	"""Called for every new Mobile Call Log. Returns how many messages were queued."""
	if call_log.direction != "Outgoing" or not call_log.party:
		return 0
	when = get_datetime(call_log.call_datetime)
	if when < add_to_date(now_datetime(), hours=-MAX_CALL_AGE_HOURS):
		return 0
	triggers = {AFTER_ANY, AFTER_CONNECTED if cint(call_log.connected) else AFTER_NOT_CONNECTED}
	queued = 0
	for rule in _rules(triggers):
		wanted = PARTY_FILTER.get(rule.parties)
		if wanted and wanted != call_log.party_doctype:
			continue
		_queue(rule, call_log.party_doctype, call_log.party, call_log.phone, call_log.caller, when)
		queued += 1
	return queued


def after_lead_insert(doc, method=None):
	"""doc_event: CRM Lead after_insert."""
	try:
		rules = _rules({NEW_LEAD})
		if not rules:
			return
		cc = hm_settings.get().default_country_code
		phone = normalize_phone(doc.get("mobile_no"), cc) or normalize_phone(doc.get("phone"), cc)
		if not phone:
			return
		user = doc.get("lead_owner") or frappe.session.user
		for rule in rules:
			_queue(rule, "CRM Lead", doc.name, phone, user, now_datetime())
	except Exception:
		# Never stop a lead from being saved because of WhatsApp.
		frappe.log_error(title="Hipersign WhatsApp rule (new lead)")


def _queue(rule, party_doctype, party, phone, user, when):
	frappe.enqueue(
		"hipersign_mobile.services.whatsapp_rules.send",
		queue="short",
		enqueue_after_commit=True,
		rule=rule.name,
		party_doctype=party_doctype,
		party=party,
		phone=phone,
		user=user,
		when=str(when),
	)


# ---------------------------------------------------------------- sending


def label_for(rule_name: str) -> str:
	return f"Hipersign rule {rule_name}"


def already_sent(rule, party_doctype: str, party: str, phone: str | None = None) -> bool:
	"""Did this rule already message this customer (ever, or within repeat_after_hours)?

	Matched by the record AND by phone number, so a lead that later became a deal or
	customer is still the same person."""
	filters = {"label": label_for(rule.name), "type": "Outgoing"}
	if not cint(rule.only_once):
		hours = cint(rule.repeat_after_hours)
		if hours <= 0:
			return False
		filters["creation"] = (">=", add_to_date(now_datetime(), hours=-hours))
	same_party = frappe.get_all(
		"WhatsApp Message",
		filters={**filters, "reference_doctype": party_doctype, "reference_name": party},
		limit=1,
		pluck="name",
	)
	if same_party:
		return True
	if phone:
		digits = "".join(ch for ch in phone if ch.isdigit())
		if digits and frappe.get_all(
			"WhatsApp Message",
			filters={**filters, "to": ["in", [digits, "+" + digits]]},
			limit=1,
			pluck="name",
		):
			return True
	return False


def send(rule: str, party_doctype: str, party: str, phone: str, user: str | None = None, when=None):
	"""Background job: create the WhatsApp Message (Frappe WhatsApp sends it on insert)."""
	try:
		if not available():
			return None
		row = frappe.db.get_value(
			RULE_DOCTYPE,
			rule,
			[
				"name",
				"enabled",
				"trigger",
				TEMPLATE_FIELD,
				"parameters",
				"repeat_after_hours",
				"only_once",
				"header_image",
			],
			as_dict=True,
		)
		if not row or not cint(row.enabled) or not row.get(TEMPLATE_FIELD):
			return None
		if not cint(frappe.db.get_single_value("Hipersign Mobile Settings", "enable_whatsapp_rules")):
			return None
		if already_sent(row, party_doctype, party, phone):
			return None
		template = frappe.db.get_value(
			"WhatsApp Templates",
			row.get(TEMPLATE_FIELD),
			["name", "sample_values", "field_names"],
			as_dict=True,
		)
		if not template:
			return None
		values = build_parameters(row.parameters, template, party_doctype, party, phone, user, when)
		msg = {
			"doctype": "WhatsApp Message",
			"type": "Outgoing",
			"to": phone.lstrip("+"),
			"use_template": 1,
			"message_type": "Template",
			"template": template.name,
			"content_type": "text",
			"reference_doctype": party_doctype,
			"reference_name": party,
			"label": label_for(row.name),
		}
		if row.header_image:
			msg["attach"] = public_url(row.header_image)  # image header of the template
		if values is not None:
			msg["body_param"] = json.dumps({str(i + 1): v for i, v in enumerate(values)})
		doc = frappe.get_doc(msg)
		doc.insert(ignore_permissions=True)
		return doc.name
	except Exception:
		frappe.log_error(title=f"Hipersign WhatsApp rule {rule} -> {party_doctype} {party}")
		return None


def public_url(file_url: str) -> str:
	"""Meta downloads the header image itself, so it must be a public file."""
	if file_url.startswith("/private/"):
		name = frappe.db.get_value("File", {"file_url": file_url}, "name")
		if name:
			f = frappe.get_doc("File", name)
			f.is_private = 0
			f.save(ignore_permissions=True)
			return f.file_url
	return file_url


PLACEHOLDER = re.compile(r"\{(\w+)\}")


def template_param_count(template) -> int:
	"""How many {{n}} variables the template body has (Frappe WhatsApp stores samples per variable)."""
	samples = (template.get("sample_values") or "").strip()
	return len(samples.split(",")) if samples else 0


def build_parameters(spec, template, party_doctype, party, phone, user, when) -> list[str] | None:
	"""Values for the template's variables, in order. None = template has no variables."""
	count = template_param_count(template)
	if not count:
		return None
	parts = [p.strip() for p in (spec or "").split(",")] if (spec or "").strip() else []
	if not parts:
		parts = ["{name}"] * count
	# Meta rejects a message whose parameter count differs from the template's.
	parts = (parts + ["{name}"] * count)[:count]
	ctx = context(party_doctype, party, phone, user, when)
	out = []
	for p in parts:
		value = PLACEHOLDER.sub(lambda m: str(ctx.get(m.group(1).lower(), m.group(0))), p).strip()
		out.append(value or "-")
	return out


def context(party_doctype, party, phone, user, when) -> dict:
	summary = party_summary(party_doctype, party) or {}
	name = summary.get("name") or party
	first = frappe.db.get_value("CRM Lead", party, "first_name") if party_doctype == "CRM Lead" else None
	first = first or (name or "").split(" ")[0]
	when = get_datetime(when) if when else now_datetime()
	sp = (
		frappe.db.get_value("User", user, ["full_name", "mobile_no", "phone"], as_dict=True) if user else None
	)
	company = (
		user and frappe.defaults.get_user_default("Company", user)
	) or frappe.defaults.get_global_default("company")
	return {
		"name": name,
		"first_name": first,
		"salesperson": (sp.full_name if sp else None) or "",
		"salesperson_phone": (sp and (sp.mobile_no or sp.phone)) or "",
		"phone": phone,
		"company": company or "",
		"date": when.strftime("%d %b %Y"),
		"time": when.strftime("%I:%M %p").lstrip("0"),
	}
