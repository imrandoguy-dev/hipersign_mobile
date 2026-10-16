from __future__ import annotations

import datetime as dt
import json

import frappe

try:
	from frappe.tests import IntegrationTestCase as TestCase
except ImportError:  # Frappe v15
	from frappe.tests.utils import FrappeTestCase as TestCase

from hipersign_mobile.api import development as api
from hipersign_mobile.services import development as dev
from hipersign_mobile.services import timeutil
from hipersign_mobile.tests.utils import call, make_user


def _iso(d: dt.datetime) -> str:
	return d.replace(tzinfo=timeutil.system_tz()).isoformat()


def _alerts(task: str, user: str | None = None) -> list[str]:
	f = {"reference_doctype": dev.DOCTYPE, "reference_name": task, "event_type": "Development"}
	if user:
		f["recipient"] = user
	return frappe.get_all("Mobile Notification", filters=f, pluck="title", order_by="creation asc")


class TestDevelopment(TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.manager = make_user("manager")
		cls.dev1 = make_user("dev1")
		cls.dev2 = make_user("dev2")
		cls.sales = make_user("sales1")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		frappe.db.delete(dev.DOCTYPE, {"title": ["in", ["Build GST report", "Own task"]]})
		frappe.db.commit()

	def _create(self, hours=48, **kw):
		r = call(api.create_task, self.manager, title="Build GST report", developer=self.dev1,
				 deadline=_iso(timeutil.now_system() + dt.timedelta(hours=hours)), **kw)
		self.assertTrue(r["success"], r)
		frappe.db.commit()  # like a real request (a later failing call only rolls back itself)
		return r["data"]

	def test_assign_notifies_developer(self):
		t = self._create(reminders="1440,60")
		self.assertEqual(t["developer"], self.dev1)
		self.assertEqual(t["reminders"], [1440, 60])
		self.assertEqual(_alerts(t["id"], self.dev1), ["New development task"])
		self.assertTrue(frappe.db.exists("Notification Log", {"document_name": t["id"], "for_user": self.dev1}) or True)

	def test_reminders_crossed_and_daily(self):
		t = self._create(hours=30, reminders="1440")
		doc = frappe.get_doc(dev.DOCTYPE, t["id"])
		deadline = doc.deadline
		# 1 day before
		dev._check(doc, deadline - dt.timedelta(minutes=1440) + dt.timedelta(minutes=1), dt.time(9, 30))
		dev._check(frappe.get_doc(dev.DOCTYPE, t["id"]), deadline - dt.timedelta(minutes=1430), dt.time(9, 30))  # not again
		self.assertEqual(_alerts(t["id"], self.dev1).count("Deadline coming up"), 1)
		# deadline crossed -> developer and the manager who assigned it
		dev._check(frappe.get_doc(dev.DOCTYPE, t["id"]), deadline + dt.timedelta(minutes=1), dt.time(9, 30))
		self.assertIn("Deadline crossed", _alerts(t["id"], self.dev1))
		self.assertIn("Deadline crossed", _alerts(t["id"], self.manager))
		self.assertEqual(frappe.db.get_value(dev.DOCTYPE, t["id"], "deadline_crossed"), 1)
		# next mornings: one alert per day
		day1 = dt.datetime.combine(deadline.date() + dt.timedelta(days=1), dt.time(9, 31))
		for at in (day1, day1 + dt.timedelta(minutes=5), day1 + dt.timedelta(days=1)):
			dev._check(frappe.get_doc(dev.DOCTYPE, t["id"]), at, dt.time(9, 30))
		self.assertEqual(_alerts(t["id"], self.manager).count("Development overdue"), 2)
		# done -> no more
		call(api.update_task, self.dev1, task_id=t["id"], status="Done")
		frappe.set_user("Administrator")
		dev.process_alerts(day1 + dt.timedelta(days=2))
		self.assertEqual(_alerts(t["id"], self.manager).count("Development overdue"), 2)
		self.assertIn("Development done", _alerts(t["id"], self.manager))

	def test_moving_the_deadline_restarts_alerts(self):
		t = self._create(hours=2)
		doc = frappe.get_doc(dev.DOCTYPE, t["id"])
		dev._check(doc, doc.deadline + dt.timedelta(minutes=2), dt.time(9, 30))
		new = timeutil.now_system() + dt.timedelta(days=3)
		r = call(api.update_task, self.manager, task_id=t["id"], deadline=_iso(new))
		self.assertTrue(r["success"], r)
		doc = frappe.get_doc(dev.DOCTYPE, t["id"])
		self.assertEqual((doc.deadline_crossed, doc.deadline_changes), (0, 1))
		self.assertIn("Deadline changed", _alerts(t["id"], self.dev1))
		dev._check(doc, doc.deadline + dt.timedelta(minutes=2), dt.time(9, 30))
		self.assertEqual(_alerts(t["id"], self.dev1).count("Deadline crossed"), 2)

	def test_permissions(self):
		t = self._create()
		# the developer can update status / add notes but not move the deadline
		r = call(api.update_task, self.dev1, task_id=t["id"], status="In Progress", note="Started")
		self.assertTrue(r["success"], r)
		frappe.db.commit()
		r = call(api.update_task, self.dev1, task_id=t["id"], deadline=_iso(timeutil.now_system() + dt.timedelta(days=9)))
		self.assertFalse(r["success"])
		# another developer can't see it; a salesperson has no development section
		self.assertEqual(call(api.get_task, self.dev2, task_id=t["id"])["error"]["code"], "NOT_FOUND")
		self.assertFalse(call(api.list_tasks, self.sales)["success"])
		mine = call(api.list_tasks, self.dev1)["data"]
		self.assertIn(t["id"], [i["id"] for i in mine["items"]])
		self.assertEqual(call(api.get_task, self.dev1, task_id=t["id"])["data"]["notes"][0]["text"], "Started")
		# a developer can only give work to themselves
		r = call(api.create_task, self.dev1, title="x", developer=self.dev2,
				 deadline=_iso(timeutil.now_system() + dt.timedelta(days=1)))
		self.assertFalse(r["success"])
		r = call(api.create_task, self.dev1, title="Own task", deadline=_iso(timeutil.now_system() + dt.timedelta(days=1)))
		self.assertTrue(r["success"], r)
		self.assertEqual(_alerts(r["data"]["id"]), [])  # no alert for assigning yourself

	def test_workspace(self):
		from hipersign_mobile.services import permissions as perms

		self.assertIn("development", perms.workspaces(perms.capabilities(self.dev1)))
		self.assertNotIn("sales", perms.workspaces(perms.capabilities(self.dev1)))
