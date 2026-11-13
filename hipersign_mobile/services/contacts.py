"""Contact persons of a customer / lead / deal (one company, several people and numbers).

They are ERPNext / Frappe CRM Contact records linked to the party (Dynamic Link), plus the
person saved on the lead / deal itself. A follow-up or meeting can name one of them
(CRM Task.hm_contact), so Call / WhatsApp go to that person.
"""

from __future__ import annotations

import frappe

from hipersign_mobile.services import settings as hm_settings
from hipersign_mobile.services.phone import contact_payload


def _linked(doctype: str, name: str) -> list[str]:
	names = frappe.get_all(
		"Dynamic Link",
		filters={"link_doctype": doctype, "link_name": name, "parenttype": "Contact"},
		pluck="parent",
		limit=50,
	)
	if doctype == "CRM Deal":
		d = frappe.db.get_value("CRM Deal", name, "contact")
		if d:
			names.insert(0, d)
		if frappe.db.table_exists("CRM Contacts"):
			names += frappe.get_all(
				"CRM Contacts", filters={"parent": name, "parenttype": "CRM Deal"}, pluck="contact", limit=50
			)
	if doctype == "Customer":
		p = frappe.db.get_value("Customer", name, "customer_primary_contact")
		if p:
			names.insert(0, p)
	out = []
	for n in names:
		if n and n not in out:
			out.append(n)
	return out


def _contact(name: str, cc: str) -> dict | None:
	c = frappe.db.get_value(
		"Contact", name, ["name", "first_name", "last_name", "full_name", "designation", "mobile_no", "phone", "email_id"],
		as_dict=True,
	)
	if not c:
		return None
	phones = frappe.get_all(
		"Contact Phone",
		filters={"parent": name, "parenttype": "Contact"},
		fields=["phone"],
		order_by="is_primary_mobile_no desc, is_primary_phone desc, idx asc",
		pluck="phone",
	)
	raw = c.mobile_no or c.phone or (phones[0] if phones else None)
	return {
		"id": c.name,
		"name": c.full_name or " ".join(x for x in (c.first_name, c.last_name) if x) or c.name,
		"designation": c.designation,
		"email": c.email_id,
		"phone": contact_payload(raw, cc, "Mobile") if raw else None,
	}


def party_contacts(doctype: str, name: str) -> list[dict]:
	cc = hm_settings.get().default_country_code
	out = []
	if doctype in ("CRM Lead", "CRM Deal"):
		r = frappe.db.get_value(
			doctype, name, ["lead_name", "first_name", "last_name", "mobile_no", "phone", "email"], as_dict=True
		)
		if r:
			person = r.lead_name or " ".join(x for x in (r.first_name, r.last_name) if x)
			raw = r.mobile_no or r.phone
			if person or raw:
				out.append({
					"id": None,  # the person saved on the lead / deal itself
					"name": person or "Main contact",
					"designation": "Main contact",
					"email": r.email,
					"phone": contact_payload(raw, cc, "Mobile") if raw else None,
				})
	for n in _linked(doctype, name):
		c = _contact(n, cc)
		if c:
			out.append(c)
	return out


def contact_ids(doctype: str, name: str) -> set[str]:
	return {c["id"] for c in party_contacts(doctype, name) if c["id"]}


def add_contact(doctype: str, name: str, full_name: str, phone: str | None, designation: str | None, email: str | None) -> dict:
	parts = full_name.strip().split(" ", 1)
	c = frappe.new_doc("Contact")
	c.first_name = parts[0][:140]
	if len(parts) > 1:
		c.last_name = parts[1][:140]
	if designation:
		c.designation = designation[:140]
	if phone:
		c.append("phone_nos", {"phone": phone, "is_primary_mobile_no": 1})
	if email:
		c.append("email_ids", {"email_id": email, "is_primary": 1})
	c.append("links", {"link_doctype": doctype, "link_name": name})
	c.insert(ignore_permissions=True)
	if doctype == "CRM Deal" and frappe.get_meta("CRM Deal").has_field("contacts"):
		deal = frappe.get_doc("CRM Deal", name)
		deal.append("contacts", {"contact": c.name})
		deal.flags.ignore_permissions = True
		deal.flags.ignore_mandatory = True
		deal.save()
	cc = hm_settings.get().default_country_code
	return _contact(c.name, cc)


def summary_of(contact: str | None) -> dict | None:
	if not contact:
		return None
	return _contact(contact, hm_settings.get().default_country_code)
