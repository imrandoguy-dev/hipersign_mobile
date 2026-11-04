"""Invoices and payments from the app."""

from __future__ import annotations

import frappe

try:
	from frappe.tests import IntegrationTestCase as TestCase
except ImportError:  # Frappe v15
	from frappe.tests.utils import FrappeTestCase as TestCase

from hipersign_mobile.api import attachments, customers, invoices, quotations
from hipersign_mobile.tests.utils import call, ensure_company, ensure_item, make_customer, make_lead, make_user


def ensure_cash(company: str) -> str:
	frappe.set_user("Administrator")
	if not frappe.db.exists("Mode of Payment", "Cash"):
		frappe.get_doc({"doctype": "Mode of Payment", "mode_of_payment": "Cash", "type": "Cash", "enabled": 1}).insert(ignore_permissions=True)
	mop = frappe.get_doc("Mode of Payment", "Cash")
	if not any(a.company == company for a in mop.accounts):
		acc = frappe.db.get_value("Account", {"company": company, "account_type": "Cash", "is_group": 0}, "name")
		mop.append("accounts", {"company": company, "default_account": acc})
		mop.save(ignore_permissions=True)
	for field, kind in (("default_receivable_account", "Receivable"), ("default_income_account", "Income Account")):
		if not frappe.db.get_value("Company", company, field):
			acc = frappe.db.get_value("Account", {"company": company, "account_type": kind, "is_group": 0}, "name")
			if acc:
				frappe.db.set_value("Company", company, field, acc)
	if not frappe.db.get_value("Company", company, "cost_center"):
		cc = frappe.db.get_value("Cost Center", {"company": company, "is_group": 0}, "name")
		frappe.db.set_value("Company", company, "cost_center", cc)
	for doctype, key, extra in (
		("Customer Group", "Commercial", {"customer_group_name": "Commercial", "is_group": 0}),
		("Territory", "India", {"territory_name": "India", "is_group": 0}),
	):
		if not frappe.db.exists(doctype, key) and not frappe.db.get_value(doctype, {"is_group": 0}):
			frappe.get_doc({"doctype": doctype, **extra}).insert(ignore_permissions=True)
	frappe.db.commit()
	return "Cash"


class TestInvoices(TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.sales = make_user("sales1")
		cls.company = ensure_company()
		cls.item = ensure_item("HM ERP Pack", 15000)
		cls.mode = ensure_cash(cls.company)
		cls.customer = make_customer("HM Invoice Customer")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def test_options_list_payment_modes(self):
		r = call(invoices.options, self.sales)
		self.assertTrue(r["success"], r)
		co = next(c for c in r["data"]["companies"] if c["name"] == self.company)
		self.assertIn("Cash", [m["name"] for m in co["modes"]])

	def test_invoice_with_payment_at_the_meeting(self):
		r = call(invoices.create, self.sales, doctype="Customer", name=self.customer, company=self.company,
				 items=[{"code": self.item, "qty": 2, "rate": 10000}], paid_amount=5000, mode_of_payment="Cash")
		self.assertTrue(r["success"], r)
		inv = r["data"]
		self.assertTrue(inv["submitted"])
		self.assertEqual(inv["total"], 20000)
		self.assertEqual(inv["outstanding"], 15000)
		self.assertEqual(len(inv["payments"]), 1)
		self.assertEqual(frappe.db.get_value("Sales Invoice", inv["id"], "owner"), self.sales)
		# Rest paid later.
		r = call(invoices.record_payment, self.sales, invoice_id=inv["id"], amount=15000, mode_of_payment="Cash")
		self.assertTrue(r["success"], r)
		self.assertEqual(r["data"]["outstanding"], 0)
		self.assertEqual(r["data"]["status"], "Paid")
		# Can't take more than is due.
		r = call(invoices.record_payment, self.sales, invoice_id=inv["id"], amount=100, mode_of_payment="Cash")
		self.assertFalse(r["success"])

	def test_invoice_from_quotation_for_a_lead(self):
		lead = make_lead("HM Inv Lead", mobile="+919876500091", owner=self.sales)
		q = call(quotations.create, self.sales, doctype="CRM Lead", name=lead, company=self.company,
				 items=[{"code": self.item, "qty": 1, "rate": 9000}], submit=1)
		self.assertTrue(q["success"], q)
		r = call(invoices.create, self.sales, quotation_id=q["data"]["id"], submit=1)
		self.assertTrue(r["success"], r)
		self.assertEqual(r["data"]["total"], 9000)
		customer = r["data"]["customer"]
		self.assertEqual(frappe.db.get_value("Customer", customer, "hm_crm_lead"), lead)
		# A second invoice for the lead uses the same customer, and the lead page lists both.
		r2 = call(invoices.create, self.sales, doctype="CRM Lead", name=lead, company=self.company,
				  items=[{"code": self.item, "qty": 1, "rate": 100}])
		self.assertTrue(r2["success"], r2)
		self.assertEqual(r2["data"]["customer"], customer)
		self.assertFalse(r2["data"]["submitted"])
		d = call(customers.get_detail, self.sales, doctype="CRM Lead", name=lead)["data"]
		self.assertEqual({x["id"] for x in d["journey"]["invoices"]}, {r["data"]["id"], r2["data"]["id"]})

	def test_voice_note_on_customer(self):
		r = call(attachments.list_files, self.sales, doctype="Customer", name=self.customer)
		self.assertTrue(r["success"], r)
		r = call(attachments.list_files, make_user("trainer"), doctype="Customer", name=self.customer)
		self.assertFalse(r["success"])
