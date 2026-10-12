from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe

try:
	from frappe.tests import IntegrationTestCase as TestCase
except ImportError:  # Frappe v15
	from frappe.tests.utils import FrappeTestCase as TestCase

from hipersign_mobile.api import customers
from hipersign_mobile.services import google_places
from hipersign_mobile.tests.utils import call, make_customer, make_user

NUMBER = "+91 98950 77001"
E164 = "+919895077001"
PLACE = {
	"places": [
		{
			"id": "ChIJ-test-place",
			"displayName": {"text": "Malabar Solar Traders", "languageCode": "en"},
			"formattedAddress": "MG Road, Kunnamkulam, Kerala",
			"googleMapsUri": "https://maps.google.com/?cid=123",
		}
	]
}


def _resp(status=200, data=None):
	r = MagicMock()
	r.status_code = status
	r.json.return_value = data if data is not None else {}
	return r


def _set(**values):
	s = frappe.get_single("Hipersign Mobile Settings")
	s.update(values)
	s.save(ignore_permissions=True)
	frappe.db.commit()


def _cleanup():
	for name in frappe.get_all("CRM Lead", filters={"mobile_no": ["like", "%9895077%"]}, pluck="name"):
		frappe.delete_doc("CRM Lead", name, force=True, ignore_permissions=True)
	for n in ("+919895077001", "+919895077002"):
		frappe.cache.delete_value(f"hipersign_mobile:gplace:{n}")
	frappe.db.commit()


class TestGoogleLeads(TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.sales = make_user("sales1")
		make_customer("HM Known Customer", "+91 98950 77999")
		_set(enable_google_lookup=1, google_maps_api_key="test-key", google_lead_source="Google Maps")

	@classmethod
	def tearDownClass(cls):
		_set(enable_google_lookup=0)
		super().tearDownClass()

	def setUp(self):
		_cleanup()

	def test_unknown_number_found_on_google_once(self):
		with patch.object(google_places.requests, "post", return_value=_resp(data=PLACE)) as post:
			r = call(customers.lookup_number, self.sales, phone=NUMBER)
			self.assertTrue(r["success"], r)
			d = r["data"]
			self.assertEqual(d["phone"], E164)
			self.assertIsNone(d["party"])
			self.assertEqual(d["google"]["name"], "Malabar Solar Traders")
			body = post.call_args.kwargs["json"]
			self.assertEqual(body, {"textQuery": "+91 9895077001", "regionCode": "IN"})
			self.assertEqual(post.call_args.kwargs["headers"]["X-Goog-Api-Key"], "test-key")
			# Second dial of the same number is served from cache: no second paid lookup.
			call(customers.lookup_number, self.sales, phone="09895077001")
			self.assertEqual(post.call_count, 1)

	def test_known_number_never_asks_google(self):
		with patch.object(google_places.requests, "post") as post:
			r = call(customers.lookup_number, self.sales, phone="9895077999")
			self.assertEqual(r["data"]["party"]["id"], "HM Known Customer")
			post.assert_not_called()

	def test_google_error_is_reported_not_raised(self):
		with patch.object(google_places.requests, "post", return_value=_resp(403, {"error": {"message": "API not enabled"}})):
			r = call(customers.lookup_number, self.sales, phone="+919895077002")
			self.assertTrue(r["success"])
			self.assertIsNone(r["data"]["google"])
			self.assertIn("API not enabled", r["data"]["google_error"])

	def test_create_lead_from_google_and_no_duplicates(self):
		r = call(
			customers.create_lead,
			self.sales,
			phone=NUMBER,
			name="Malabar Solar Traders",
			address="MG Road, Kunnamkulam, Kerala",
			maps_url="https://maps.google.com/?cid=123",
			place_id="ChIJ-test-place",
		)
		self.assertTrue(r["success"], r)
		self.assertTrue(r["data"]["created"])
		lead = frappe.get_doc("CRM Lead", r["data"]["party"]["id"])
		self.assertEqual(lead.mobile_no, E164)
		self.assertEqual(lead.organization, "Malabar Solar Traders")
		self.assertEqual(lead.lead_owner, self.sales)
		self.assertEqual(lead.source, "Google Maps")
		self.assertEqual(lead.hm_google_place_id, "ChIJ-test-place")
		self.assertTrue(
			frappe.db.exists("Comment", {"reference_doctype": "CRM Lead", "reference_name": lead.name, "content": ["like", "%Kunnamkulam%"]})
		)
		frappe.db.commit()
		again = call(customers.create_lead, self.sales, phone="9895077001", name="Other name")
		self.assertFalse(again["data"]["created"])
		self.assertEqual(again["data"]["party"]["id"], lead.name)
		same_place = call(customers.create_lead, self.sales, phone="+919895077002", name="X", place_id="ChIJ-test-place")
		self.assertEqual(same_place["data"]["party"]["id"], lead.name)

	def test_typed_name_without_google(self):
		r = call(customers.create_lead, self.sales, phone="+919895077002", name="Walk-in Shop")
		self.assertTrue(r["data"]["created"])
		lead = frappe.get_doc("CRM Lead", r["data"]["party"]["id"])
		self.assertFalse(lead.source)
		self.assertEqual(call(customers.create_lead, self.sales, phone="12", name="X")["_http_status"], 422)

	def test_switched_off(self):
		_set(enable_google_lookup=0)
		try:
			with patch.object(google_places.requests, "post") as post:
				r = call(customers.lookup_number, self.sales, phone=NUMBER)
				self.assertFalse(r["data"]["google_enabled"])
				self.assertIsNone(r["data"]["google"])
				post.assert_not_called()
		finally:
			_set(enable_google_lookup=1)


class TestDealsInCustomerList(TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.sales = make_user("sales1")
		name = frappe.db.get_value("CRM Deal", {"lead_name": "HM Deal Person"}, "name")
		if not name:
			contact = frappe.get_doc(
				{
					"doctype": "Contact",
					"first_name": "HM Deal Person",
					"phone_nos": [{"phone": "+91 97450 33221", "is_primary_mobile_no": 1}],
				}
			).insert(ignore_permissions=True)
			name = (
				frappe.get_doc(
					{
						"doctype": "CRM Deal",
						"lead_name": "HM Deal Person",
						"contacts": [{"contact": contact.name, "is_primary": 1}],
					}
				)
				.insert(ignore_permissions=True)
				.name
			)
			frappe.db.commit()
		cls.deal = name

	def test_deals_filter_and_search_by_contact_phone(self):
		r = call(customers.search, self.sales, kind="deal", query="HM Deal Person")
		self.assertTrue(r["success"], r)
		self.assertEqual([(i["id"], i["kind"]) for i in r["data"]["items"]], [(self.deal, "Deal")])
		by_phone = call(customers.search, self.sales, kind="all", query="97450 33221")
		self.assertIn(self.deal, [i["id"] for i in by_phone["data"]["items"]])
		detail = call(customers.get_detail, self.sales, doctype="CRM Deal", name=self.deal)
		self.assertTrue(detail["success"], detail)

	def test_number_of_a_deal_is_not_a_new_lead(self):
		r = call(customers.lookup_number, self.sales, phone="9745033221")
		self.assertEqual(r["data"]["party"]["id"], self.deal)
		c = call(customers.create_lead, self.sales, phone="9745033221", name="Dup")
		self.assertFalse(c["data"]["created"])
