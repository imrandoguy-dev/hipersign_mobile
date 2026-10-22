"""Backend acceptance tests, grouped like the PRD's QA checklist.

Run:  bench --site <test-site> run-tests --app hipersign_mobile
"""

from __future__ import annotations

import datetime as dt
from unittest.mock import patch

import frappe

try:
	from frappe.tests import IntegrationTestCase as TestCase
except ImportError:  # Frappe v15
	from frappe.tests.utils import FrappeTestCase as TestCase

from hipersign_mobile.api import activities, auth, calendar, customers, devices, trials
from hipersign_mobile.notifications import fcm, queue
from hipersign_mobile.services import timeutil
from hipersign_mobile.services.phone import normalize_phone
from hipersign_mobile.tests.utils import (
	call,
	future,
	make_customer,
	make_lead,
	make_slot,
	make_task,
	make_user,
)


class Base(TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		frappe.set_user("Administrator")
		cls.s1 = make_user("sales1")
		cls.s2 = make_user("sales2")
		cls.mgr = make_user("manager")
		cls.trainer = make_user("trainer")
		cls.trainer2 = make_user("trainer2")
		cls.norole = make_user("norole")
		cls.cust = make_customer("HM Test Customer", phone="98765 43210")
		cls.cust2 = make_customer("HM Other Customer")
		cls.lead = make_lead("HMLeadOne", mobile="9087652245", owner=cls.s1)
		settings = frappe.get_single("Hipersign Mobile Settings")
		settings.customer_scope = "All Records"
		settings.default_country_code = "91"
		settings.enable_push = 1
		settings.save()
		frappe.db.commit()

	def setUp(self):
		frappe.set_user("Administrator")
		frappe.local.hm_party_cache = {}
		self.uid = frappe.generate_hash(length=8)  # unique keys per run: the test DB persists

	def tearDown(self):
		frappe.set_user("Administrator")


# ------------------------------------------------------------------ Access


class TestAccess(Base):
	def test_salesperson_gets_sales_workspace(self):
		r = call(auth.get_me, self.s1)
		self.assertTrue(r["success"])
		self.assertIn("sales", r["data"]["workspaces"])
		self.assertTrue(r["data"]["capabilities"]["trial_booking"])
		self.assertFalse(r["data"]["capabilities"]["team_view"])

	def test_user_without_mobile_role_gets_no_workspace(self):
		r = call(auth.get_me, self.norole)
		self.assertEqual(r["data"]["workspaces"], [])
		r = call(calendar.get_my_day, self.norole)
		self.assertEqual(r["_http_status"], 403)
		self.assertEqual(r["error"]["code"], "FORBIDDEN")

	def test_cannot_read_or_change_another_employees_activity(self):
		frappe.set_user("Administrator")
		t = make_task(self.s1, future(1), ref=("Customer", self.cust))
		for fn, kwargs in (
			(activities.get_detail, {"activity_id": t.name}),
			(activities.update_status, {"activity_id": t.name, "status": "Completed"}),
			(
				activities.reschedule_activity,
				{"activity_id": t.name, "new_start_datetime": future(2).isoformat()},
			),
			(activities.add_note, {"activity_id": t.name, "note": "x"}),
		):
			r = call(fn, self.s2, **kwargs)
			self.assertEqual(r["_http_status"], 404, fn.__name__)
		self.assertEqual(frappe.db.get_value("CRM Task", t.name, "status"), "Todo")

	def test_cannot_query_another_employees_calendar(self):
		r = call(calendar.get_my_day, self.s2, user=self.s1)
		self.assertEqual(r["_http_status"], 403)
		r = call(calendar.get_my_day, self.mgr, user=self.s1)  # manager has team_view
		self.assertTrue(r["success"])

	def test_cannot_create_activity_for_someone_else(self):
		r = call(
			activities.create_activity,
			self.s2,
			title="Sneaky",
			reference_doctype="Customer",
			reference_name=self.cust,
			start=future(1).isoformat(),
			assigned_to=self.s1,
		)
		self.assertEqual(r["_http_status"], 403)


# ------------------------------------------------------------------ Calendar & activities


class TestCalendar(Base):
	def test_activity_on_correct_local_date_and_timezone(self):
		frappe.set_user("Administrator")
		frappe.db.set_value("User", self.s2, "time_zone", "Asia/Dubai")
		# 01:00 IST on day+5 is 23:30 Dubai on day+4.
		due = future(5, hour=1, minute=0)
		t = make_task(self.s2, due, title="TZ check", ref=("Customer", self.cust))
		day4 = (due - dt.timedelta(days=1)).date().isoformat()
		day5 = due.date().isoformat()
		r4 = call(calendar.get_my_day, self.s2, start_date=day4, end_date=day4)
		r5 = call(calendar.get_my_day, self.s2, start_date=day5, end_date=day5)
		ids4 = [a["id"] for a in r4["data"]["items"]]
		ids5 = [a["id"] for a in r5["data"]["items"]]
		self.assertIn(str(t.name), ids4)
		self.assertNotIn(str(t.name), ids5)
		item = next(a for a in r4["data"]["items"] if a["id"] == str(t.name))
		self.assertTrue(item["start"].endswith("+05:30"))  # explicit offset, never ambiguous
		frappe.db.set_value("User", self.s2, "time_zone", None)

	def test_display_states(self):
		frappe.set_user("Administrator")
		# Leftovers from earlier runs (persisted test DB) must not crowd these out of the day view.
		frappe.db.delete("CRM Task", {"assigned_to": self.s1, "due_date": ["between", [future(-3), future(3)]]})
		active = make_task(self.s1, future(1), title="Active one", ref=("Customer", self.cust))
		overdue = make_task(self.s1, future(-2), title="Overdue one", ref=("Customer", self.cust))
		done = make_task(self.s1, future(1, hour=12), title="Done one", ref=("Customer", self.cust))
		call(activities.update_status, self.s1, activity_id=done.name, status="Completed")
		moved = make_task(self.s1, future(1, hour=14), title="Moved one", ref=("Customer", self.cust))
		call(
			activities.reschedule_activity,
			self.s1,
			activity_id=moved.name,
			new_start_datetime=future(2, hour=15).isoformat(),
		)

		day1 = future(1).date().isoformat()
		day2 = future(2).date().isoformat()
		r = call(calendar.get_my_day, self.s1, start_date=day1, end_date=day2)
		state = {a["id"]: a["display_state"] for a in r["data"]["items"]}
		self.assertEqual(state[str(active.name)], "active")
		self.assertEqual(state[str(done.name)], "completed")
		self.assertEqual(state[str(moved.name)], "rescheduled")
		overdue_ids = {a["id"]: a["display_state"] for a in r["data"]["overdue"]}
		self.assertEqual(overdue_ids[str(overdue.name)], "pending")  # overdue stays visible

	def test_complete_updates_erpnext_and_is_idempotent(self):
		frappe.set_user("Administrator")
		t = make_task(self.s1, future(1), ref=("Customer", self.cust))
		r = call(
			activities.update_status,
			self.s1,
			activity_id=t.name,
			status="Completed",
			outcome="Demo booked",
			notes="Good call",
		)
		self.assertTrue(r["data"]["changed"])
		row = frappe.db.get_value(
			"CRM Task", t.name, ["status", "hm_completed_by", "hm_outcome"], as_dict=True
		)
		self.assertEqual(row.status, "Done")
		self.assertEqual(row.hm_completed_by, self.s1)
		self.assertEqual(row.hm_outcome, "Demo booked")
		before = frappe.db.count("Sales Activity History", {"crm_task": t.name})
		r = call(activities.update_status, self.s1, activity_id=t.name, status="Completed")  # retry
		self.assertFalse(r["data"]["changed"])
		self.assertEqual(frappe.db.count("Sales Activity History", {"crm_task": t.name}), before)

	def test_reschedule_preserves_history(self):
		frappe.set_user("Administrator")
		old = future(1, hour=11)
		t = make_task(self.s1, old, ref=("Customer", self.cust))
		new = future(3, hour=16)
		r = call(
			activities.reschedule_activity,
			self.s1,
			activity_id=t.name,
			new_start_datetime=new.isoformat(),
			reason="Client asked",
		)
		self.assertTrue(r["success"], r)
		self.assertEqual(r["data"]["activity"]["status"], "Rescheduled")
		self.assertEqual(frappe.db.get_value("CRM Task", t.name, "hm_reschedule_count"), 1)
		h = frappe.get_all(
			"Sales Activity History",
			filters={"crm_task": t.name, "event_type": "Rescheduled"},
			fields=["old_start_datetime", "new_start_datetime", "reason", "actor", "source"],
		)
		self.assertEqual(len(h), 1)
		self.assertEqual(frappe.utils.get_datetime(h[0].old_start_datetime), old)
		self.assertEqual(frappe.utils.get_datetime(h[0].new_start_datetime), new)
		self.assertEqual(h[0].reason, "Client asked")
		self.assertEqual(h[0].actor, self.s1)
		self.assertEqual(h[0].source, "Mobile")
		# Same request again (e.g. retry after timeout): no second history row.
		call(activities.reschedule_activity, self.s1, activity_id=t.name, new_start_datetime=new.isoformat())
		self.assertEqual(
			frappe.db.count("Sales Activity History", {"crm_task": t.name, "event_type": "Rescheduled"}), 1
		)

	def test_followup_and_meeting_filters(self):
		frappe.set_user("Administrator")
		# Leftovers from earlier runs (persisted test DB) must not push these off page 1.
		frappe.db.delete("CRM Task", {"assigned_to": self.s2, "status": ["not in", ["Done", "Canceled"]]})
		f = make_task(
			self.s2, future(1), title="F", ref=("Customer", self.cust), hm_activity_type="Follow-up"
		)
		m = make_task(self.s2, future(1), title="M", ref=("Customer", self.cust), hm_activity_type="Meeting")
		fu = call(activities.get_followups, self.s2, status="Pending")
		me = call(activities.get_meetings, self.s2, status="Pending")
		self.assertIn(str(f.name), [a["id"] for a in fu["data"]["items"]])
		self.assertNotIn(str(m.name), [a["id"] for a in fu["data"]["items"]])
		self.assertIn(str(m.name), [a["id"] for a in me["data"]["items"]])
		call(activities.update_status, self.s2, activity_id=f.name, status="Completed")
		done = call(activities.get_followups, self.s2, status="Completed")
		self.assertIn(str(f.name), [a["id"] for a in done["data"]["items"]])


# ------------------------------------------------------------------ Customer actions


class TestCustomers(Base):
	def test_phone_normalisation(self):
		cases = {
			"9087652245": "+919087652245",
			"98765 43210": "+919876543210",
			"09087652245": "+919087652245",
			"+971 50 123 4567": "+971501234567",
			"00971501234567": "+971501234567",
			"919087652245": "+919087652245",
			"971501234567": "+971501234567",
			"12345": None,
			"": None,
			"abc": None,
		}
		for raw, expected in cases.items():
			self.assertEqual(normalize_phone(raw, "91"), expected, raw)
		self.assertEqual(normalize_phone("0501234567", "971"), "+971501234567")

	def test_call_and_whatsapp_links_on_activity(self):
		frappe.set_user("Administrator")
		t = make_task(self.s1, future(1), ref=("CRM Lead", self.lead))
		r = call(activities.get_detail, self.s1, activity_id=t.name)
		phone = r["data"]["party"]["primary_phone"]
		self.assertEqual(phone["tel_url"], "tel:+919087652245")
		self.assertEqual(phone["whatsapp_url"], "https://wa.me/919087652245")

	def test_customer_phone_from_linked_contact(self):
		r = call(customers.get_detail, self.s1, doctype="Customer", name=self.cust)
		self.assertTrue(r["success"], r)
		self.assertEqual(r["data"]["primary_phone"]["e164"], "+919876543210")

	def test_search_respects_visibility_setting(self):
		r = call(customers.search, self.s2, query="HM Test Customer", kind="customer")
		names = [i["id"] for i in r["data"]["items"]]
		self.assertIn(self.cust, names)
		frappe.set_user("Administrator")
		s = frappe.get_single("Hipersign Mobile Settings")
		s.customer_scope = "Own Records"
		s.save()
		try:
			private = make_customer(f"HM Private {self.uid}")
			r = call(customers.search, self.s2, query=private, kind="customer")
			self.assertEqual(r["data"]["items"], [])
			r = call(customers.get_detail, self.s2, doctype="Customer", name=private)
			self.assertEqual(r["_http_status"], 404)
			r = call(customers.search, self.mgr, query=private, kind="customer")  # managers see all
			self.assertEqual([i["id"] for i in r["data"]["items"]], [private])
			# Once s2 works with that customer, it becomes visible to them.
			make_task(self.s2, future(1), ref=("Customer", private))
			r = call(customers.search, self.s2, query=private, kind="customer")
			self.assertEqual([i["id"] for i in r["data"]["items"]], [private])
		finally:
			frappe.set_user("Administrator")
			s.reload()
			s.customer_scope = "All Records"
			s.save()

	def test_search_by_phone(self):
		r = call(customers.search, self.s1, query="9876543210")
		self.assertIn(self.cust, [i["id"] for i in r["data"]["items"]])

	def test_activity_details_hide_other_employees_work(self):
		frappe.set_user("Administrator")
		mine = make_task(self.s1, future(1), title="mine", ref=("Customer", self.cust2))
		theirs = make_task(self.s2, future(1), title="theirs", ref=("Customer", self.cust2))
		r = call(customers.get_detail, self.s1, doctype="Customer", name=self.cust2)
		ids = [a["id"] for a in r["data"]["activities"]]
		self.assertIn(str(mine.name), ids)
		self.assertNotIn(str(theirs.name), ids)


# ------------------------------------------------------------------ Trials


class TestTrials(Base):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		# The test DB persists between runs: clear this suite's trainers' calendar first.
		for trainer in (cls.trainer, cls.trainer2):
			for b in frappe.get_all("Trial Booking", filters={"trainer": trainer}, pluck="name"):
				frappe.db.set_value("CRM Task", {"hm_trial_booking": b}, "hm_trial_booking", None)
				frappe.delete_doc("Trial Booking", b, force=True, ignore_permissions=True)
			frappe.db.delete("Trainer Slot", {"trainer": trainer})
		frappe.db.commit()

	def test_book_valid_slot_creates_one_calendar_activity(self):
		slot = make_slot(self.trainer, future(4, hour=10))
		r = call(
			trials.book_trial,
			self.s1,
			trainer_slot=slot,
			customer=self.cust,
			booking_type="ERP Trial",
			idempotency_key=f"k-book-1-{self.uid}",
		)
		self.assertTrue(r["success"], r)
		b = r["data"]["booking"]
		self.assertEqual(b["status"], "Booked")
		self.assertEqual(frappe.db.get_value("Trainer Slot", slot, "status"), "Reserved")
		task = frappe.get_doc("CRM Task", b["activity_id"])
		self.assertEqual(task.hm_activity_type, "Trial")
		self.assertEqual(task.assigned_to, self.trainer)  # on the trainer's calendar
		self.assertEqual(frappe.db.count("CRM Task", {"hm_trial_booking": b["id"]}), 1)
		# Trainer notified
		self.assertTrue(
			frappe.db.exists(
				"Mobile Notification",
				{"recipient": self.trainer, "reference_name": b["id"], "event_type": "Trial Update"},
			)
		)
		# On the trainer's calendar, not the salesperson's (she follows it in Trials)
		day = future(4).date().isoformat()
		cal = call(calendar.get_my_day, self.s1, start_date=day, end_date=day)
		self.assertNotIn(b["activity_id"], [a["id"] for a in cal["data"]["items"]])
		cal = call(calendar.get_my_day, self.trainer, start_date=day, end_date=day)
		self.assertIn(b["activity_id"], [a["id"] for a in cal["data"]["items"]])
		self.assertTrue(call(trials.list_bookings, self.s1)["data"]["items"])
		# Idempotent retry returns the same booking
		r2 = call(
			trials.book_trial,
			self.s1,
			trainer_slot=slot,
			customer=self.cust,
			idempotency_key=f"k-book-1-{self.uid}",
		)
		self.assertFalse(r2["data"]["created"])
		self.assertEqual(r2["data"]["booking"]["id"], b["id"])

	def test_conflicting_booking_rejected(self):
		start = future(6, hour=10)
		slot_a = make_slot(self.trainer, start)
		r = call(trials.book_trial, self.s1, trainer_slot=slot_a, customer=self.cust)
		self.assertTrue(r["success"], r)
		r = call(trials.book_trial, self.s2, trainer_slot=slot_a, customer=self.cust2)
		self.assertEqual(r["_http_status"], 409)
		self.assertEqual(r["error"]["code"], "CONFLICT")

	def test_overlapping_slots_cannot_both_be_booked(self):
		start = future(7, hour=10)
		frappe.set_user("Administrator")
		slot_a = make_slot(self.trainer2, start, minutes=60)
		# A booking created on Desk for an overlapping time (e.g. after an availability edit):
		frappe.db.set_value("Trainer Slot", slot_a, "status", "Available")
		r = call(trials.book_trial, self.s1, trainer_slot=slot_a, customer=self.cust)
		self.assertTrue(r["success"], r)
		# Force an overlapping second slot past slot validation to prove the booking check holds.
		slot_b = frappe.get_doc(
			{
				"doctype": "Trainer Slot",
				"trainer": self.trainer2,
				"start_datetime": start + dt.timedelta(minutes=30),
				"end_datetime": start + dt.timedelta(minutes=90),
				"status": "Unavailable",
			}
		).insert(ignore_permissions=True)
		frappe.db.set_value("Trainer Slot", slot_b.name, "status", "Available")
		r = call(trials.book_trial, self.s2, trainer_slot=slot_b.name, customer=self.cust2)
		self.assertEqual(r["_http_status"], 409)

	def test_reschedule_and_cancel_booking(self):
		s1 = make_slot(self.trainer, future(8, hour=10))
		s2 = make_slot(self.trainer, future(8, hour=14))
		b = call(trials.book_trial, self.s1, trainer_slot=s1, customer=self.cust)["data"]["booking"]
		r = call(trials.reschedule_booking, self.s1, booking_id=b["id"], new_slot_id=s2, reason="Client busy")
		self.assertTrue(r["success"], r)
		self.assertEqual(frappe.db.get_value("Trainer Slot", s1, "status"), "Available")
		self.assertEqual(frappe.db.get_value("Trainer Slot", s2, "status"), "Reserved")
		task_due = frappe.db.get_value("CRM Task", b["activity_id"], "due_date")
		self.assertEqual(frappe.utils.get_datetime(task_due), future(8, hour=14))
		r = call(trials.cancel_booking, self.s1, booking_id=b["id"], reason="Not interested")
		self.assertEqual(r["data"]["booking"]["status"], "Cancelled")
		self.assertEqual(frappe.db.get_value("Trainer Slot", s2, "status"), "Available")
		self.assertEqual(frappe.db.get_value("CRM Task", b["activity_id"], "status"), "Canceled")
		# Another salesperson cannot see it
		r = call(trials.get_booking_detail, self.s2, booking_id=b["id"])
		self.assertEqual(r["_http_status"], 404)

	def test_completing_trial_activity_completes_booking(self):
		slot = make_slot(self.trainer, future(9, hour=10))
		b = call(trials.book_trial, self.s1, trainer_slot=slot, customer=self.cust)["data"]["booking"]
		r = call(
			activities.update_status,
			self.s1,
			activity_id=b["activity_id"],
			status="Completed",
			notes="Went well",
		)
		self.assertTrue(r["success"], r)
		self.assertEqual(frappe.db.get_value("Trial Booking", b["id"], "status"), "Completed")

	def test_available_slots_hide_taken(self):
		slot = make_slot(self.trainer, future(10, hour=9))
		free = make_slot(self.trainer, future(10, hour=12))
		call(trials.book_trial, self.s1, trainer_slot=slot, customer=self.cust)
		d = future(10).date().isoformat()
		r = call(trials.get_available_slots, self.s2, start_date=d, end_date=d, trainer=self.trainer)
		ids = [s["id"] for s in r["data"]["items"]]
		self.assertIn(free, ids)
		self.assertNotIn(slot, ids)


# ------------------------------------------------------------------ Notifications


class FakeFCM:
	def __init__(self, result):
		self.result = result
		self.calls = []

	def __call__(self, token, title, body, data=None):
		self.calls.append((token, title, body, data))
		return self.result


class TestNotifications(Base):
	def _register(self, user, device_id, token):
		r = call(devices.register_device, user, device_id=device_id, platform="Android", push_token=token)
		self.assertTrue(r["success"], r)

	def test_assignment_creates_push_with_generic_payload(self):
		frappe.set_user(self.mgr)
		t = make_task(self.s1, future(2), title="Call Dr Secret about pricing", ref=("Customer", self.cust))
		frappe.set_user("Administrator")
		n = frappe.get_all(
			"Mobile Notification",
			filters={"reference_name": str(t.name), "event_type": "Assignment"},
			fields=["recipient", "title", "body"],
		)
		self.assertEqual(len(n), 1)
		self.assertEqual(n[0].recipient, self.s1)
		self.assertNotIn("Secret", n[0].title + n[0].body)  # no customer details in push

	def test_self_created_activity_does_not_notify_self(self):
		r = call(
			activities.create_activity,
			self.s2,
			title="Own",
			reference_doctype="Customer",
			reference_name=self.cust,
			start=future(2).isoformat(),
		)
		self.assertTrue(r["success"], r)
		self.assertFalse(
			frappe.db.exists(
				"Mobile Notification",
				{"reference_name": r["data"]["activity"]["id"], "event_type": "Assignment"},
			)
		)

	def test_reminder_scheduled_and_replaced_on_reschedule(self):
		due = future(3, hour=10)
		t = make_task(self.s1, due, ref=("Customer", self.cust))
		q = frappe.get_all(
			"Mobile Notification",
			filters={"reference_name": str(t.name), "event_type": "Reminder", "status": "Queued"},
			fields=["scheduled_for"],
		)
		# one 15 minutes before, one again at the start time
		self.assertEqual(
			sorted(frappe.utils.get_datetime(x.scheduled_for) for x in q),
			[due - dt.timedelta(minutes=15), due],
		)
		call(
			activities.reschedule_activity,
			self.s1,
			activity_id=t.name,
			new_start_datetime=future(4, hour=11).isoformat(),
		)
		q = frappe.get_all(
			"Mobile Notification",
			filters={"reference_name": str(t.name), "event_type": "Reminder"},
			fields=["status", "scheduled_for"],
		)
		self.assertEqual(sorted(x.status for x in q), ["Cancelled", "Cancelled", "Queued", "Queued"])
		new = future(4, hour=11)
		self.assertEqual(
			sorted(frappe.utils.get_datetime(x.scheduled_for) for x in q if x.status == "Queued"),
			[new - dt.timedelta(minutes=15), new],
		)
		call(activities.update_status, self.s1, activity_id=t.name, status="Completed")
		self.assertFalse(
			frappe.db.exists(
				"Mobile Notification",
				{"reference_name": str(t.name), "event_type": "Reminder", "status": "Queued"},
			)
		)

	def test_due_reminder_is_delivered_by_scheduler(self):
		self._register(self.s1, f"dev-rem-{self.uid}", "tok-rem")
		t = make_task(self.s1, timeutil.now_system() + dt.timedelta(minutes=5), ref=("Customer", self.cust))
		name = frappe.get_all(
			"Mobile Notification",
			filters={"reference_name": str(t.name), "event_type": "Reminder"},
			pluck="name",
			order_by="scheduled_for asc",
		)[0]
		# scheduled_for is "now" (5 min < 15 min lead); make it old enough for the scheduler.
		frappe.db.set_value(
			"Mobile Notification", name, "creation", timeutil.now_system() - dt.timedelta(minutes=1)
		)
		# Older queued notifications left by earlier runs must not use up this batch.
		frappe.db.sql(
			"update `tabMobile Notification` set status='Cancelled' where status='Queued' and name != %s", name
		)
		fake = FakeFCM(fcm.SendResult(ok=True, message_id="projects/x/messages/1"))
		with patch.object(fcm, "send", fake):
			queue.process_due_notifications()
		self.assertEqual(frappe.db.get_value("Mobile Notification", name, "status"), "Sent")
		data = next(c[3] for c in fake.calls if c[3]["notification_id"] == name)
		self.assertEqual(data["route"], f"/activity/{t.name}")  # tap opens the activity
		self.assertEqual(data["reference_doctype"], "CRM Task")

	def _deliver_reminder(self, show_details: int):
		self._register(self.s1, f"dev-det-{self.uid}", "tok-det")
		t = make_task(self.s1, future(3, hour=10), title="Discuss AMC renewal", ref=("Customer", self.cust),
					  description="<p>Bring the <b>new price list</b></p>")
		svc_add = __import__("hipersign_mobile.services.activity_service", fromlist=["add_history"])
		svc_add.add_history(t.name, "Note Added", actor=self.s1, reason="Asked to call after 10")
		name = frappe.db.get_value("Mobile Notification", {"reference_name": str(t.name), "event_type": "Reminder"}, "name")
		frappe.db.set_single_value("Hipersign Mobile Settings", "push_show_details", show_details)
		frappe.clear_document_cache("Hipersign Mobile Settings", "Hipersign Mobile Settings")
		fake = FakeFCM(fcm.SendResult(ok=True, message_id="m-1"))
		try:
			with patch.object(fcm, "send", fake):
				queue.deliver(name)
		finally:
			frappe.db.set_single_value("Hipersign Mobile Settings", "push_show_details", 1)
			frappe.clear_document_cache("Hipersign Mobile Settings", "Hipersign Mobile Settings")
		return fake.calls[-1][1], fake.calls[-1][2]

	def test_reminder_shows_customer_title_and_notes(self):
		title, body = self._deliver_reminder(1)
		cust_name = frappe.db.get_value("Customer", self.cust, "customer_name")
		self.assertIn(cust_name, title)
		self.assertIn("Discuss AMC renewal", body)
		self.assertIn("Notes: Bring the new price list", body)
		self.assertIn("Last note: Asked to call after 10", body)

	def test_reminder_details_can_be_hidden(self):
		title, body = self._deliver_reminder(0)
		self.assertNotIn("AMC", title + body)
		self.assertNotIn("price list", body)

	def test_invalid_token_deactivates_device_without_crash(self):
		self._register(self.s2, f"dev-bad-{self.uid}", "tok-bad")
		n = queue.enqueue(self.s2, "Test", "Hello", "Body", dedup_key=f"test-invalid-token-{self.uid}")
		with patch.object(
			fcm, "send", FakeFCM(fcm.SendResult(ok=False, error_code="UNREGISTERED", invalid_token=True))
		):
			queue.deliver(n)
		self.assertEqual(frappe.db.get_value("Mobile Notification", n, "status"), "Failed")
		self.assertEqual(
			frappe.db.get_value("Mobile Device", {"device_id": f"dev-bad-{self.uid}"}, "is_active"), 0
		)

	def test_transient_failure_retries_with_backoff(self):
		self._register(self.mgr, f"dev-mgr-{self.uid}", "tok-mgr")
		n = queue.enqueue(self.mgr, "Test", "Hello", "Body", dedup_key=f"test-transient-{self.uid}")
		with patch.object(
			fcm, "send", FakeFCM(fcm.SendResult(ok=False, error_code="UNAVAILABLE", transient=True))
		):
			queue.deliver(n)
		row = frappe.db.get_value(
			"Mobile Notification", n, ["status", "attempt_count", "next_attempt_at"], as_dict=True
		)
		self.assertEqual(row.status, "Queued")
		self.assertEqual(row.attempt_count, 1)
		self.assertGreater(row.next_attempt_at, timeutil.now_system())

	def test_no_device_and_push_disabled_are_recorded(self):
		n = queue.enqueue(self.trainer2, "Test", "Hi", "B", dedup_key=f"test-nodevice-{self.uid}")
		queue.deliver(n)
		self.assertEqual(frappe.db.get_value("Mobile Notification", n, "last_error_code"), "NO_DEVICE")

	def test_dedup_key_prevents_duplicate_pushes(self):
		a = queue.enqueue(self.s1, "Test", "Hi", "B", dedup_key=f"test-dedupe-{self.uid}")
		b = queue.enqueue(self.s1, "Test", "Hi", "B", dedup_key=f"test-dedupe-{self.uid}")
		self.assertEqual(a, b)
		self.assertEqual(
			frappe.db.count("Mobile Notification", {"deduplication_key": f"test-dedupe-{self.uid}"}), 1
		)

	def test_device_registration_is_per_user_and_rotates_token(self):
		self._register(self.s1, f"shared-phone-{self.uid}", "tok-1")
		self._register(self.s1, f"shared-phone-{self.uid}", "tok-2")
		self.assertEqual(
			frappe.db.count("Mobile Device", {"device_id": f"shared-phone-{self.uid}", "user": self.s1}), 1
		)
		dev = frappe.get_doc("Mobile Device", {"device_id": f"shared-phone-{self.uid}", "user": self.s1})
		self.assertEqual(dev.get_password("push_token"), "tok-2")
		# Another user signs in on the same phone: the first registration stops receiving pushes.
		self._register(self.s2, f"shared-phone-{self.uid}", "tok-3")
		self.assertEqual(
			frappe.db.get_value(
				"Mobile Device", {"device_id": f"shared-phone-{self.uid}", "user": self.s1}, "is_active"
			),
			0,
		)

	def test_fcm_error_classification(self):
		r = fcm.classify_error(
			404, {"error": {"status": "NOT_FOUND", "details": [{"errorCode": "UNREGISTERED"}]}}
		)
		self.assertTrue(r.invalid_token)
		r = fcm.classify_error(503, {"error": {"status": "UNAVAILABLE"}})
		self.assertTrue(r.transient)
		r = fcm.classify_error(
			400,
			{
				"error": {
					"status": "INVALID_ARGUMENT",
					"message": "The registration token is not a valid FCM registration token",
				}
			},
		)
		self.assertTrue(r.invalid_token)


# ------------------------------------------------------------------ Synchronisation


class TestSync(Base):
	def test_mobile_created_activity_is_saved_in_erpnext(self):
		r = call(
			activities.create_activity,
			self.s1,
			title="Follow up on ERP demo",
			activity_type="Follow-up",
			lead=self.lead,
			start_datetime=future(2, hour=10, minute=30).isoformat(),
			end_datetime=future(2, hour=10, minute=45).isoformat(),
			priority="High",
			idempotency_key=f"sync-create-1-{self.uid}",
		)
		self.assertTrue(r["success"], r)
		task = frappe.get_doc("CRM Task", r["data"]["activity"]["id"])
		self.assertEqual(task.reference_doctype, "CRM Lead")
		self.assertEqual(task.assigned_to, self.s1)
		self.assertEqual(task.priority, "High")
		self.assertTrue(
			frappe.db.exists(
				"ToDo",
				{"reference_type": "CRM Task", "reference_name": str(task.name), "allocated_to": self.s1},
			)
		)

	def test_retry_with_same_key_does_not_duplicate(self):
		kwargs = dict(
			title="Dup test",
			reference_doctype="Customer",
			reference_name=self.cust,
			start=future(2).isoformat(),
			idempotency_key=f"sync-dup-1-{self.uid}",
		)
		a = call(activities.create_activity, self.s1, **kwargs)
		b = call(activities.create_activity, self.s1, **kwargs)
		self.assertTrue(a["data"]["created"])
		self.assertFalse(b["data"]["created"])
		self.assertEqual(a["data"]["activity"]["id"], b["data"]["activity"]["id"])
		self.assertEqual(frappe.db.count("CRM Task", {"hm_idempotency_key": f"sync-dup-1-{self.uid}"}), 1)

	def test_desk_side_change_appears_in_app(self):
		t = make_task(self.s2, future(3, hour=9), ref=("Customer", self.cust))
		frappe.set_user(self.mgr)
		doc = frappe.get_doc("CRM Task", t.name)
		doc.due_date = future(3, hour=17)
		doc.title = "Changed on Desk"
		doc.save(ignore_permissions=True)
		frappe.set_user("Administrator")
		r = call(activities.get_detail, self.s2, activity_id=t.name)
		self.assertEqual(r["data"]["title"], "Changed on Desk")
		self.assertEqual(r["data"]["status"], "Rescheduled")
		self.assertEqual(r["data"]["history"][0]["source"], "Desk")
		# The assignee is told their activity moved.
		self.assertTrue(
			frappe.db.exists(
				"Mobile Notification",
				{"recipient": self.s2, "reference_name": str(t.name), "event_type": "Rescheduled"},
			)
		)

	def test_failed_write_leaves_nothing_behind(self):
		before = frappe.db.count("CRM Task")
		r = call(
			activities.create_activity,
			self.s1,
			title="Bad",
			reference_doctype="Customer",
			reference_name="Does Not Exist",
			start=future(1).isoformat(),
		)
		self.assertEqual(r["_http_status"], 404)
		self.assertFalse(r["success"])
		self.assertEqual(frappe.db.count("CRM Task"), before)

	def test_invalid_input_is_a_validation_error_not_a_crash(self):
		t = make_task(self.s1, future(1), ref=("Customer", self.cust))
		r = call(activities.reschedule_activity, self.s1, activity_id=t.name, new_start_datetime="not-a-date")
		self.assertEqual(r["_http_status"], 422)
		r = call(
			activities.reschedule_activity,
			self.s1,
			activity_id=t.name,
			new_start_datetime=future(2, hour=12).isoformat(),
			new_end_datetime=future(2, hour=11).isoformat(),
		)
		self.assertEqual(r["_http_status"], 422)
		r = call(activities.update_status, self.s1, activity_id=t.name, status="Bogus")
		self.assertEqual(r["_http_status"], 422)
