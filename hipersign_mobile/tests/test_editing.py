from __future__ import annotations

import json
import uuid

import frappe

try:
	from frappe.tests import IntegrationTestCase as TestCase
except ImportError:  # Frappe v15
	from frappe.tests.utils import FrappeTestCase as TestCase

from hipersign_mobile.api import activities, customers
from hipersign_mobile.tests.utils import call, future, make_customer, make_lead, make_task, make_user


def _digits():
	return "".join(ch for ch in str(uuid.uuid4().int))[:5]


class TestPartyEditing(TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.sales = make_user("sales1")

	def tearDown(self):
		frappe.db.rollback()

	def _form(self, doctype, name):
		r = call(customers.edit_form, self.sales, doctype=doctype, name=name)
		self.assertTrue(r["success"], r)
		return {f["key"]: f for f in r["data"]["fields"]}

	def test_edit_lead(self):
		lead = make_lead(f"HM Edit {_digits()}", f"+91 97462 {_digits()}")
		f = self._form("CRM Lead", lead)
		self.assertIn("status", f)
		self.assertTrue(f["status"]["options"])
		lost = f["lost_reason"]["show_if"]["in"] if "lost_reason" in f else []
		status = next(o for o in f["status"]["options"] if o not in lost and o != "New")
		r = call(customers.update_party, self.sales, doctype="CRM Lead", name=lead, values=json.dumps({
			"first_name": "Renamed Biz", "mobile_no": "98470 12345", "status": status,
			"hm_address": "Near Bus Stand, Palakkad",
		}))
		self.assertTrue(r["success"], r)
		doc = frappe.get_doc("CRM Lead", lead)
		self.assertEqual(doc.first_name, "Renamed Biz")
		self.assertEqual(doc.mobile_no, "+919847012345")
		self.assertEqual(doc.status, status)
		self.assertEqual(r["data"]["location"]["address"], "Near Bus Stand, Palakkad")

	def test_lost_needs_reason(self):
		lead = make_lead(f"HM Edit L {_digits()}", f"+91 97462 {_digits()}")
		f = self._form("CRM Lead", lead)
		if "lost_reason" not in f:
			return
		lost = f["lost_reason"]["show_if"]["in"][0]
		r = call(customers.update_party, self.sales, doctype="CRM Lead", name=lead, values=json.dumps({"status": lost}))
		self.assertFalse(r["success"])
		self.assertIn("reason", r["error"]["message"].lower())

	def test_validation(self):
		lead = make_lead(f"HM Edit V {_digits()}", f"+91 97462 {_digits()}")
		bad = [
			{"first_name": ""},
			{"status": "Not a status"},
			{"mobile_no": "12"},
			{"email": "not-an-email"},
			{"owner": "x"},
		]
		for values in bad:
			r = call(customers.update_party, self.sales, doctype="CRM Lead", name=lead, values=json.dumps(values))
			self.assertFalse(r["success"], values)

	def test_edit_customer_contact_and_address(self):
		cust = make_customer(f"HM Edit Cust {_digits()}")
		if not frappe.db.exists("Address Template", {"is_default": 1}):
			frappe.get_doc({"doctype": "Address Template", "country": "India", "is_default": 1,
							"template": "{{ address_line1 }}"}).insert(ignore_permissions=True)
		r = call(customers.update_party, self.sales, doctype="Customer", name=cust, values=json.dumps({
			"contact_mobile": "98470 55555", "contact_email": "buyer@example.com",
			"address_line1": "12 Market Road", "city": "Ottapalam",
		}))
		self.assertTrue(r["success"], r)
		d = r["data"]
		self.assertIn("+919847055555", [p["e164"] for p in d["phones"]])
		self.assertEqual(d["email"], "buyer@example.com")
		self.assertTrue(d["location"]["address"].startswith("12 Market Road, Ottapalam"))
		f = self._form("Customer", cust)
		self.assertEqual(f["city"]["value"], "Ottapalam")

	def test_address_needs_city(self):
		cust = make_customer(f"HM Edit Cust2 {_digits()}")
		r = call(customers.update_party, self.sales, doctype="Customer", name=cust,
				 values=json.dumps({"address_line1": "Only line"}))
		self.assertFalse(r["success"])


class TestActivityEditing(TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.sales = make_user("sales1")
		cls.other = make_user("sales2")
		cls.customer = make_customer("HM Edit Activity Customer")

	def tearDown(self):
		frappe.db.rollback()

	def test_edit_details_and_time(self):
		task = make_task(self.sales, future(days=2, hour=10), ref=("Customer", self.customer)).name
		start = future(days=3, hour=15)
		r = call(activities.update_activity, self.sales, activity_id=task, title="New title",
				 activity_type="Meeting", priority="High", description="Bring samples",
				 start=start.isoformat(), end=(start.replace(hour=16)).isoformat())
		self.assertTrue(r["success"], r)
		a = r["data"]["activity"]
		self.assertEqual(a["title"], "New title")
		self.assertEqual(a["activity_type"], "Meeting")
		self.assertEqual(a["priority"], "High")
		self.assertTrue(r["data"]["changed"])
		doc = frappe.get_doc("CRM Task", task)
		self.assertEqual(doc.due_date.hour, 15)
		self.assertTrue(frappe.db.exists("Sales Activity History", {"crm_task": task, "event_type": "Rescheduled"}))

	def test_no_change_and_permissions(self):
		task = make_task(self.sales, future(days=2, hour=11), ref=("Customer", self.customer)).name
		title = frappe.db.get_value("CRM Task", task, "title")
		r = call(activities.update_activity, self.sales, activity_id=task, title=title)
		self.assertFalse(r["data"]["changed"])
		r = call(activities.update_activity, self.other, activity_id=task, title="Hijack")
		self.assertFalse(r["success"])
		r = call(activities.update_activity, self.sales, activity_id=task, priority="Urgent")
		self.assertFalse(r["success"])

	def test_closed_activity_cannot_be_edited(self):
		task = make_task(self.sales, future(days=2, hour=12), ref=("Customer", self.customer)).name
		frappe.db.set_value("CRM Task", task, "status", "Done")
		r = call(activities.update_activity, self.sales, activity_id=task, title="Late edit")
		self.assertFalse(r["success"])


class TestActivityDelete(TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.sales = make_user("sales1")
		cls.other = make_user("sales2")
		cls.customer = make_customer("HM Delete Activity Customer")

	def tearDown(self):
		frappe.db.rollback()

	def test_delete_own_activity(self):
		task = make_task(self.sales, future(days=2, hour=9), title="Wrong follow-up", ref=("Customer", self.customer)).name
		self.assertTrue(frappe.db.exists("Mobile Notification", {"reference_name": str(task), "status": "Queued"}))
		r = call(activities.delete_activity, self.sales, activity_id=task, reason="Duplicate")
		self.assertTrue(r["success"], r)
		self.assertFalse(frappe.db.exists("CRM Task", task))
		self.assertFalse(frappe.db.exists("Sales Activity History", {"crm_task": task}))
		self.assertFalse(frappe.db.exists("Mobile Notification", {"reference_name": str(task), "status": "Queued"}))
		self.assertTrue(frappe.db.exists("Comment", {"reference_name": self.customer, "content": ["like", "%Wrong follow-up%Duplicate%"]}))

	def test_cannot_delete_someone_elses(self):
		task = make_task(self.sales, future(days=2, hour=9), ref=("Customer", self.customer)).name
		r = call(activities.delete_activity, self.other, activity_id=task)
		self.assertFalse(r["success"])
		self.assertTrue(frappe.db.exists("CRM Task", task))
