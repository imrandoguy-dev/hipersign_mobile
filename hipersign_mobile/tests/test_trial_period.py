from __future__ import annotations

import datetime as dt

import frappe

try:
	from frappe.tests import IntegrationTestCase as TestCase
except ImportError:  # Frappe v15
	from frappe.tests.utils import FrappeTestCase as TestCase

from hipersign_mobile.api import trials
from hipersign_mobile.services import booking_service as svc
from hipersign_mobile.tests.utils import call, make_customer, make_task, make_user


def _monday(weeks_ahead=2):
	d = dt.date.today() + dt.timedelta(days=7 * weeks_ahead)
	return d - dt.timedelta(days=d.weekday())


class TestTrialPeriod(TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.sales = make_user("sales1")
		cls.trainer = make_user("trainer")
		cls.customer = make_customer("HM Trial Period Customer")
		for b in frappe.get_all("Trial Booking", filters={"trainer": cls.trainer}, pluck="name"):
			frappe.db.set_value("CRM Task", {"hm_trial_booking": b}, "hm_trial_booking", None)
			frappe.delete_doc("Trial Booking", b, force=True, ignore_permissions=True)
		frappe.db.delete("Trainer Slot", {"trainer": cls.trainer})
		frappe.db.delete("CRM Task", {"assigned_to": cls.trainer})
		frappe.db.delete("Employee", {"user_id": cls.trainer})
		frappe.db.set_single_value("Hipersign Mobile Settings", {
			"auto_trainer_slots": 1, "trainer_working_days": "Mon,Tue,Wed,Thu,Fri,Sat",
			"trainer_day_start": "09:30:00", "trainer_day_end": "18:00:00", "trial_slot_minutes": 60,
		})
		frappe.clear_document_cache("Hipersign Mobile Settings", "Hipersign Mobile Settings")
		frappe.db.commit()

	def tearDown(self):
		frappe.db.rollback()

	def _slot(self, day, hhmm="10:30"):
		r = call(trials.get_available_slots, self.sales, start_date=str(day), end_date=str(day), trainer=self.trainer)
		return next(i["id"] for i in r["data"]["items"] if i["start"][11:16] == hhmm)

	def test_seven_day_trial_books_each_working_day(self):
		mon = _monday(2)
		# trainer busy on Wednesday at 10:30
		make_task(self.trainer, dt.datetime.combine(mon + dt.timedelta(days=2), dt.time(10, 30)),
				  ref=("Customer", self.customer), hm_end_datetime=dt.datetime.combine(mon + dt.timedelta(days=2), dt.time(11, 30)))
		r = call(trials.book_trial, self.sales, trainer_slot=self._slot(mon), customer=self.customer,
				 booking_type="ERP Trial", trial_days=7, idempotency_key="trial-7")
		self.assertTrue(r["success"], r)
		t = r["data"]["booking"]["trial"]
		# 7 trial days: Mon-Sat, Sunday skipped, so it ends the next Monday
		self.assertEqual((t["days"], t["start_date"], t["end_date"]), (7, str(mon), str(mon + dt.timedelta(days=7))))
		# 7 trial days minus the busy Wednesday = 6 sessions
		self.assertEqual(len(t["sessions"]), 6)
		self.assertEqual([s["session_no"] for s in t["sessions"]], [1, 2, 3, 4, 5, 6])
		self.assertNotIn(str(mon + dt.timedelta(days=6)), [s["start"][:10] for s in t["sessions"]])  # no Sunday
		self.assertEqual(r["data"]["skipped"], [{"date": str(mon + dt.timedelta(days=2)), "reason": "Trainer is busy at this time"}])
		self.assertEqual(t["phase"], "upcoming")

		# add the missing Wednesday at another time
		r2 = call(trials.add_trial_session, self.sales, booking_id=t["series"],
				  trainer_slot=self._slot(mon + dt.timedelta(days=2), "14:30"))
		self.assertTrue(r2["success"], r2)
		self.assertEqual(len(r2["data"]["booking"]["trial"]["sessions"]), 7)

		# change the length to 5 days
		r3 = call(trials.set_trial_days, self.sales, booking_id=t["series"], days=5)
		self.assertEqual(r3["data"]["booking"]["trial"]["end_date"], str(mon + dt.timedelta(days=4)))  # Fri

	def test_countdown_skips_sundays_and_holidays(self):
		# Mon 19 Oct 2026 + 7 trial days, Thu 22 is a holiday -> Mon..Wed, Fri, Sat, Mon 26, Tue 27
		hl = frappe.get_doc({"doctype": "Holiday List", "holiday_list_name": "HM Trial HL", "from_date": "2026-01-01",
							 "to_date": "2026-12-31", "holidays": [{"holiday_date": "2026-10-22", "description": "Holiday"}]})
		if not frappe.db.exists("Holiday List", "HM Trial HL"):
			hl.insert(ignore_permissions=True)
		frappe.db.set_single_value("Hipersign Mobile Settings", "trial_holiday_list", "HM Trial HL")
		try:
			days = svc.trial_days_from(dt.date(2026, 10, 19), 7)
			self.assertEqual(days[-1], dt.date(2026, 10, 27))
			self.assertNotIn(dt.date(2026, 10, 22), days)
			self.assertNotIn(dt.date(2026, 10, 25), days)
			b = frappe._dict(trial_series="X", trial_days=7, trial_start_date="2026-10-19",
							 trial_end_date="2026-10-27", start_datetime="2026-10-19 10:30:00")
			info = svc.trial_info(b, today=dt.date(2026, 10, 21))
			self.assertEqual((info["phase"], info["day_no"], info["days_left"], info["today_off"]), ("running", 3, 4, False))
			info = svc.trial_info(b, today=dt.date(2026, 10, 22))  # holiday: paused
			self.assertEqual((info["day_no"], info["days_left"], info["today_off"]), (3, 4, True))
			info = svc.trial_info(b, today=dt.date(2026, 10, 25))  # Sunday
			self.assertEqual((info["day_no"], info["days_left"], info["today_off"]), (5, 2, True))
			info = svc.trial_info(b, today=dt.date(2026, 10, 17))
			self.assertEqual((info["phase"], info["starts_in"], info["days_left"]), ("upcoming", 2, 7))
			info = svc.trial_info(b, today=dt.date(2026, 10, 28))
			self.assertEqual((info["phase"], info["days_left"]), ("ended", 0))
		finally:
			frappe.db.set_single_value("Hipersign Mobile Settings", "trial_holiday_list", None)

	def test_single_session_is_a_one_day_trial(self):
		mon = _monday(3)
		r = call(trials.book_trial, self.sales, trainer_slot=self._slot(mon), customer=self.customer, booking_type="ERP Trial")
		self.assertTrue(r["success"], r)
		self.assertEqual(r["data"]["booking"]["trial"]["days"], 1)
		self.assertEqual(len(r["data"]["booking"]["trial"]["sessions"]), 1)
