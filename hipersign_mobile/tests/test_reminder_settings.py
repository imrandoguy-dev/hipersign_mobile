from __future__ import annotations

import datetime as dt
from unittest.mock import patch

import frappe

try:
	from frappe.tests import IntegrationTestCase as TestCase
except ImportError:  # Frappe v15
	from frappe.tests.utils import FrappeTestCase as TestCase

from hipersign_mobile.api import activities, devices
from hipersign_mobile.notifications import fcm, queue
from hipersign_mobile.tests.utils import call, future, make_customer, make_task, make_user


def _queued(task):
	return frappe.get_all(
		"Mobile Notification",
		filters={"reference_name": str(task), "event_type": "Reminder", "status": "Queued"},
		fields=["title", "scheduled_for", "name"],
		order_by="scheduled_for asc",
	)


class TestReminderSettings(TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.sales = make_user("sales1")
		cls.customer = make_customer("HM Reminder Customer")

	def tearDown(self):
		frappe.db.rollback()
		frappe.db.delete("Mobile User Preference", {"user": self.sales})
		frappe.db.commit()

	def test_my_reminder_times(self):
		r = call(devices.update_notification_preferences, self.sales, reminder_offsets="60, 15,0")
		self.assertTrue(r["success"], r)
		p = call(devices.get_notification_preferences, self.sales)["data"]
		self.assertEqual(p["reminder_offsets"], [60, 15, 0])
		due = future(2, hour=10)
		t = make_task(self.sales, due, ref=("Customer", self.customer))
		q = _queued(t.name)
		self.assertEqual(
			[frappe.utils.get_datetime(x.scheduled_for) for x in q],
			[due - dt.timedelta(minutes=60), due - dt.timedelta(minutes=15), due],
		)
		self.assertEqual([x.title for x in q], ["Upcoming follow-up in 1 h", "Upcoming follow-up in 15 min", "Follow-up now"])

	def test_bad_reminder_times(self):
		for bad in ("abc", "-5", "1,2,3,4,5,6", ""):
			r = call(devices.update_notification_preferences, self.sales, reminder_offsets=bad)
			self.assertFalse(r["success"], bad)

	def test_activity_own_reminders(self):
		start = future(3, hour=11)
		r = call(activities.create_activity, self.sales, title="Big demo", reference_doctype="Customer",
				 reference_name=self.customer, start=start.isoformat(), reminder_offsets="1440")
		self.assertTrue(r["success"], r)
		a = r["data"]["activity"]
		self.assertEqual(a["reminder_offsets"], [1440])
		self.assertEqual([x.title for x in _queued(a["id"])], ["Upcoming follow-up tomorrow"])
		# back to my default (15 min before + at start)
		r = call(activities.update_activity, self.sales, activity_id=a["id"], reminder_offsets="")
		frappe.local.form_dict = frappe._dict()
		self.assertTrue(r["success"], r)
		self.assertIsNone(r["data"]["activity"]["reminder_offsets"])
		self.assertEqual(len(_queued(a["id"])), 2)

	def test_high_priority_rings_like_alarm(self):
		self.assertTrue(call(devices.register_device, self.sales, device_id="dev-alarm", platform="Android",
							 push_token="tok-alarm")["success"])
		t = make_task(self.sales, future(2, hour=9), ref=("Customer", self.customer), priority="High")
		name = _queued(t.name)[0].name
		sent = []
		with patch.object(fcm, "send", lambda tok, title, body, data=None: sent.append(data) or fcm.SendResult(ok=True)):
			queue.deliver(name)
		self.assertEqual(sent[-1].get("alarm"), "1")
		msg = fcm.build_message("tok", "T", "B", sent[-1])["message"]
		self.assertNotIn("notification", msg)  # the app shows it with the alarm sound
		self.assertEqual(msg["data"]["title"], "T")

		# switched off -> normal notification
		call(devices.update_notification_preferences, self.sales, alarm_high_priority=0)
		t2 = make_task(self.sales, future(2, hour=12), ref=("Customer", self.customer), priority="High")
		sent.clear()
		with patch.object(fcm, "send", lambda tok, title, body, data=None: sent.append(data) or fcm.SendResult(ok=True)):
			queue.deliver(_queued(t2.name)[0].name)
		self.assertNotIn("alarm", sent[-1])
