"""Invoices and payments from the app - e.g. the customer pays during a meeting.

- An invoice is for an ERPNext Customer. For a CRM lead / deal the customer it became is used,
  or one is made for it (linked by Customer.hm_crm_lead / crm_deal, so it is made only once).
- It can be made from a quotation (same items, prices and taxes) or with new items.
- A payment is an ERPNext Payment Entry against the invoice, to the account set on the
  Mode of Payment for the company (Cash, UPI, Bank Transfer, ...).
"""

from __future__ import annotations

import frappe
from frappe.utils import flt, nowdate

from hipersign_mobile.api._common import choice, mobile_api, optional_str, require_str
from hipersign_mobile.api import quotations as qt
from hipersign_mobile.services import permissions as perms

PARTY = ("Customer", "CRM Lead", "CRM Deal")


def _require(user):
	perms.require("invoices", user)
	if not frappe.db.exists("DocType", "Sales Invoice"):
		raise frappe.ValidationError("Invoices need ERPNext on this site.")


def _modes(company: str) -> list[dict]:
	rows = frappe.get_all(
		"Mode of Payment Account",
		filters={"company": company, "default_account": ["is", "set"], "parenttype": "Mode of Payment"},
		fields=["parent"],
	)
	names = {r.parent for r in rows}
	modes = frappe.get_all(
		"Mode of Payment", filters={"name": ["in", list(names) or [""]], "enabled": 1}, fields=["name", "type"], order_by="name asc"
	)
	# Cash first, it's the usual one at a meeting.
	modes.sort(key=lambda m: (m.type != "Cash", m.name))
	return [{"name": m.name, "type": m.type} for m in modes]


@mobile_api(methods=("GET",))
def options():
	me = frappe.session.user
	_require(me)
	out = qt.company_options(me)
	for c in out["companies"]:
		c["modes"] = _modes(c["name"])
	return out


# ---------------------------------------------------------------- customer for a lead / deal

def customer_for(doctype: str, name: str) -> str:
	if doctype == "Customer":
		return name
	from hipersign_mobile.services import journey

	rel = journey.related(doctype, name)
	if rel["Customer"]:
		return sorted(rel["Customer"])[0]
	meta = frappe.get_meta("Customer")
	if doctype == "CRM Deal":
		d = frappe.db.get_value("CRM Deal", name, ["organization", "lead"], as_dict=True) or frappe._dict()
		lead = d.lead
		title = d.organization
	else:
		lead = name
		title = None
	l = frappe.db.get_value(
		"CRM Lead", lead, ["organization", "lead_name", "first_name", "last_name"], as_dict=True
	) if lead else None
	if l:
		title = title or l.organization or l.lead_name or " ".join(x for x in (l.first_name, l.last_name) if x)
	title = (title or name).strip()
	c = frappe.new_doc("Customer")
	c.customer_name = title
	c.customer_type = "Company"
	c.customer_group = (
		frappe.db.get_single_value("Selling Settings", "customer_group")
		or frappe.db.get_value("Customer Group", {"is_group": 0}, "name")
	)
	c.territory = (
		frappe.db.get_single_value("Selling Settings", "territory")
		or frappe.db.get_value("Territory", {"is_group": 0}, "name")
	)
	if doctype == "CRM Deal" and meta.has_field("crm_deal"):
		c.crm_deal = name
	if lead and meta.has_field("hm_crm_lead"):
		c.hm_crm_lead = lead
	c.flags.ignore_mandatory = True
	c.insert(ignore_permissions=True)
	if doctype == "CRM Deal" and frappe.get_meta("CRM Deal").has_field("erpnext_customer"):
		frappe.db.set_value("CRM Deal", name, "erpnext_customer", c.name, update_modified=False)
	return c.name


def _customer_of_quotation(q) -> str:
	if q.quotation_to == "Customer":
		return q.party_name
	if q.quotation_to == "CRM Deal":
		return customer_for("CRM Deal", q.party_name)
	if q.quotation_to == "Lead":
		crm = frappe.db.get_value("Lead", q.party_name, "hm_crm_lead") if frappe.get_meta("Lead").has_field("hm_crm_lead") else None
		if crm:
			return customer_for("CRM Lead", crm)
		existing = frappe.db.get_value("Customer", {"lead_name": q.party_name}, "name") if frappe.get_meta("Customer").has_field("lead_name") else None
		if existing:
			return existing
		from erpnext.crm.doctype.lead.lead import _make_customer

		c = _make_customer(q.party_name, ignore_permissions=True)
		c.flags.ignore_mandatory = True
		c.insert(ignore_permissions=True)
		return c.name
	raise frappe.ValidationError("This quotation's customer can't be invoiced from the app.")


# ---------------------------------------------------------------- read

def payments_of(name: str) -> list[dict]:
	refs = frappe.get_all(
		"Payment Entry Reference",
		filters={"reference_doctype": "Sales Invoice", "reference_name": name, "docstatus": 1},
		fields=["parent", "allocated_amount"],
	)
	out = []
	for r in refs:
		pe = frappe.db.get_value(
			"Payment Entry", r.parent, ["posting_date", "mode_of_payment", "reference_no", "paid_to_account_currency", "owner"], as_dict=True
		)
		out.append({
			"id": r.parent,
			"date": str(pe.posting_date),
			"mode": pe.mode_of_payment,
			"reference": pe.reference_no,
			"amount": flt(r.allocated_amount),
			"amount_text": frappe.utils.fmt_money(r.allocated_amount, currency=pe.paid_to_account_currency),
			"by": frappe.utils.get_fullname(pe.owner),
		})
	return sorted(out, key=lambda x: x["date"], reverse=True)


def serialize(inv, full: bool = False) -> dict:
	money = lambda v: frappe.utils.fmt_money(v, currency=inv.currency)  # noqa: E731
	out = {
		"id": inv.name,
		"status": "Draft" if inv.docstatus == 0 else inv.status,
		"submitted": inv.docstatus == 1,
		"party": inv.customer_name or inv.customer,
		"customer": inv.customer,
		"date": str(inv.posting_date),
		"due_date": str(inv.due_date) if inv.due_date else None,
		"company": inv.company,
		"currency": inv.currency,
		"total": flt(inv.grand_total),
		"total_text": money(inv.grand_total),
		"net_text": money(inv.net_total),
		"tax_text": money(inv.total_taxes_and_charges),
		"outstanding": flt(inv.outstanding_amount),
		"outstanding_text": money(inv.outstanding_amount),
		"by": frappe.utils.get_fullname(inv.owner),
	}
	if full:
		out["items"] = [
			{"code": i.item_code, "name": i.item_name, "qty": flt(i.qty), "rate": flt(i.rate), "uom": i.uom,
			 "amount_text": money(i.amount)}
			for i in inv.items
		]
		out["taxes"] = inv.taxes_and_charges
		out["payments"] = payments_of(inv.name) if inv.docstatus == 1 else []
		out["modes"] = _modes(inv.company)
	return out


def _get(name: str, me: str):
	name = require_str(name, "Invoice", 140)
	if not frappe.db.exists("Sales Invoice", name):
		raise frappe.DoesNotExistError("Invoice not found.")
	inv = frappe.get_doc("Sales Invoice", name)
	if "team_view" not in perms.capabilities(me) and inv.owner != me:
		perms.require_party("Customer", inv.customer, me)
	return inv


@mobile_api(methods=("GET",))
def get_invoice(invoice_id: str | None = None):
	me = frappe.session.user
	_require(me)
	return serialize(_get(invoice_id, me), full=True)


# ---------------------------------------------------------------- write

def _pay(inv, amount: float, mode: str, reference: str | None, me: str):
	"""Call inside qt._as_admin()."""
	from erpnext.accounts.doctype.payment_entry.payment_entry import get_payment_entry

	if inv.docstatus != 1:
		raise frappe.ValidationError("Submit the invoice before taking a payment.")
	outstanding = flt(frappe.db.get_value("Sales Invoice", inv.name, "outstanding_amount"))
	if amount <= 0:
		raise frappe.ValidationError("Enter the amount received.")
	if outstanding <= 0:
		raise frappe.ValidationError("This invoice is already paid.")
	if amount > outstanding + 0.01:
		raise frappe.ValidationError(f"Only {frappe.utils.fmt_money(outstanding, currency=inv.currency)} is due on this invoice.")
	mode = require_str(mode, "Payment mode", 140)
	account = frappe.db.get_value("Mode of Payment Account", {"parent": mode, "company": inv.company}, "default_account")
	if not account:
		raise frappe.ValidationError(f"In ERP, set the {inv.company} account on Mode of Payment '{mode}' first.")
	pe = get_payment_entry("Sales Invoice", inv.name, party_amount=amount, bank_account=account)
	pe.mode_of_payment = mode
	pe.reference_no = optional_str(reference, 140) or f"{mode} - {inv.name}"
	pe.reference_date = nowdate()
	pe.flags.ignore_permissions = True
	pe.insert()
	pe.submit()
	frappe.db.set_value("Payment Entry", pe.name, {"owner": me, "modified_by": me}, update_modified=False)
	return pe


@mobile_api(methods=("POST",))
def create(
	doctype: str | None = None,
	name: str | None = None,
	company: str | None = None,
	items=None,
	taxes: str | None = None,
	quotation_id: str | None = None,
	submit: str | int | bool | None = None,
	paid_amount: str | float | None = None,
	mode_of_payment: str | None = None,
	reference_no: str | None = None,
	remarks: str | None = None,
):
	"""New invoice: from a quotation (quotation_id) or for a customer / lead / deal with items
	[{code, qty, rate}]. With paid_amount (a number, or "full") the payment is recorded too
	(the invoice is then submitted)."""
	me = frappe.session.user
	_require(me)
	full = str(paid_amount or "").strip().lower() == "full"  # the whole invoice, taxes included
	paid = 1 if full else flt(paid_amount)
	do_submit = str(submit).lower() in ("1", "true", "yes") or paid > 0
	if quotation_id:
		perms.require("quotations", me)
		q = qt._get(quotation_id, me)
		if q.docstatus == 2:
			raise frappe.ValidationError("This quotation is cancelled.")
		rows = [{"code": i.item_code, "qty": i.qty, "rate": i.rate, "description": i.description} for i in q.items]
		company = q.company
		taxes = q.taxes_and_charges
		with qt._as_admin():
			customer = _customer_of_quotation(q)
	else:
		doctype = choice(doctype, PARTY, "doctype")
		name = require_str(name, "Customer", 140)
		perms.require_party(doctype, name, me)
		company = require_str(company, "Company", 140)
		if not frappe.db.exists("Company", company):
			raise frappe.ValidationError("Choose the company.")
		rows = qt.parse_lines(items)
		with qt._as_admin():
			customer = customer_for(doctype, name)
	inv = frappe.new_doc("Sales Invoice")
	inv.customer = customer
	inv.company = company
	inv.posting_date = nowdate()
	inv.set_posting_time = 1
	inv.due_date = nowdate()
	if remarks:
		inv.remarks = optional_str(remarks, 2000)
	qt.fill_lines(inv, company, rows, taxes)
	with qt._as_admin():
		qt.price_lines(inv, rows)
		if not inv.due_date or str(inv.due_date) < str(inv.posting_date):
			inv.due_date = inv.posting_date
		inv.flags.ignore_permissions = True
		inv.insert()
		if do_submit:
			inv.submit()
		frappe.db.set_value("Sales Invoice", inv.name, {"owner": me, "modified_by": me}, update_modified=False)
		inv.owner = me
		inv.add_comment("Info", f"Made in Hipersign by {frappe.utils.get_fullname(me)}"
						+ (f" from quotation {quotation_id}" if quotation_id else ""))
		if paid > 0:
			_pay(inv, flt(inv.grand_total) if full else paid, mode_of_payment, reference_no, me)
			inv.reload()
	return serialize(inv, full=True)


@mobile_api(methods=("POST",))
def submit(invoice_id: str | None = None):
	me = frappe.session.user
	_require(me)
	inv = _get(invoice_id, me)
	if inv.docstatus == 0:
		with qt._as_admin():
			inv.flags.ignore_permissions = True
			inv.submit()
	return serialize(inv, full=True)


@mobile_api(methods=("POST",))
def record_payment(
	invoice_id: str | None = None,
	amount: str | float | None = None,
	mode_of_payment: str | None = None,
	reference_no: str | None = None,
):
	me = frappe.session.user
	_require(me)
	inv = _get(invoice_id, me)
	with qt._as_admin():
		_pay(inv, flt(amount), mode_of_payment, reference_no, me)
	inv.reload()
	return serialize(inv, full=True)


def _pdf(inv) -> bytes:
	fmt = frappe.db.get_value("Property Setter", {"doc_type": "Sales Invoice", "property": "default_print_format"}, "value") or None
	return frappe.get_print("Sales Invoice", inv.name, print_format=fmt, as_pdf=True, letterhead=inv.letter_head or None)


@frappe.whitelist(methods=["GET"])
def pdf(invoice_id: str | None = None):
	perms.require_active_user()
	me = frappe.session.user
	_require(me)
	inv = _get(invoice_id, me)
	frappe.local.response.filename = f"{inv.name}.pdf"
	with qt._as_admin():
		frappe.local.response.filecontent = _pdf(inv)
	frappe.local.response.type = "download"
	frappe.local.response.display_content_as = "inline"
	frappe.local.response.content_type = "application/pdf"


@mobile_api(methods=("POST",))
def send_whatsapp(invoice_id: str | None = None, phone: str | None = None, caption: str | None = None):
	me = frappe.session.user
	_require(me)
	inv = _get(invoice_id, me)
	from hipersign_mobile.services import whatsapp_chat as wc

	wc.require_enabled()
	perms.require("whatsapp", me)
	t = wc.target(require_str(phone, "Phone", 40), wc.Viewer(me))
	with qt._as_admin():
		content = _pdf(inv)
	doc = wc.send_media(t, content, f"{inv.name}.pdf", "application/pdf",
		optional_str(caption, 1024) or f"Invoice {inv.name} - {frappe.utils.fmt_money(inv.grand_total, currency=inv.currency)}", None)
	return {"sent": True, "message": doc.name if doc else None}
