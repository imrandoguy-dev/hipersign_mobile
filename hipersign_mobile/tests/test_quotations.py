"""Quotations from the app and the customer stage."""

from __future__ import annotations

import datetime as dt

import frappe

try:
	from frappe.tests import IntegrationTestCase as TestCase
except ImportError:  # Frappe v15
	from frappe.tests.utils import FrappeTestCase as TestCase

from hipersign_mobile.api import customers, development, quotations
from hipersign_mobile.services import timeutil
from hipersign_mobile.tests.utils import call, ensure_company, ensure_item, make_customer, make_lead, make_user


class TestQuotations(TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.sales = make_user("sales1")
		cls.manager = make_user("manager")
		cls.company = ensure_company()
		cls.item = ensure_item("HM ERP Pack", 15000)
		cls.customer = make_customer("HM Quote Customer")
		cls.lead = make_lead("HM Quote Lead", mobile="+919876500011", owner=cls.sales)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def test_options_items_and_create_for_customer(self):
		r = call(quotations.options, self.sales)
		self.assertTrue(r["success"], r)
		self.assertIn(self.company, [c["name"] for c in r["data"]["companies"]])
		r = call(quotations.items, self.sales, query="ERP Pack", company=self.company)
		self.assertEqual(r["data"]["items"][0]["code"], self.item)
		self.assertEqual(r["data"]["items"][0]["rate"], 15000)
		r = call(quotations.create, self.sales, doctype="Customer", name=self.customer, company=self.company,
				 items=[{"code": self.item, "qty": 2, "rate": 12000}])
		self.assertTrue(r["success"], r)
		q = r["data"]
		self.assertEqual(q["total"], 24000)
		self.assertEqual(frappe.db.get_value("Quotation", q["id"], "party_name"), self.customer)
		frappe.db.commit()
		# On the customer: listed, and the stage says "Quotation sent".
		r = call(quotations.list_for, self.sales, doctype="Customer", name=self.customer)
		self.assertIn(q["id"], [x["id"] for x in r["data"]["items"]])
		d = call(customers.get_detail, self.sales, doctype="Customer", name=self.customer)["data"]
		self.assertEqual(d["journey"]["stage"]["label"], "Quotation sent")
		# Development work for the customer moves the stage on.
		r = call(development.create_task, self.manager, kind="team", title="Set up their ERP", developer=self.manager,
				 deadline=(timeutil.now_system() + dt.timedelta(days=3)).replace(tzinfo=timeutil.system_tz()).isoformat(),
				 customer=self.customer)
		self.assertTrue(r["success"], r)
		frappe.db.commit()
		d = call(customers.get_detail, self.sales, doctype="Customer", name=self.customer)["data"]
		self.assertEqual(d["journey"]["stage"]["label"], "In development")
		frappe.db.delete("Team Task", {"title": "Set up their ERP"})
		frappe.db.commit()

	def test_quotation_for_a_crm_lead(self):
		r = call(quotations.create, self.sales, doctype="CRM Lead", name=self.lead, company=self.company,
				 items=[{"code": self.item, "qty": 1, "rate": 15000}])
		self.assertTrue(r["success"], r)
		q = frappe.get_doc("Quotation", r["data"]["id"])
		self.assertEqual(q.quotation_to, "Lead")
		self.assertEqual(frappe.db.get_value("Lead", q.party_name, "hm_crm_lead"), self.lead)
		frappe.db.commit()
		# The same ERPNext lead is reused next time.
		r2 = call(quotations.create, self.sales, doctype="CRM Lead", name=self.lead, company=self.company,
				  items=[{"code": self.item, "qty": 1, "rate": 14000}])
		self.assertEqual(frappe.db.get_value("Quotation", r2["data"]["id"], "party_name"), q.party_name)
		self.assertTrue(call(quotations.get_quotation, self.sales, quotation_id=q.name)["success"])
		self.assertFalse(call(quotations.create, self.sales, doctype="CRM Lead", name=self.lead, company=self.company, items=[])["success"])
