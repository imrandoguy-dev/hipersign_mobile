from __future__ import annotations

import datetime as dt

import frappe

try:
	from frappe.tests import IntegrationTestCase as TestCase
except ImportError:  # Frappe v15
	from frappe.tests.utils import FrappeTestCase as TestCase

from hipersign_mobile.api import trials
from hipersign_mobile.services import trainer_hours
from unittest.mock import patch

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
		# 09:30-18:00, hourly, but never during the 13:00-14:00 lunch break
		self.assertEqual(starts, ["09:30", "10:30", "11:30", "14:30", "15:30", "16:30"])
		self.assertTrue(all(i["id"].startswith("auto:") for i in items))

	def test_assigned_activity_makes_trainer_busy(self):
		day = _next_weekday(1)
		make_task(self.trainer, dt.datetime.combine(day, dt.time(11, 0)), ref=("Customer", self.customer),
				  hm_end_datetime=dt.datetime.combine(day, dt.time(12, 0)))
		starts = [i["start"][11:16] for i in self._day_slots(day, self.trainer)]
		self.assertEqual(starts, ["09:30", "14:30", "15:30", "16:30"])  # 11-12 busy + lunch

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

	def _employee(self, user, holiday_list=None):
		company = frappe.db.get_value("Company", {}, "name") or "HM Test Co"  # test site has no setup
		if not frappe.db.exists("Gender", "Female"):
			frappe.get_doc({"doctype": "Gender", "gender": "Female"}).insert(ignore_permissions=True)
		name = frappe.db.get_value("Employee", {"user_id": user})
		if not name:
			emp = frappe.get_doc({
				"doctype": "Employee", "first_name": "HM Trainer", "gender": "Female",
				"date_of_birth": "1995-01-01", "date_of_joining": "2024-01-01", "company": company, "status": "Active",
				"user_id": user,
			})
			emp.name = f"HM-EMP-{user.split('@')[0]}"
			emp.db_insert()  # the test site has no company set up: store the row as-is
			name = emp.name
		frappe.db.set_value("Employee", name, "holiday_list", holiday_list)
		return name

	def test_shift_hours_and_its_own_lunch_break(self):
		self._employee(self.trainer)
		day = _next_weekday(4)
		with patch.object(trainer_hours, "shift_for", return_value="Shift 9:00-5:00"), patch.object(
			trainer_hours, "_shift_hours", return_value=(dt.time(9, 0), dt.time(17, 0), (dt.time(12, 30), dt.time(13, 0)))
		):
			starts = [i["start"][11:16] for i in self._day_slots(day, self.trainer)]
		# 9:00-17:00 shift, its own 12:30-13:00 break
		self.assertEqual(starts, ["09:00", "10:00", "11:00", "13:00", "14:00", "15:00", "16:00"])

	def test_holiday_is_a_day_off(self):
		day = _next_weekday(5)
		hl = frappe.get_doc({
			"doctype": "Holiday List", "holiday_list_name": f"HM HL {day}", "from_date": day - dt.timedelta(days=30),
			"to_date": day + dt.timedelta(days=30), "holidays": [{"holiday_date": day, "description": "Festival"}],
		}).insert(ignore_permissions=True)
		self._employee(self.trainer2, hl.name)
		self.assertEqual(self._day_slots(day, self.trainer2), [])
		self.assertTrue(self._day_slots(day + dt.timedelta(days=1), self.trainer2))
