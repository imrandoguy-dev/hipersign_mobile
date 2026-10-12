"""Customer / CRM Lead / CRM Deal summaries and their callable phone numbers."""

from __future__ import annotations

import frappe

from hipersign_mobile.services import settings as hm_settings
from hipersign_mobile.services.phone import contact_payload


def _cache() -> dict:
	if not hasattr(frappe.local, "hm_party_cache"):
		frappe.local.hm_party_cache = {}
	return frappe.local.hm_party_cache


def _dedupe_phones(items: list[dict | None]) -> list[dict]:
	seen, out = set(), []
	for item in items:
		if not item:
			continue
		key = item["e164"] or item["raw"]
		if key in seen:
			continue
		seen.add(key)
		out.append(item)
	# Valid numbers first so the app's one-tap buttons use a working number.
	out.sort(key=lambda p: (not p["valid"],))
	return out


def _contact_phones(contact_names: list[str], cc: str) -> list[dict]:
	if not contact_names:
		return []
	rows = frappe.get_all(
		"Contact Phone",
		filters={"parent": ["in", contact_names], "parenttype": "Contact"},
		fields=["phone", "is_primary_mobile_no", "is_primary_phone"],
		order_by="is_primary_mobile_no desc, is_primary_phone desc, idx asc",
	)
	return [contact_payload(r.phone, cc, "Contact") for r in rows]


def _linked_contacts(doctype: str, name: str) -> list[str]:
	return frappe.get_all(
		"Dynamic Link",
		filters={"link_doctype": doctype, "link_name": name, "parenttype": "Contact"},
		pluck="parent",
		limit=10,
	)


def _first_email(contact_names: list[str]) -> str | None:
	if not contact_names:
		return None
	return frappe.db.get_value(
		"Contact Email", {"parent": ["in", contact_names], "parenttype": "Contact"}, "email_id"
	)


def party_summary(
	doctype: str | None, name: str | None, cc: str | None = None, full: bool = False
) -> dict | None:
	if not doctype or not name:
		return None
	key = (doctype, name, full)
	cache = _cache()
	if key in cache:
		return cache[key]
	cc = cc or hm_settings.get().default_country_code
	out = None
	if doctype == "Customer":
		out = _customer(name, cc, full)
	elif doctype == "CRM Lead":
		out = _lead(name, cc, full)
	elif doctype == "CRM Deal":
		out = _deal(name, cc, full)
	cache[key] = out
	return out


def _customer(name, cc, full):
	c = frappe.db.get_value(
		"Customer",
		name,
		[
			"name",
			"customer_name",
			"mobile_no",
			"email_id",
			"customer_primary_contact",
			"customer_group",
			"territory",
			"disabled",
		],
		as_dict=True,
	)
	if not c:
		return None
	contacts = _linked_contacts("Customer", name)
	if c.customer_primary_contact and c.customer_primary_contact not in contacts:
		contacts.insert(0, c.customer_primary_contact)
	phones = _dedupe_phones([contact_payload(c.mobile_no, cc, "Mobile"), *_contact_phones(contacts, cc)])
	out = {
		"kind": "Customer",
		"doctype": "Customer",
		"id": c.name,
		"name": c.customer_name or c.name,
		"subtitle": c.customer_group,
		"email": c.email_id or _first_email(contacts),
		"phones": phones,
		"primary_phone": phones[0] if phones else None,
	}
	if full:
		out.update(
			{"territory": c.territory, "disabled": bool(c.disabled), "customer_group": c.customer_group}
		)
	return out


def _lead(name, cc, full):
	lead = frappe.db.get_value(
		"CRM Lead",
		name,
		[
			"name",
			"lead_name",
			"first_name",
			"last_name",
			"organization",
			"mobile_no",
			"phone",
			"email",
			"status",
			"lead_owner",
		],
		as_dict=True,
	)
	if not lead:
		return None
	display = lead.lead_name or " ".join(p for p in (lead.first_name, lead.last_name) if p) or lead.name
	phones = _dedupe_phones(
		[contact_payload(lead.mobile_no, cc, "Mobile"), contact_payload(lead.phone, cc, "Phone")]
	)
	out = {
		"kind": "Lead",
		"doctype": "CRM Lead",
		"id": lead.name,
		"name": display,
		"subtitle": lead.organization,
		"email": lead.email,
		"status": lead.status,
		"phones": phones,
		"primary_phone": phones[0] if phones else None,
	}
	if full:
		out.update({"organization": lead.organization, "owner": lead.lead_owner})
	return out


def _deal(name, cc, full):
	d = frappe.db.get_value(
		"CRM Deal",
		name,
		[
			"name",
			"organization",
			"lead_name",
			"first_name",
			"last_name",
			"mobile_no",
			"phone",
			"email",
			"status",
			"deal_owner",
			"contact",
		],
		as_dict=True,
	)
	if not d:
		return None
	person = d.lead_name or " ".join(p for p in (d.first_name, d.last_name) if p)
	contacts = [d.contact] if d.contact else []
	phones = _dedupe_phones(
		[
			contact_payload(d.mobile_no, cc, "Mobile"),
			contact_payload(d.phone, cc, "Phone"),
			*_contact_phones(contacts, cc),
		]
	)
	out = {
		"kind": "Deal",
		"doctype": "CRM Deal",
		"id": d.name,
		"name": d.organization or person or d.name,
		"subtitle": person if d.organization else None,
		"email": d.email,
		"status": d.status,
		"phones": phones,
		"primary_phone": phones[0] if phones else None,
	}
	if full:
		out.update({"owner": d.deal_owner})
	return out
