"""Contact persons, trial notes, and new numbers from Google Maps."""

from __future__ import annotations

import datetime as dt
import json
import uuid
from unittest.mock import patch

import frappe

try:
	from frappe.tests import IntegrationTestCase as TestCase
except ImportError:  # Frappe v15
	from frappe.tests.utils import FrappeTestCase as TestCase

from hipersign_mobile.api import activities, calls, customers, prospects, trials
from hipersign_mobile.services import prospects as psvc
from hipersign_mobile.tests.utils import call, future, make_customer, make_lead, make_slot, make_user


def _n():
	return str(uuid.uuid4().int)[:5]


def _place(name, phone):
	return {"id": f"gp-{name}", "displayName": {"text": name}, "formattedAddress": f"{name} road, Kochi",
			"googleMapsUri": f"https://maps.google.com/?cid={name}", "internationalPhoneNumber": phone}


class TestContactsAndProspects(TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.sales = make_user("sales1")
		cls.sales2 = make_user("sales2")
		cls.manager = make_user("manager")
		cls.caller = make_user("caller")
		cls.trainer = make_user("trainer")
		if not frappe.db.exists("CRM Lead Source", "Google Maps"):
			frappe.get_doc({"doctype": "CRM Lead Source", "source_name": "Google Maps"}).insert(ignore_permissions=True)
		s = frappe.get_single("Hipersign Mobile Settings")
		s.update({"google_maps_api_key": "test-key", "google_lead_source": "Google Maps"})
		s.save(ignore_permissions=True)
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	# ------------------------------------------------------------ contact persons

	def test_add_contact_person_and_use_it_on_a_follow_up(self):
		lead = make_lead(f"HM Contacts {_n()}", f"+91 97461 {_n()}", owner=self.sales)
		r = call(customers.add_contact, self.sales, doctype="CRM Lead", name=lead, full_name="Anu Accounts",
				 phone=f"+91 97462 {_n()}", designation="Accountant")
		self.assertTrue(r["success"], r)
		cid = r["data"]["id"]
		people = call(customers.contacts, self.sales, doctype="CRM Lead", name=lead)["data"]["items"]
		self.assertEqual([p["id"] for p in people], [None, cid])  # main contact first
		# The new number is callable from the lead too.
		d = call(customers.get_detail, self.sales, doctype="CRM Lead", name=lead)["data"]
		self.assertIn(r["data"]["phone"]["e164"], [p["e164"] for p in d["phones"]])
		start = future(2, 11)
		a = call(activities.create_activity, self.sales, title="Call Anu", activity_type="Follow-up",
				 reference_doctype="CRM Lead", reference_name=lead, start=start.isoformat(),
				 end=(start + dt.timedelta(minutes=15)).isoformat(), contact=cid, idempotency_key=f"c{_n()}{_n()}")
		self.assertTrue(a["success"], a)
		self.assertEqual(a["data"]["activity"]["contact"]["name"], "Anu Accounts")
		# Clearing it goes back to the main contact.
		u = call(activities.update_activity, self.sales, activity_id=a["data"]["activity"]["id"], contact="")
		self.assertTrue(u["success"], u)
		self.assertIsNone(u["data"]["activity"].get("contact"))

	def test_contact_of_another_customer_is_refused(self):
		lead = make_lead(f"HM C1 {_n()}", f"+91 97463 {_n()}", owner=self.sales)
		other = make_lead(f"HM C2 {_n()}", f"+91 97464 {_n()}", owner=self.sales)
		cid = call(customers.add_contact, self.sales, doctype="CRM Lead", name=other, full_name="Not Here",
				   phone=f"+91 97465 {_n()}")["data"]["id"]
		start = future(2, 12)
		a = call(activities.create_activity, self.sales, title="x", activity_type="Follow-up",
				 reference_doctype="CRM Lead", reference_name=lead, start=start.isoformat(),
				 end=(start + dt.timedelta(minutes=15)).isoformat(), contact=cid, idempotency_key=f"c{_n()}{_n()}")
		self.assertFalse(a["success"])

	# ------------------------------------------------------------ trial notes

	def test_trial_note_saved_on_trial_and_customer(self):
		cust = make_customer(f"HM Trial Notes {_n()}", f"+91 97466 {_n()}")
		slot = make_slot(self.trainer, future(5, hour=10))
		b = call(trials.book_trial, self.sales, trainer_slot=slot, customer=cust, booking_type="ERP Trial",
				 idempotency_key=f"t{_n()}{_n()}")
		self.assertTrue(b["success"], b)
		bid = b["data"]["booking"]["id"]
		r = call(trials.add_note, self.sales, booking_id=bid, text="Wants payroll too")
		self.assertTrue(r["success"], r)
		self.assertIn("Wants payroll too", r["data"]["booking"]["notes"])
		r = call(trials.add_note, self.trainer, booking_id=bid, text="Day 1 done")
		self.assertTrue(r["success"], r)
		self.assertEqual(len(r["data"]["booking"]["notes"].splitlines()), 2)
		self.assertEqual(frappe.db.count("FCRM Note", {"reference_doctype": "Customer", "reference_docname": cust}), 2)

	# ------------------------------------------------------------ new numbers

	def _find(self, places, **kw):
		with patch.object(psvc, "_search", side_effect=[(places, None)] * 5):
			return call(prospects.find, self.manager, business="solar companies", places="Kochi",
						count=kw.get("count", 10), callers=json.dumps(kw.get("callers", [self.caller, self.sales2])))

	def test_find_skips_known_numbers_and_shares_out(self):
		known = f"+91 97467 {_n()}"
		make_customer(f"HM Known {_n()}", known)
		a, b, c = (f"+91 97468 {_n()}" for _ in range(3))
		r = self._find([_place("Sun A", a), _place("Known", known), _place("Sun B", b), _place("No phone", None), _place("Sun C", c)])
		self.assertTrue(r["success"], r)
		self.assertEqual((r["data"]["found"], r["data"]["skipped_known"], r["data"]["no_phone"]), (3, 1, 1))
		self.assertEqual(sorted(x["count"] for x in r["data"]["per_caller"]), [1, 2])
		mine = call(prospects.get_list, self.caller)["data"]
		self.assertEqual(len(mine["items"]), 2)
		self.assertEqual(mine["counts"]["to_call"], 2)
		# Searching again doesn't give the same numbers twice.
		again = self._find([_place("Sun A", a)])
		self.assertEqual(again["data"]["found"], 0)
		# A salesperson can't run searches.
		self.assertFalse(call(prospects.find, self.sales, business="x", places="y", count=1, callers=self.sales)["success"])

	def test_call_over_5_seconds_makes_a_lead_shorter_stays(self):
		long_n, short_n = f"+91 97469 {_n()}", f"+91 97460 {_n()}"
		self._find([_place("Long Talk", long_n), _place("Short Ring", short_n)], callers=[self.caller])
		when = (frappe.utils.now_datetime() - dt.timedelta(minutes=5)).isoformat()
		r = call(calls.sync, self.caller, calls=json.dumps([
			{"key": f"k{_n()}", "number": long_n, "started_at": when, "duration": 40, "direction": "outgoing", "via_app": True},
			{"key": f"k{_n()}", "number": short_n, "started_at": when, "duration": 3, "direction": "outgoing", "via_app": False},
		]))
		self.assertTrue(r["success"], r)
		self.assertEqual(r["data"]["saved"], 2)  # the new number counts as work even from the phone dialer
		items = {p["name"]: p for p in call(prospects.get_list, self.caller, tab="converted")["data"]["items"]}
		self.assertIn("Long Talk", items)
		lead = items["Long Talk"]["lead"]
		self.assertEqual(frappe.db.get_value("CRM Lead", lead, "lead_owner"), self.caller)
		self.assertEqual(frappe.db.get_value("Mobile Call Log", {"phone": psvc.normalize_phone(long_n)}, "party"), lead)
		todo = {p["name"]: p for p in call(prospects.get_list, self.caller)["data"]["items"]}
		self.assertEqual(todo["Short Ring"]["status"], "Not Connected")
		self.assertEqual(todo["Short Ring"]["attempts"], 1)

	def test_convert_by_hand_and_not_interested(self):
		x, y = f"+91 97470 {_n()}", f"+91 97471 {_n()}"
		self._find([_place("Hand Convert", x), _place("No Thanks", y)], callers=[self.caller])
		todo = {p["name"]: p for p in call(prospects.get_list, self.caller)["data"]["items"]}
		r = call(prospects.convert, self.caller, prospect_id=todo["Hand Convert"]["id"])
		self.assertTrue(r["success"], r)
		self.assertEqual(r["data"]["party"]["doctype"], "CRM Lead")
		r = call(prospects.set_status, self.caller, prospect_id=todo["No Thanks"]["id"], status="Not Interested", notes="Has ERP")
		self.assertTrue(r["success"], r)
		self.assertEqual(call(prospects.get_list, self.caller)["data"]["counts"]["to_call"], 0)
		# Someone else's number can't be touched.
		self.assertFalse(call(prospects.convert, self.sales2, prospect_id=todo["No Thanks"]["id"])["success"])

	# ------------------------------------------------------------ lead -> deal

	def test_convert_lead_to_deal(self):
		lead = make_lead(f"HM Convert {_n()}", f"+91 97472 {_n()}", owner=self.sales)
		start = future(2, 15)
		a = call(activities.create_activity, self.sales, title="Demo", activity_type="Follow-up",
				 reference_doctype="CRM Lead", reference_name=lead, start=start.isoformat(),
				 end=(start + dt.timedelta(minutes=15)).isoformat(), idempotency_key=f"c{_n()}{_n()}")
		self.assertTrue(a["success"], a)
		r = call(customers.convert_to_deal, self.sales, name=lead)
		self.assertTrue(r["success"], r)
		deal = r["data"]["deal"]
		self.assertEqual(frappe.db.get_value("CRM Deal", deal, "lead"), lead)
		self.assertTrue(frappe.db.get_value("CRM Lead", lead, "converted"))
		self.assertEqual(r["data"]["party"]["doctype"], "CRM Deal")
		# The follow-up is on the deal now, and shows on its page.
		task = a["data"]["activity"]["id"]
		self.assertEqual(frappe.db.get_value("CRM Task", task, ["reference_doctype", "reference_docname"]), ("CRM Deal", deal))
		d = call(customers.get_detail, self.sales, doctype="CRM Deal", name=deal)
		self.assertTrue(d["success"], d)
		self.assertIn(str(task), [str(x["id"]) for x in d["data"]["activities"]])
		# Converting again gives the same deal; someone without access can't convert.
		self.assertEqual(call(customers.convert_to_deal, self.sales, name=lead)["data"]["deal"], deal)
		other = make_lead(f"HM Convert2 {_n()}", f"+91 97473 {_n()}", owner=self.sales)
		self.assertFalse(call(customers.convert_to_deal, self.trainer, name=other)["success"])
