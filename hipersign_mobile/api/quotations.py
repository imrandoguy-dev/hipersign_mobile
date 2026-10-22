"""Quotations from the app: pick items, quantities and prices for a customer / lead / deal,
save it in ERPNext, open the PDF or send it on WhatsApp.

- Customer -> Quotation to Customer; CRM Deal -> Quotation to CRM Deal (ERPNext supports it);
  CRM Lead -> its deal when it has one, else an ERPNext Lead made for it (linked by hm_crm_lead).
- Company, price list and taxes default to what the person used last time.
"""

from __future__ import annotations

import frappe
from frappe.utils import add_days, cint, flt, getdate, nowdate

from hipersign_mobile.api._common import choice, mobile_api, optional_str, require_str
from hipersign_mobile.services import permissions as perms
from hipersign_mobile.services import timeutil

PARTY = ("Customer", "CRM Lead", "CRM Deal")


def _require(user):
	perms.require("quotations", user)
	if not frappe.db.exists("DocType", "Quotation"):
		raise frappe.ValidationError("Quotations need ERPNext on this site.")


class _as_admin:
	"""Run ERPNext's own logic as Administrator (the caller was already checked)."""

	def __enter__(self):
		self.user = frappe.session.user
		frappe.set_user("Administrator")

	def __exit__(self, *exc):
		frappe.set_user(self.user)
		return False


def _price_list_for(company: str) -> str | None:
	currency = frappe.get_cached_value("Company", company, "default_currency")
	default = frappe.db.get_single_value("Selling Settings", "selling_price_list")
	lists = frappe.get_all("Price List", filters={"selling": 1, "enabled": 1}, fields=["name", "currency"])
	if default and any(p.name == default and p.currency == currency for p in lists):
		return default
	same = [p.name for p in lists if p.currency == currency]
	return same[0] if same else default


def _last(user: str) -> frappe._dict:
	return frappe.db.get_value(
		"Quotation", {"owner": user}, ["company", "taxes_and_charges", "selling_price_list"], as_dict=True,
		order_by="creation desc",
	) or frappe._dict()


@mobile_api(methods=("GET",))
def options():
	me = frappe.session.user
	_require(me)
	last = _last(me)
	companies = frappe.get_all("Company", fields=["name", "default_currency"], order_by="name asc")
	default = last.company or frappe.defaults.get_user_default("Company") or frappe.defaults.get_global_default("company")
	if default not in [c.name for c in companies]:
		default = companies[0].name if companies else None
	out = []
	for c in companies:
		taxes = frappe.get_all(
			"Sales Taxes and Charges Template", filters={"company": c.name, "disabled": 0}, fields=["name", "is_default"],
			order_by="is_default desc, name asc",
		)
		out.append({
			"name": c.name,
			"currency": c.default_currency,
			"price_list": _price_list_for(c.name),
			"taxes": [t.name for t in taxes],
			"default_tax": last.taxes_and_charges if last.company == c.name and last.taxes_and_charges else None,
		})
	return {"companies": out, "default_company": default, "valid_days": 30}


@mobile_api(methods=("GET",))
def items(query: str | None = None, company: str | None = None):
	"""Sales items by code or name, with the price from the company's price list."""
	me = frappe.session.user
	_require(me)
	q = (query or "").strip()
	filters = {"disabled": 0, "is_sales_item": 1, "has_variants": 0}
	or_filters = [["item_code", "like", f"%{q}%"], ["item_name", "like", f"%{q}%"]] if q else None
	rows = frappe.get_all(
		"Item", filters=filters, or_filters=or_filters,
		fields=["item_code", "item_name", "stock_uom", "standard_rate", "item_group", "description"],
		order_by="modified desc", limit=40,
	)
	price_list = _price_list_for(company) if company and frappe.db.exists("Company", company) else None
	prices = {}
	if price_list and rows:
		for p in frappe.get_all(
			"Item Price", filters={"price_list": price_list, "item_code": ["in", [r.item_code for r in rows]]},
			fields=["item_code", "price_list_rate"],
		):
			prices[p.item_code] = p.price_list_rate
	from hipersign_mobile.api.notes import html_to_text

	return {
		"price_list": price_list,
		"items": [
			{
				"code": r.item_code,
				"name": r.item_name or r.item_code,
				"uom": r.stock_uom,
				"group": r.item_group,
				"rate": flt(prices.get(r.item_code, r.standard_rate)),
				"description": (html_to_text(r.description) or "")[:300] if r.description and r.description != r.item_name else None,
			}
			for r in rows
		],
	}


def erp_lead_for(crm_lead: str) -> str:
	"""ERPNext Lead for a CRM lead (made once, reused)."""
	existing = frappe.db.get_value("Lead", {"hm_crm_lead": crm_lead}, "name") if frappe.get_meta("Lead").has_field("hm_crm_lead") else None
	if existing:
		return existing
	l = frappe.db.get_value(
		"CRM Lead", crm_lead, ["lead_name", "first_name", "organization", "mobile_no", "phone", "email", "lead_owner"], as_dict=True
	)
	doc = frappe.new_doc("Lead")
	doc.first_name = l.first_name or l.lead_name or l.organization or crm_lead
	doc.lead_name = l.lead_name or l.organization or doc.first_name
	doc.company_name = l.organization
	doc.mobile_no = l.mobile_no
	doc.phone = l.phone
	doc.email_id = l.email
	if doc.meta.has_field("lead_owner") and l.lead_owner:
		doc.lead_owner = l.lead_owner
	if doc.meta.has_field("hm_crm_lead"):
		doc.hm_crm_lead = crm_lead
	doc.flags.ignore_mandatory = True
	doc.insert(ignore_permissions=True)
	return doc.name


def _quotation_party(doctype: str, name: str) -> tuple[str, str]:
	if doctype == "Customer":
		return "Customer", name
	if doctype == "CRM Deal":
		return "CRM Deal", name
	deal = frappe.db.get_value("CRM Deal", {"lead": name}, "name", order_by="creation desc")
	if deal:
		return "CRM Deal", deal
	return "Lead", erp_lead_for(name)


def serialize(q, full: bool = False) -> dict:
	out = {
		"id": q.name,
		"status": q.status,
		"submitted": q.docstatus == 1,
		"party": q.customer_name or q.party_name,
		"date": str(q.transaction_date),
		"valid_till": str(q.valid_till) if q.valid_till else None,
		"company": q.company,
		"currency": q.currency,
		"total": flt(q.grand_total),
		"total_text": frappe.utils.fmt_money(q.grand_total, currency=q.currency),
		"net_text": frappe.utils.fmt_money(q.net_total, currency=q.currency),
		"tax_text": frappe.utils.fmt_money(q.total_taxes_and_charges, currency=q.currency),
		"by": frappe.utils.get_fullname(q.owner),
	}
	if full:
		out["items"] = [
			{"code": i.item_code, "name": i.item_name, "qty": flt(i.qty), "rate": flt(i.rate), "uom": i.uom,
			 "amount_text": frappe.utils.fmt_money(i.amount, currency=q.currency)}
			for i in q.items
		]
		out["taxes"] = q.taxes_and_charges
		out["terms"] = q.terms
	return out


def _get(name: str, me: str):
	name = require_str(name, "Quotation", 140)
	if not frappe.db.exists("Quotation", name):
		raise frappe.DoesNotExistError("Quotation not found.")
	q = frappe.get_doc("Quotation", name)
	if "team_view" not in perms.capabilities(me) and q.owner != me:
		# Salespeople: their own, or ones for a customer / lead / deal they can see.
		ref = {"Customer": "Customer", "CRM Deal": "CRM Deal"}.get(q.quotation_to)
		if ref:
			perms.require_party(ref, q.party_name, me)
		elif q.quotation_to == "Lead" and frappe.get_meta("Lead").has_field("hm_crm_lead"):
			crm = frappe.db.get_value("Lead", q.party_name, "hm_crm_lead")
			if not crm:
				raise frappe.DoesNotExistError("Quotation not found.")
			perms.require_party("CRM Lead", crm, me)
		else:
			raise frappe.DoesNotExistError("Quotation not found.")
	return q


@mobile_api(methods=("GET",))
def list_for(doctype: str | None = None, name: str | None = None):
	me = frappe.session.user
	_require(me)
	doctype = choice(doctype, PARTY, "doctype")
	name = require_str(name, "Customer", 140)
	perms.require_party(doctype, name, me)
	from hipersign_mobile.services import journey

	rel = journey.related(doctype, name)
	parties = list(rel["Customer"] | rel["CRM Deal"] | journey._erp_leads(rel))
	rows = frappe.get_all(
		"Quotation", filters={"party_name": ["in", parties or [""]], "docstatus": ["<", 2]}, pluck="name",
		order_by="creation desc", limit=30,
	)
	return {"items": [serialize(frappe.get_doc("Quotation", n)) for n in rows]}


@mobile_api(methods=("GET",))
def get_quotation(quotation_id: str | None = None):
	me = frappe.session.user
	_require(me)
	return serialize(_get(quotation_id, me), full=True)


@mobile_api(methods=("POST",))
def create(
	doctype: str | None = None,
	name: str | None = None,
	company: str | None = None,
	items=None,
	taxes: str | None = None,
	valid_days: int | str | None = None,
	terms: str | None = None,
	submit: str | int | bool | None = None,
):
	"""items: [{code, qty, rate, description?}] (JSON list)."""
	me = frappe.session.user
	_require(me)
	doctype = choice(doctype, PARTY, "doctype")
	name = require_str(name, "Customer", 140)
	perms.require_party(doctype, name, me)
	company = require_str(company, "Company", 140)
	if not frappe.db.exists("Company", company):
		raise frappe.ValidationError("Choose the company.")
	if isinstance(items, str):
		items = frappe.parse_json(items)
	rows = [r for r in (items or []) if r and (r.get("code") or r.get("item_code"))]
	if not rows:
		raise frappe.ValidationError("Add at least one item.")
	to, party = _quotation_party(doctype, name)
	q = frappe.new_doc("Quotation")
	q.quotation_to = to
	q.party_name = party
	q.company = company
	q.transaction_date = nowdate()
	q.valid_till = add_days(nowdate(), max(cint(valid_days) or 30, 1))
	q.order_type = "Sales"
	q.currency = frappe.get_cached_value("Company", company, "default_currency")
	q.conversion_rate = 1
	pl = _price_list_for(company)
	if pl:
		q.selling_price_list = pl
		q.price_list_currency = frappe.db.get_value("Price List", pl, "currency") or q.currency
		q.plc_conversion_rate = 1 if q.price_list_currency == q.currency else None
	for r in rows:
		code = r.get("code") or r.get("item_code")
		if not frappe.db.exists("Item", code):
			raise frappe.ValidationError(f"Item {code} not found.")
		qty = flt(r.get("qty")) or 1
		if qty <= 0:
			raise frappe.ValidationError("Quantity must be more than 0.")
		rate = flt(r.get("rate"))
		if rate < 0:
			raise frappe.ValidationError("Price can't be negative.")
		row = {"item_code": code, "qty": qty, "rate": rate, "price_list_rate": rate}
		desc = optional_str(r.get("description"), 2000)
		if desc:
			row["description"] = desc
		q.append("items", row)
	if terms:
		q.terms = optional_str(terms, 10000)
	if taxes:
		if not frappe.db.exists("Sales Taxes and Charges Template", {"name": taxes, "company": company}):
			raise frappe.ValidationError("This tax template is not for the chosen company.")
		q.taxes_and_charges = taxes
		from erpnext.controllers.accounts_controller import _get_taxes_and_charges

		for t in _get_taxes_and_charges("Sales Taxes and Charges Template", taxes) or []:
			q.append("taxes", t)
	# ERPNext fills addresses, taxes and totals with the user's desk permissions; salespeople
	# may not have those, so this part runs as Administrator and the quotation is theirs after.
	with _as_admin():
		q.run_method("set_missing_values")
		for i in q.items:  # keep the price typed in the app (set_missing_values may load list prices)
			r = next((x for x in rows if (x.get("code") or x.get("item_code")) == i.item_code), None)
			if r is not None:
				i.rate = flt(r.get("rate"))
				i.price_list_rate = i.rate
				i.discount_percentage = 0
				i.discount_amount = 0
		q.calculate_taxes_and_totals()
		q.flags.ignore_permissions = True
		q.insert()
		if str(submit).lower() in ("1", "true", "yes"):
			q.submit()
	frappe.db.set_value("Quotation", q.name, {"owner": me, "modified_by": me}, update_modified=False)
	q.owner = me
	q.add_comment("Info", f"Made in Hipersign by {frappe.utils.get_fullname(me)}")
	return serialize(q, full=True)


@mobile_api(methods=("POST",))
def submit(quotation_id: str | None = None):
	me = frappe.session.user
	_require(me)
	q = _get(quotation_id, me)
	if q.docstatus == 0:
		with _as_admin():
			q.flags.ignore_permissions = True
			q.submit()
	return serialize(q, full=True)


def _pdf(q) -> bytes:
	fmt = frappe.db.get_value("Property Setter", {"doc_type": "Quotation", "property": "default_print_format"}, "value") or None
	return frappe.get_print("Quotation", q.name, print_format=fmt, as_pdf=True, letterhead=q.letter_head or None)


@frappe.whitelist(methods=["GET"])
def pdf(quotation_id: str | None = None):
	perms.require_active_user()
	me = frappe.session.user
	_require(me)
	q = _get(quotation_id, me)
	frappe.local.response.filename = f"{q.name}.pdf"
	with _as_admin():
		frappe.local.response.filecontent = _pdf(q)
	frappe.local.response.type = "download"
	frappe.local.response.display_content_as = "inline"
	frappe.local.response.content_type = "application/pdf"


@mobile_api(methods=("POST",))
def send_whatsapp(quotation_id: str | None = None, phone: str | None = None, caption: str | None = None):
	"""Sends the quotation PDF to the customer's WhatsApp (Frappe WhatsApp)."""
	me = frappe.session.user
	_require(me)
	q = _get(quotation_id, me)
	from hipersign_mobile.services import whatsapp_chat as wc

	wc.require_enabled()
	perms.require("whatsapp", me)
	t = wc.target(require_str(phone, "Phone", 40), wc.Viewer(me))
	with _as_admin():
		content = _pdf(q)
	doc = wc.send_media(t, content, f"{q.name}.pdf", "application/pdf",
		optional_str(caption, 1024) or f"Quotation {q.name} - {frappe.utils.fmt_money(q.grand_total, currency=q.currency)}", None)
	return {"sent": True, "message": doc.name if doc else None}
