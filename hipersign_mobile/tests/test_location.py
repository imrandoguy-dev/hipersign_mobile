from __future__ import annotations

import uuid

import frappe

try:
	from frappe.tests import IntegrationTestCase as TestCase
except ImportError:  # Frappe v15
	from frappe.tests.utils import FrappeTestCase as TestCase

from hipersign_mobile.api import customers
from hipersign_mobile.install import backfill_lead_addresses
from hipersign_mobile.tests.utils import call, make_customer, make_user

MAPS = "https://maps.google.com/?cid=123"


class TestLocation(TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.sales = make_user("sales1")

	def _num(self):
		digits = "".join(ch for ch in str(uuid.uuid4().int) if ch.isdigit())[:5]
		return f"+91 97461 {digits}"

	def test_google_lead_has_address_and_maps_link(self):
		r = call(customers.create_lead, self.sales, phone=self._num(), name="HM Loc Biz",
				 address="MG Road, Palakkad, Kerala", maps_url=MAPS, place_id=f"pl-{uuid.uuid4().hex[:8]}")
		self.assertTrue(r["success"], r)
		lead = r["data"]["party"]["id"]
		d = call(customers.get_detail, self.sales, doctype="CRM Lead", name=lead)["data"]
		self.assertEqual(d["location"]["address"], "MG Road, Palakkad, Kerala")
		self.assertEqual(d["location"]["maps_url"], MAPS)
		self.assertTrue(d["location"]["place_id"].startswith("pl-"))
		frappe.db.rollback()

	def test_lead_without_location(self):
		r = call(customers.create_lead, self.sales, phone=self._num(), name="HM Loc Plain")
		d = call(customers.get_detail, self.sales, doctype="CRM Lead", name=r["data"]["party"]["id"])["data"]
		self.assertIsNone(d["location"])
		frappe.db.rollback()

	def test_deal_uses_its_lead_address(self):
		r = call(customers.create_lead, self.sales, phone=self._num(), name="HM Loc Deal Biz",
				 address="Kunnamkulam, Thrissur", maps_url=MAPS)
		lead = r["data"]["party"]["id"]
		deal = frappe.get_doc({"doctype": "CRM Deal", "lead": lead, "organization": None}).insert(
			ignore_permissions=True, ignore_mandatory=True
		)
		d = call(customers.get_detail, self.sales, doctype="CRM Deal", name=deal.name)["data"]
		self.assertEqual(d["location"]["address"], "Kunnamkulam, Thrissur")
		self.assertEqual(d["location"]["maps_url"], MAPS)
		frappe.db.rollback()

	def test_customer_address(self):
		cust = make_customer("HM Loc Customer", "+91 97461 77001")
		if not frappe.db.exists("Address Template", {"is_default": 1}):
			frappe.get_doc({"doctype": "Address Template", "country": "India", "is_default": 1,
							"template": "{{ address_line1 }}<br>{{ city }}"}).insert(ignore_permissions=True)
		if not frappe.db.exists("Dynamic Link", {"link_doctype": "Customer", "link_name": cust, "parenttype": "Address"}):
			frappe.get_doc({
				"doctype": "Address", "address_title": "HM Loc", "address_type": "Billing",
				"address_line1": "12 Market Street", "city": "Palakkad", "state": "Kerala", "country": "India",
				"is_primary_address": 1, "links": [{"link_doctype": "Customer", "link_name": cust}],
			}).insert(ignore_permissions=True)
		d = call(customers.get_detail, self.sales, doctype="Customer", name=cust)["data"]
		self.assertEqual(d["location"]["address"], "12 Market Street, Palakkad, Kerala, India")
		self.assertIsNone(d["location"]["maps_url"])
		frappe.db.rollback()

	def test_backfill_from_old_comment(self):
		r = call(customers.create_lead, self.sales, phone=self._num(), name="HM Loc Old")
		lead = r["data"]["party"]["id"]
		frappe.get_doc({
			"doctype": "Comment", "comment_type": "Info", "reference_doctype": "CRM Lead", "reference_name": lead,
			"content": 'Found on Google Maps: Near Bus Stand, Ottapalam &amp; Co - <a href="x" target="_blank">Open in Google Maps</a>',
		}).insert(ignore_permissions=True)
		backfill_lead_addresses()
		self.assertEqual(frappe.db.get_value("CRM Lead", lead, "hm_address"), "Near Bus Stand, Ottapalam & Co")
		frappe.db.rollback()
