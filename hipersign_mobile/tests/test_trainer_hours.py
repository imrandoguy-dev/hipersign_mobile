from __future__ import annotations

import datetime as dt

import frappe

try:
	from frappe.tests import IntegrationTestCase as TestCase
except ImportError:  # Frappe v15
	from frappe.tests.utils import FrappeTestCase as TestCase

from hipersign_mobile.api import trials
from hipersign_mobile.services import trainer_hours
from hipersign_mobile.tests.utils import call, make_customer, make_task, make_user


def _next_weekday(nth=0):
	"""The nth working day (Mon-Sat) starting 3 days from today - a different day per test."""
	d = dt.date.today() + dt.timedelta(days=3)
	found = -1
	while True:
		if d.weekday() != 6:
			found += 1
			if found == nth:
				return d
		d += dt.timedelta(days=1)


class TestTrainerWorkingHours(TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.sales = make_user("sales1")
		cls.trainer = make_user("trainer")
		cls.trainer2 = make_user("trainer2")
		cls.customer = make_customer("HM Trainer Hours Customer")
		for t in (cls.trainer, cls.trainer2):
			for b in frappe.get_all("Trial Booking", filters={"trainer": t}, pluck="name"):
				frappe.db.set_value("CRM Task", {"hm_trial_booking": b}, "hm_trial_booking", None)
				frappe.delete_doc("Trial Booking", b, force=True, ignore_permissions=True)
			frappe.db.delete("Trainer Slot", {"trainer": t})
			frappe.db.delete("CRM Task", {"assigned_to": t})
		frappe.db.set_single_value("Hipersign Mobile Settings", {
			"auto_trainer_slots": 1, "trainer_working_days": "Mon,Tue,Wed,Thu,Fri,Sat",
			"trainer_day_start": "09:30:00", "trainer_day_end": "18:00:00", "trial_slot_minutes": 60,
		})
		frappe.clear_document_cache("Hipersign Mobile Settings", "Hipersign Mobile Settings")
		frappe.db.commit()

	def tearDown(self):
		frappe.db.rollback()

	def _day_slots(self, day, trainer):
		r = call(trials.get_available_slots, self.sales, start_date=str(day), end_date=str(day), trainer=trainer)
		self.assertTrue(r["success"], r)
		return r["data"]["items"]

	def test_trainer_without_slots_is_free_in_working_hours(self):
		day = _next_weekday()
		items = self._day_slots(day, self.trainer)
		starts = [i["start"][11:16] for i in items]
		self.assertEqual(starts[0], "09:30")
		self.assertEqual(starts[-1], "16:30")  # last full hour before 18:00
		self.assertEqual(len(items), 8)
		self.assertTrue(all(i["id"].startswith("auto:") for i in items))

	def test_assigned_activity_makes_trainer_busy(self):
		day = _next_weekday(1)
		make_task(self.trainer, dt.datetime.combine(day, dt.time(11, 0)), ref=("Customer", self.customer),
				  hm_end_datetime=dt.datetime.combine(day, dt.time(12, 0)))
		starts = [i["start"][11:16] for i in self._day_slots(day, self.trainer)]
		self.assertNotIn("10:30", starts)  # 10:30-11:30 overlaps 11:00-12:00
		self.assertNotIn("11:30", starts)
		self.assertIn("09:30", starts)

	def test_book_free_time_then_it_is_taken(self):
		day = _next_weekday(2)
		slot = self._day_slots(day, self.trainer2)[2]
		r = call(trials.book_trial, self.sales, trainer_slot=slot["id"], customer=self.customer, booking_type="ERP Trial")
		self.assertTrue(r["success"], r)
		b = r["data"]["booking"]
		self.assertEqual(b["trainer"], self.trainer2)
		self.assertEqual(frappe.db.get_value("Trainer Slot", b["trainer_slot"], "status"), "Reserved")
		self.assertNotIn(slot["id"], [i["id"] for i in self._day_slots(day, self.trainer2)])
		r2 = call(trials.book_trial, self.sales, trainer_slot=slot["id"], customer=self.customer, booking_type="ERP Trial")
		self.assertFalse(r2["success"])

	def test_switched_off(self):
		frappe.db.set_single_value("Hipersign Mobile Settings", "auto_trainer_slots", 0)
		frappe.clear_document_cache("Hipersign Mobile Settings", "Hipersign Mobile Settings")
		try:
			self.assertEqual(self._day_slots(_next_weekday(3), self.trainer), [])
		finally:
			frappe.db.set_single_value("Hipersign Mobile Settings", "auto_trainer_slots", 1)
			frappe.clear_document_cache("Hipersign Mobile Settings", "Hipersign Mobile Settings")

	def test_bad_or_outside_hours_id(self):
		day = _next_weekday()
		bad = trainer_hours.slot_id(self.trainer, dt.datetime.combine(day, dt.time(20, 0)), 60)
		r = call(trials.book_trial, self.sales, trainer_slot=bad, customer=self.customer, booking_type="ERP Trial")
		self.assertFalse(r["success"])
