"""Edit a customer, lead or deal from the app.

The server decides which fields can be edited (and their choices), so the app shows one generic
form: [{key, label, type, value, options, required}], type = text | phone | email | select |
multiline | number | date. Only fields that exist on this site are offered.

Phone / email of a customer or deal live on its Contact; a customer's address on its Address.
"""

from __future__ import annotations

import frappe
from frappe.utils import flt, getdate

from hipersign_mobile.services import settings as hm_settings
from hipersign_mobile.services.phone import normalize_phone

MAX_OPTIONS = 300


def _options(doctype: str, order_by: str = "name asc", filters=None) -> list[str]:
	if not frappe.db.exists("DocType", doctype):
		return []
	return frappe.get_all(doctype, filters=filters or {}, pluck="name", order_by=order_by, limit=MAX_OPTIONS)


def _status_options(doctype: str) -> list[str]:
	meta = frappe.get_meta(doctype)
	order = "position asc" if meta.has_field("position") else "name asc"
	return _options(doctype, order)


def _field(key, label, type_="text", value=None, options=None, required=False, show_if=None):
	f = {"key": key, "label": label, "type": type_, "value": value, "required": required}
	if options is not None:
		f["options"] = options
	if show_if:
		f["show_if"] = show_if  # {"key": other field, "in": [values]} - shown only then
	return f


def _lost_fields(status_doctype: str, doc) -> list:
	"""Frappe CRM asks why a lead / deal was lost: shown when a Lost status is chosen."""
	if not doc.meta.has_field("lost_reason"):
		return []
	lost = (
		frappe.get_all(status_doctype, filters={"type": "Lost"}, pluck="name")
		if frappe.get_meta(status_doctype).has_field("type")
		else []
	)
	if not lost:
		return []
	cond = {"key": "status", "in": lost}
	out = [
		_field(
			"lost_reason",
			"Reason lost",
			"select",
			doc.get("lost_reason"),
			_options("CRM Lost Reason"),
			show_if=cond,
		)
	]
	if doc.meta.has_field("lost_notes"):
		out.append(_field("lost_notes", "Lost notes", "multiline", doc.get("lost_notes"), show_if=cond))
	return out


# ------------------------------------------------------------------ contact / address helpers


def _primary_contact_for_deal(deal) -> str | None:
	if deal.get("contact"):
		return deal.contact
	for row in deal.get("contacts") or []:
		if row.get("is_primary"):
			return row.contact
	rows = deal.get("contacts") or []
	return rows[0].contact if rows else None


def _contact_values(contact: str | None) -> dict:
	if not contact or not frappe.db.exists("Contact", contact):
		return {"first_name": None, "mobile": None, "email": None}
	c = frappe.get_doc("Contact", contact)
	mobile = c.mobile_no or c.phone
	if not mobile:
		for p in c.get("phone_nos") or []:
			mobile = p.phone
			break
	email = c.email_id
	if not email:
		for e in c.get("email_ids") or []:
			email = e.email_id
			break
	return {"first_name": c.first_name, "mobile": mobile, "email": email}


def _save_contact(contact: str | None, *, first_name=None, mobile=None, email=None, link=None) -> str | None:
	"""Update (or create) a Contact with this name / primary mobile / primary email."""
	if not any(v is not None for v in (first_name, mobile, email)):
		return contact
	if contact and frappe.db.exists("Contact", contact):
		c = frappe.get_doc("Contact", contact)
	else:
		if not (first_name or mobile or email):
			return contact
		c = frappe.new_doc("Contact")
		c.first_name = first_name or (link[1] if link else "Contact")
		if link:
			c.append("links", {"link_doctype": link[0], "link_name": link[1]})
	if first_name:
		c.first_name = first_name
	if mobile is not None:
		rows = c.get("phone_nos") or []
		primary = next((r for r in rows if r.is_primary_mobile_no), None) or (rows[0] if rows else None)
		if mobile:
			if primary:
				primary.phone = mobile
				primary.is_primary_mobile_no = 1
			else:
				c.append("phone_nos", {"phone": mobile, "is_primary_mobile_no": 1})
		elif primary:
			c.remove(primary)
	if email is not None:
		rows = c.get("email_ids") or []
		primary = next((r for r in rows if r.is_primary), None) or (rows[0] if rows else None)
		if email:
			if primary:
				primary.email_id = email
				primary.is_primary = 1
			else:
				c.append("email_ids", {"email_id": email, "is_primary": 1})
		elif primary:
			c.remove(primary)
	c.flags.ignore_mandatory = True
	c.save(ignore_permissions=True) if not c.is_new() else c.insert(ignore_permissions=True)
	return c.name


def _customer_address(customer: str) -> str | None:
	rows = frappe.db.sql(
		"""select a.name from `tabAddress` a join `tabDynamic Link` dl
			on dl.parent = a.name and dl.parenttype = 'Address'
		where dl.link_doctype = 'Customer' and dl.link_name = %s and ifnull(a.disabled, 0) = 0
		order by a.is_primary_address desc, a.modified desc limit 1""",
		customer,
	)
	return rows[0][0] if rows else None


def _default_country() -> str | None:
	return frappe.db.get_single_value("System Settings", "country") or (
		"India" if frappe.db.exists("Country", "India") else None
	)


# ------------------------------------------------------------------ form


def form(doctype: str, name: str) -> list[dict]:
	doc = frappe.get_doc(doctype, name)
	meta = frappe.get_meta(doctype)
	has = meta.has_field
	out: list[dict] = []
	if doctype == "CRM Lead":
		out += [
			_field("first_name", "First name / Business", value=doc.first_name, required=True),
			_field("last_name", "Last name", value=doc.last_name),
			_field("organization", "Organization", value=doc.organization),
			_field("job_title", "Job title", value=doc.get("job_title")) if has("job_title") else None,
			_field("mobile_no", "Mobile", "phone", doc.mobile_no),
			_field("phone", "Other phone", "phone", doc.phone) if has("phone") else None,
			_field("email", "Email", "email", doc.email),
			_field("website", "Website", value=doc.get("website")) if has("website") else None,
			_field("status", "Status", "select", doc.status, _status_options("CRM Lead Status")),
			*_lost_fields("CRM Lead Status", doc),
			_field(
				"lead_temperature",
				"Temperature",
				"select",
				doc.get("lead_temperature"),
				["Cold", "Warm", "Hot"],
			)
			if has("lead_temperature")
			else None,
			_field("source", "Source", "select", doc.get("source"), _options("CRM Lead Source")),
			_field("territory", "Territory", "select", doc.get("territory"), _options("CRM Territory"))
			if has("territory")
			else None,
			_field("hm_address", "Address", "multiline", doc.get("hm_address"))
			if has("hm_address")
			else None,
		]
	elif doctype == "CRM Deal":
		contact = _contact_values(_primary_contact_for_deal(doc))
		out += [
			_field("status", "Status", "select", doc.status, _status_options("CRM Deal Status")),
			*_lost_fields("CRM Deal Status", doc),
			_field("deal_value", "Deal value", "number", doc.get("deal_value"))
			if has("deal_value")
			else None,
			_field("probability", "Probability (%)", "number", doc.get("probability"))
			if has("probability")
			else None,
			_field(
				"expected_closure_date",
				"Expected closing date",
				"date",
				_date(doc.get("expected_closure_date")),
			)
			if has("expected_closure_date")
			else None,
			_field("next_step", "Next step", value=doc.get("next_step")) if has("next_step") else None,
			_field("contact_name", "Contact person", value=contact["first_name"]),
			_field("contact_mobile", "Contact mobile", "phone", contact["mobile"]),
			_field("contact_email", "Contact email", "email", contact["email"]),
			_field("source", "Source", "select", doc.get("source"), _options("CRM Lead Source"))
			if has("source")
			else None,
			_field("territory", "Territory", "select", doc.get("territory"), _options("CRM Territory"))
			if has("territory")
			else None,
			_field("hm_address", "Address", "multiline", doc.get("hm_address"))
			if has("hm_address")
			else None,
		]
	elif doctype == "Customer":
		contact = _contact_values(doc.get("customer_primary_contact"))
		addr = _customer_address(name)
		a = (
			frappe.db.get_value(
				"Address", addr, ["address_line1", "address_line2", "city", "state", "pincode"], as_dict=True
			)
			if addr
			else {}
		)
		a = a or {}
		out += [
			_field("customer_name", "Customer name", value=doc.customer_name, required=True),
			_field("contact_name", "Contact person", value=contact["first_name"]),
			_field("contact_mobile", "Mobile", "phone", contact["mobile"] or doc.get("mobile_no")),
			_field("contact_email", "Email", "email", contact["email"] or doc.get("email_id")),
			_field(
				"customer_group",
				"Customer group",
				"select",
				doc.customer_group,
				_options("Customer Group", filters={"is_group": 0}),
			),
			_field(
				"territory",
				"Territory",
				"select",
				doc.territory,
				_options("Territory", filters={"is_group": 0}),
			),
			_field("address_line1", "Address", value=a.get("address_line1")),
			_field("address_line2", "Address line 2", value=a.get("address_line2")),
			_field("city", "City / Town", value=a.get("city")),
			_field("state", "State", value=a.get("state")),
			_field("pincode", "PIN code", value=a.get("pincode")),
		]
	return [f for f in out if f]


def _date(v):
	return str(getdate(v)) if v else None


# ------------------------------------------------------------------ save


def save(doctype: str, name: str, values: dict) -> bool:
	fields = {f["key"]: f for f in form(doctype, name)}
	unknown = [k for k in values if k not in fields]
	if unknown:
		raise frappe.ValidationError(f"These fields can't be edited here: {', '.join(unknown)}")
	clean = {k: _clean(fields[k], v) for k, v in values.items()}
	for k, v in clean.items():
		if fields[k]["required"] and not v:
			raise frappe.ValidationError(f"{fields[k]['label']} is required.")

	doc = frappe.get_doc(doctype, name)
	contact_keys = {"contact_name", "contact_mobile", "contact_email"}
	address_keys = {"address_line1", "address_line2", "city", "state", "pincode"}
	changed = False
	for k, v in clean.items():
		if k in contact_keys or k in address_keys:
			continue
		if (doc.get(k) or None) != (v if v not in ("", None) else None):
			doc.set(k, v)
			changed = True

	if contact_keys & clean.keys():

		def pick(k):
			return clean[k] if k in clean else None

		if doctype == "CRM Deal":
			current = _primary_contact_for_deal(doc)
			new = _save_contact(
				current,
				first_name=pick("contact_name"),
				mobile=pick("contact_mobile"),
				email=pick("contact_email"),
			)
			if new and new != current:
				doc.append("contacts", {"contact": new, "is_primary": 1})
				if doc.meta.has_field("contact"):
					doc.contact = new
			if "contact_mobile" in clean and doc.meta.has_field("mobile_no"):
				doc.mobile_no = clean["contact_mobile"]
			if "contact_email" in clean and doc.meta.has_field("email"):
				doc.email = clean["contact_email"]
		else:  # Customer
			current = doc.get("customer_primary_contact")
			new = _save_contact(
				current,
				first_name=pick("contact_name"),
				mobile=pick("contact_mobile"),
				email=pick("contact_email"),
				link=("Customer", name),
			)
			if new and new != current:
				doc.customer_primary_contact = new
			if "contact_mobile" in clean:
				doc.mobile_no = clean["contact_mobile"]
			if "contact_email" in clean:
				doc.email_id = clean["contact_email"]
		changed = True

	if doctype == "Customer" and address_keys & clean.keys():
		_save_address(name, doc.customer_name, {k: clean[k] for k in address_keys if k in clean})
		changed = True

	if changed:
		doc.flags.ignore_mandatory = True
		doc.save(ignore_permissions=True)
	return changed


def _save_address(customer: str, title: str, vals: dict):
	addr = _customer_address(customer)
	if addr:
		a = frappe.get_doc("Address", addr)
	else:
		if not any(vals.values()):
			return
		a = frappe.new_doc("Address")
		a.address_title = title
		a.address_type = "Billing"
		a.country = _default_country()
		a.is_primary_address = 1
		a.append("links", {"link_doctype": "Customer", "link_name": customer})
	for k, v in vals.items():
		a.set(k, v)
	if not a.address_line1:
		raise frappe.ValidationError("Address is required to save the address.")
	if not a.city:
		raise frappe.ValidationError("City / Town is required to save the address.")
	a.save(ignore_permissions=True) if not a.is_new() else a.insert(ignore_permissions=True)


def _clean(field: dict, value):
	t = field["type"]
	if value is None:
		return None
	if t == "number":
		if value == "":
			return None
		return flt(value)
	if t == "date":
		return str(getdate(value)) if value else None
	text = str(value).strip()
	if t == "select":
		if text and text not in (field.get("options") or []):
			raise frappe.ValidationError(f"{field['label']}: choose one of the listed values.")
		return text or None
	if t == "phone":
		if not text:
			return None
		e164 = normalize_phone(text, hm_settings.get().default_country_code)
		if not e164:
			raise frappe.ValidationError(f"{field['label']}: enter a valid phone number.")
		return e164
	if t == "email":
		if text and not frappe.utils.validate_email_address(text):
			raise frappe.ValidationError(f"{field['label']}: enter a valid email address.")
		return text or None
	limit = 2000 if t == "multiline" else 140
	return text[:limit] or None
