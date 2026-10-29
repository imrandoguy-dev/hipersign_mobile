from __future__ import annotations

import json
import uuid

import frappe

try:
	from frappe.tests import IntegrationTestCase as TestCase
except ImportError:  # Frappe v15
	from frappe.tests.utils import FrappeTestCase as TestCase

from hipersign_mobile.api import activities
from hipersign_mobile.services import call_targets
from hipersign_mobile.tests.utils import call, future, make_lead, make_user


def _n():
	return str(uuid.uuid4().int)[:5]


class TestMeetingPeople(TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.sales = make_user("sales1")
		cls.sales2 = make_user("sales2")
		cls.manager = make_user("manager")
		frappe.db.commit()

	def tearDown(self):
		frappe.db.rollback()

	def _meeting(self, people, as_user=None, kind="Meeting"):
		lead = make_lead(f"HM Meet {_n()}", f"+91 97463 {_n()}")
		start = future(2, 11)
		return call(
			activities.create_activity,
			as_user or self.sales,
			title="Demo",
			activity_type=kind,
			reference_doctype="CRM Lead",
			reference_name=lead,
			start=start.isoformat(),
			end=(start + frappe.utils.datetime.timedelta(hours=1)).isoformat(),
			people=json.dumps(people),
			idempotency_key=f"k{_n()}{_n()}",
		)

	def _copies(self, activity_id):
		group = frappe.db.get_value("CRM Task", activity_id, "hm_meeting_group")
		return {r.assigned_to: r for r in frappe.get_all(
			"CRM Task", filters={"hm_meeting_group": group}, fields=["name", "assigned_to", "status", "due_date", "title"]
		)}

	def test_meeting_for_several_people(self):
		r = self._meeting([self.sales, self.sales2, self.manager])
		self.assertTrue(r["success"], r)
		a = r["data"]["activity"]
		self.assertEqual(a["assigned_to"], self.sales)
		self.assertEqual({p["id"] for p in a["people"]}, {self.sales, self.sales2, self.manager})
		copies = self._copies(a["id"])
		self.assertEqual(set(copies), {self.sales, self.sales2, self.manager})
		# Each invited person is told about it.
		self.assertTrue(frappe.db.exists("Mobile Notification", {"recipient": self.sales2, "reference_name": copies[self.sales2].name}))
		# Counted as one meeting booked.
		now = frappe.utils.now_datetime()
		n = call_targets.meetings_assigned(self.sales, now - frappe.utils.datetime.timedelta(hours=1), now + frappe.utils.datetime.timedelta(hours=1))
		self.assertEqual(n, 1)

	def test_follow_up_for_others_needs_manager(self):
		r = self._meeting([self.sales, self.sales2], kind="Follow-up")
		self.assertFalse(r["success"])

	def test_reschedule_and_complete_apply_to_everyone(self):
		r = self._meeting([self.sales, self.sales2])
		aid = r["data"]["activity"]["id"]
		new = future(3, 15)
		r = call(activities.update_activity, self.sales, activity_id=aid, title="Demo moved", start=new.isoformat())
		self.assertTrue(r["success"], r)
		copies = self._copies(aid)
		self.assertEqual({c.title for c in copies.values()}, {"Demo moved"})
		self.assertEqual({frappe.utils.get_datetime(c.due_date) for c in copies.values()}, {new.replace(tzinfo=None)})
		r = call(activities.update_status, self.sales2, activity_id=copies[self.sales2].name, status="Completed")
		self.assertTrue(r["success"], r)
		self.assertEqual({c.status for c in self._copies(aid).values()}, {"Done"})

	def test_add_and_remove_people(self):
		r = self._meeting([self.sales])
		aid = r["data"]["activity"]["id"]
		self.assertEqual(len(r["data"]["activity"]["people"]), 1)
		r = call(activities.update_activity, self.sales, activity_id=aid, people=json.dumps([self.sales, self.sales2, self.manager]))
		self.assertTrue(r["success"], r)
		self.assertEqual(len(r["data"]["activity"]["people"]), 3)
		r = call(activities.update_activity, self.sales, activity_id=aid, people=json.dumps([self.sales, self.manager]))
		self.assertTrue(r["success"], r)
		self.assertEqual(set(self._copies(aid)), {self.sales, self.manager})

	def test_invited_person_can_leave_but_not_remove_others(self):
		aid = self._meeting([self.sales, self.sales2])["data"]["activity"]["id"]
		theirs = self._copies(aid)[self.sales2].name
		r = call(activities.update_activity, self.sales2, activity_id=theirs, people=json.dumps([self.sales]))
		self.assertTrue(r["success"], r)
		self.assertTrue(r["data"]["removed"])
		self.assertEqual(set(self._copies(aid)), {self.sales})

	def test_invited_person_cannot_remove_others(self):
		aid = self._meeting([self.sales, self.sales2])["data"]["activity"]["id"]
		theirs = self._copies(aid)[self.sales2].name
		r = call(activities.update_activity, self.sales2, activity_id=theirs, people=json.dumps([self.sales2]))
		self.assertFalse(r["success"])

	def test_delete_by_organiser_removes_all(self):
		r = self._meeting([self.sales, self.sales2])
		aid = r["data"]["activity"]["id"]
		group = frappe.db.get_value("CRM Task", aid, "hm_meeting_group")
		r = call(activities.delete_activity, self.sales, activity_id=aid)
		self.assertTrue(r["success"], r)
		self.assertFalse(frappe.get_all("CRM Task", filters={"hm_meeting_group": group}))

	def test_people_list(self):
		r = call(activities.people, self.sales)
		self.assertTrue(r["success"], r)
		self.assertIn(self.sales2, [p["id"] for p in r["data"]["items"]])
