"""Team tasks, attachments, trainer calendars, exact trial times, concerns, app suggestions,
call targets and the daily work report."""

from __future__ import annotations

import datetime as dt
import io
from unittest.mock import patch

import frappe
from werkzeug.test import EnvironBuilder
from werkzeug.wrappers import Request

try:
	from frappe.tests import IntegrationTestCase as TestCase
except ImportError:  # Frappe v15
	from frappe.tests.utils import FrappeTestCase as TestCase

from hipersign_mobile.api import attachments, calendar, concerns, development, feedback, reports, targets, trials
from hipersign_mobile.services import call_targets, daily_report, timeutil, trainer_hours
from hipersign_mobile.tests.utils import call, make_customer, make_task, make_user


def _iso(d: dt.datetime) -> str:
	return d.replace(tzinfo=timeutil.system_tz()).isoformat()


def _notes(user: str, event: str) -> list[str]:
	return frappe.get_all(
		"Mobile Notification", filters={"recipient": user, "event_type": event}, pluck="title", order_by="creation asc"
	)


def _next_weekday(nth=0):
	d = dt.date.today() + dt.timedelta(days=3)
	found = -1
	while True:
		if d.weekday() != 6:
			found += 1
			if found == nth:
				return d
		d += dt.timedelta(days=1)


def _with_file(name: str, content: bytes):
	env = EnvironBuilder(method="POST", data={"file": (io.BytesIO(content), name)}).get_environ()
	return patch.object(frappe.local, "request", Request(env), create=True)


class TestTeamFeatures(TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.manager = make_user("manager")
		cls.accounts = make_user("accounts")
		cls.sales = make_user("sales1")
		cls.sales2 = make_user("sales2")
		cls.trainer = make_user("trainer")
		cls.ceo = make_user("ceo")
		cls.hr = make_user("hr")
		cls.caller = make_user("caller")
		cls.customer = make_customer("HM Team Features Customer")
		for d in ("Team Task", "Hipersign Concern", "Hipersign Feedback", "Call Target", "Hipersign Daily Report"):
			frappe.db.delete(d)
		frappe.db.delete("Mobile Notification")
		frappe.db.delete("Mobile Call Log", {"caller": cls.caller})
		for b in frappe.get_all("Trial Booking", filters={"trainer": cls.trainer}, pluck="name"):
			frappe.db.set_value("CRM Task", {"hm_trial_booking": b}, "hm_trial_booking", None)
			frappe.delete_doc("Trial Booking", b, force=True, ignore_permissions=True)
		frappe.db.delete("Trainer Slot", {"trainer": cls.trainer})
		frappe.db.delete("CRM Task", {"assigned_to": ["in", [cls.trainer, cls.caller]]})
		frappe.db.set_single_value("Hipersign Mobile Settings", {
			"auto_trainer_slots": 1, "trainer_working_days": "Mon,Tue,Wed,Thu,Fri,Sat",
			"trainer_day_start": "09:30:00", "trainer_day_end": "18:00:00", "trial_slot_minutes": 60,
			"enable_daily_report": 1, "daily_report_time": "18:30:00",
		})
		frappe.clear_document_cache("Hipersign Mobile Settings", "Hipersign Mobile Settings")
		frappe.db.commit()

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()
		for d in ("Team Task", "Hipersign Concern", "Hipersign Feedback", "Call Target", "Hipersign Daily Report", "Mobile Notification"):
			frappe.db.delete(d)
		frappe.db.delete("Mobile Call Log", {"caller": self.caller})
		frappe.db.commit()

	# ------------------------------------------------------------ team tasks

	def _team_task(self, **kw):
		r = call(development.create_task, self.manager, kind="team", title="Pay GST", team="Accounts",
				 developer=self.accounts, deadline=_iso(timeutil.now_system() + dt.timedelta(hours=30)), **kw)
		self.assertTrue(r["success"], r)
		frappe.db.commit()
		return r["data"]

	def test_team_task_for_anyone_with_alerts(self):
		t = self._team_task()
		self.assertEqual((t["kind"], t["team"], t["developer"]), ("team", "Accounts", self.accounts))
		self.assertTrue(t["id"].startswith("TASK-"))
		self.assertEqual(_notes(self.accounts, "Task"), ["New task"])
		# An accounts person without any app role sees it as their own work.
		r = call(development.list_tasks, self.accounts, kind="team")
		self.assertTrue(r["success"], r)
		self.assertEqual([i["id"] for i in r["data"]["items"]], [t["id"]])
		self.assertIn("Accounts", r["data"]["teams"])
		# ... but can't give work to others, and can't see development tasks.
		r = call(development.create_task, self.accounts, kind="team", title="x", developer=self.sales,
				 deadline=_iso(timeutil.now_system() + dt.timedelta(hours=3)))
		self.assertFalse(r["success"])
		self.assertFalse(call(development.list_tasks, self.accounts, kind="dev")["success"])
		# People list for team tasks = every employee user (managers).
		r = call(development.developers, self.manager, kind="team")
		self.assertIn(self.accounts, [p["id"] for p in r["data"]["items"]])
		# Done -> whoever assigned it is told; overdue engine covers team tasks.
		r = call(development.update_task, self.accounts, kind="team", task_id=t["id"], status="Done")
		self.assertTrue(r["success"], r)
		self.assertIn("Task done", _notes(self.manager, "Task"))

	def test_team_task_deadline_crossed(self):
		from hipersign_mobile.services import development as dev

		t = self._team_task()
		deadline = timeutil.parse_client_datetime(t["deadline"])
		dev.process_alerts(now=deadline + dt.timedelta(minutes=5))
		self.assertIn("Deadline crossed", _notes(self.accounts, "Task"))
		self.assertTrue(frappe.db.get_value("Team Task", t["id"], "deadline_crossed"))

	# ------------------------------------------------------------ attachments

	def test_files_and_voice_notes(self):
		t = self._team_task()
		with _with_file("rec.m4a", b"voice-bytes"):
			r = call(attachments.upload, self.manager, doctype="Team Task", name=t["id"], voice=1)
		self.assertTrue(r["success"], r)
		self.assertEqual(r["data"]["kind"], "audio")
		self.assertTrue(r["data"]["name"].startswith("Voice note"))
		with _with_file("brief.txt", b"Pay before the 20th"):
			r2 = call(attachments.upload, self.accounts, doctype="Team Task", name=t["id"])
			self.assertTrue(r2["success"], r2)
		frappe.db.commit()
		files = call(attachments.list_files, self.accounts, doctype="Team Task", name=t["id"])["data"]["items"]
		self.assertEqual([f["kind"] for f in files], ["audio", "document"])
		# Someone not on the task can't see or add files.
		self.assertFalse(call(attachments.list_files, self.sales, doctype="Team Task", name=t["id"])["success"])
		with _with_file("x.txt", b"x"):
			self.assertFalse(call(attachments.upload, self.sales, doctype="Team Task", name=t["id"])["success"])
		# Raw bytes for the app to play / open.
		frappe.set_user(self.accounts)
		attachments.file(file_id=files[0]["id"])
		frappe.set_user("Administrator")
		self.assertEqual(frappe.local.response.filecontent, b"voice-bytes")

	# ------------------------------------------------------------ trainings

	def _book(self, day, start, minutes, user=None):
		slot = trainer_hours.slot_id(self.trainer, dt.datetime.combine(day, start), minutes)
		return call(trials.book_trial, user or self.sales, trainer_slot=slot, customer=self.customer, booking_type="Training")

	def test_exact_time_and_trainer_calendar(self):
		day = _next_weekday(4)
		# The app checks a hand-picked time first.
		r = call(trials.check_time, self.sales, trainer=self.trainer,
				 start=_iso(dt.datetime.combine(day, dt.time(10, 15))), end=_iso(dt.datetime.combine(day, dt.time(11, 30))))
		self.assertTrue(r["success"], r)
		self.assertIsNone(r["data"]["note"])
		r = self._book(day, dt.time(10, 15), 75)
		self.assertTrue(r["success"], r)
		b = r["data"]["booking"]
		self.assertEqual((b["start"][11:16], b["end"][11:16]), ("10:15", "11:30"))
		frappe.db.commit()
		from hipersign_mobile.api import customers

		d = call(customers.get_detail, self.sales, doctype="Customer", name=self.customer)["data"]
		self.assertEqual(d["journey"]["stage"]["label"], "Training booked")
		self.assertIn("Trainer", d["journey"]["stage"]["detail"])
		# Overlapping time with the same trainer is refused (check and booking).
		r = call(trials.check_time, self.sales2, trainer=self.trainer,
				 start=_iso(dt.datetime.combine(day, dt.time(11, 0))), end=_iso(dt.datetime.combine(day, dt.time(12, 0))))
		self.assertFalse(r["success"])
		self.assertFalse(self._book(day, dt.time(11, 0), 60, self.sales2)["success"])
		# Outside working hours is allowed, with a note.
		r = call(trials.check_time, self.sales, trainer=self.trainer,
				 start=_iso(dt.datetime.combine(day, dt.time(18, 30))), end=_iso(dt.datetime.combine(day, dt.time(19, 45))))
		self.assertTrue(r["success"], r)
		self.assertIn("working hours", r["data"]["note"])
		# The training is on the trainer's calendar (booked by someone else) and she can open it.
		r = call(calendar.get_my_day, self.trainer, start_date=str(day), end_date=str(day))
		self.assertTrue(r["success"], r)
		self.assertIn(b["activity_id"], [i["id"] for i in r["data"]["items"]])
		from hipersign_mobile.api import activities

		self.assertTrue(call(activities.get_detail, self.trainer, activity_id=b["activity_id"])["success"])
		# ... and in her trials list.
		r = call(trials.list_bookings, self.trainer, status="Active")
		self.assertIn(b["id"], [i["id"] for i in r["data"]["items"]])
		# Files on the training: the salesperson adds, the trainer sees.
		with _with_file("agenda.txt", b"Day 1: sales module"):
			self.assertTrue(call(attachments.upload, self.sales, doctype="Trial Booking", name=b["id"])["success"])
		self.assertEqual(len(call(attachments.list_files, self.trainer, doctype="Trial Booking", name=b["id"])["data"]["items"]), 1)

	# ------------------------------------------------------------ concerns

	def test_concerns_are_private(self):
		r = call(concerns.create_concern, self.sales, audience="CEO Only", category="Manager", subject="Shouting in meetings", details="...")
		self.assertTrue(r["success"], r)
		ceo_only = r["data"]["id"]
		r = call(concerns.create_concern, self.sales, audience="HR & Managers", category="Workload", subject="Too many calls")
		hr = r["data"]["id"]
		frappe.db.commit()
		self.assertEqual(_notes(self.ceo, "Concern"), ["New concern (Manager)", "New concern (Workload)"])
		self.assertEqual(_notes(self.hr, "Concern"), ["New concern (Workload)"])
		self.assertEqual(_notes(self.manager, "Concern"), ["New concern (Workload)"])

		def inbox(user):
			r = call(concerns.list_concerns, user, scope="inbox")
			return [i["id"] for i in r["data"]["items"]] if r["success"] else None

		self.assertEqual(set(inbox(self.ceo)), {ceo_only, hr})
		self.assertEqual(inbox(self.hr), [hr])
		self.assertEqual(inbox(self.manager), [hr])
		self.assertIsNone(inbox(self.sales2))
		self.assertFalse(call(concerns.get_concern, self.hr, concern_id=ceo_only)["success"])
		self.assertFalse(call(concerns.get_concern, self.sales2, concern_id=hr)["success"])
		mine = call(concerns.list_concerns, self.sales, scope="mine")["data"]["items"]
		self.assertEqual({i["id"] for i in mine}, {ceo_only, hr})
		# Desk permissions follow the same rule.
		frappe.set_user(self.hr)
		self.assertEqual(frappe.get_list("Hipersign Concern", pluck="name"), [hr])
		frappe.set_user("Administrator")
		# The CEO answers; the person is told.
		r = call(concerns.respond, self.ceo, concern_id=ceo_only, response="I'll talk to him today.")
		self.assertTrue(r["success"], r)
		self.assertEqual(r["data"]["status"], "In Review")
		self.assertIn("Your concern: In Review", _notes(self.sales, "Concern"))
		self.assertFalse(call(concerns.respond, self.hr, concern_id=ceo_only, response="x")["success"])

	# ------------------------------------------------------------ app suggestions

	def test_feedback_board(self):
		r = call(feedback.create_feedback, self.sales, kind="Bug", title="Calendar crashes on Sunday", details="steps...")
		self.assertTrue(r["success"], r)
		fid = r["data"]["id"]
		self.assertEqual((r["data"]["votes"], r["data"]["voted"]), (1, True))
		frappe.db.commit()
		self.assertIn("App bug: Calendar crashes on Sunday", _notes(self.manager, "Feedback"))
		r = call(feedback.vote, self.accounts, feedback_id=fid)
		self.assertEqual(r["data"]["votes"], 2)
		items = call(feedback.list_feedback, self.accounts)["data"]["items"]
		self.assertEqual(items[0]["id"], fid)
		self.assertFalse(call(feedback.update_feedback, self.accounts, feedback_id=fid, status="Done")["success"])
		r = call(feedback.update_feedback, self.manager, feedback_id=fid, status="In Progress", reply="Fixing it this week")
		self.assertTrue(r["success"], r)
		self.assertIn("Your suggestion: In Progress", _notes(self.sales, "Feedback"))

	# ------------------------------------------------------------ call targets

	def _log_calls(self, n, at: dt.datetime, connected=1, call_type="New"):
		for i in range(n):
			frappe.get_doc({
				"doctype": "Mobile Call Log", "caller": self.caller, "call_datetime": at + dt.timedelta(minutes=i),
				"direction": "Outgoing", "connected": connected, "duration_seconds": 30, "phone": f"+91900{connected}{'N' if call_type == 'New' else 'F'}{i:05d}".replace("N", "1").replace("F", "2"),
				"source": "Hipersign", "sync_key": f"t-{at.isoformat()}-{i}-{call_type}-{connected}", "call_type": call_type,
			}).insert(ignore_permissions=True)

	def test_new_connected_calls_and_meeting_target(self):
		r = call(targets.save_target, self.manager, user=self.caller, daily_target=5, count_connected_only=1,
				 count_new_only=1, meeting_target=2, check_times="17:00")
		self.assertTrue(r["success"], r)
		day = _next_weekday(6)
		frappe.db.delete("CRM Task", {"title": "Meet X"})
		self._log_calls(3, dt.datetime.combine(day, dt.time(10, 0)))  # new, connected
		self._log_calls(2, dt.datetime.combine(day, dt.time(11, 0)), connected=0)  # new, not connected
		self._log_calls(4, dt.datetime.combine(day, dt.time(12, 0)), call_type="Follow-up")  # follow-ups
		make_task(self.manager, dt.datetime.combine(day, dt.time(15, 0)), title="Meet X", ref=("Customer", self.customer),
				  hm_activity_type="Meeting")
		frappe.db.set_value("CRM Task", {"title": "Meet X"}, {"creation": dt.datetime.combine(day, dt.time(10, 30)), "owner": self.caller})
		frappe.db.commit()
		p = call_targets.progress(self.caller, day)
		self.assertEqual((p["made"], p["counted"], p["remaining"], p["unit"]), (9, 3, 2, "new connected calls"))
		self.assertEqual((p["meetings"], p["meetings_remaining"]), (1, 1))
		# 2 new numbers not connected that day (the follow-ups / connected ones don't count).
		self.assertEqual(p["new_not_reached_today"], 2)
		call_targets.check(frappe.get_doc("Call Target", self.caller), now=dt.datetime.combine(day, dt.time(17, 1)))
		body = frappe.db.get_value("Mobile Notification", {"recipient": self.caller, "event_type": "Call Target", "title": ["like", "2 more%"]}, "body")
		self.assertIn("Meetings: 1 of 2", body)
		frappe.db.delete("CRM Task", {"title": "Meet X"})
		frappe.db.commit()

	def test_call_target_alerts(self):
		self.assertFalse(call(targets.save_target, self.caller, user=self.caller, daily_target=10)["success"])
		r = call(targets.save_target, self.manager, user=self.caller, daily_target=10, check_times="17:00, 16:00",
				 idle_minutes=30, work_start="09:30", work_end="18:00")
		self.assertTrue(r["success"], r)
		frappe.db.commit()
		self.assertEqual(frappe.db.get_value("Call Target", self.caller, "check_times"), "16:00,17:00")
		day = _next_weekday(5)
		self._log_calls(4, dt.datetime.combine(day, dt.time(10, 0)))
		p = call_targets.progress(self.caller, day)
		self.assertEqual((p["made"], p["remaining"]), (4, 6))
		t = frappe.get_doc("Call Target", self.caller)
		call_targets.check(t, now=dt.datetime.combine(day, dt.time(16, 2)))
		self.assertIn("6 more calls needed", _notes(self.caller, "Call Target"))
		# The idle alert at 16:02 too: last call at 10:03 and nothing on the calendar.
		self.assertTrue(any(x.startswith("No calls for") for x in _notes(self.caller, "Call Target")))
		# Not while a follow-up is going on.
		frappe.db.delete("Mobile Notification", {"recipient": self.caller})
		make_task(self.caller, dt.datetime.combine(day, dt.time(16, 30)), ref=("Customer", self.customer),
				  hm_end_datetime=dt.datetime.combine(day, dt.time(17, 0)))
		call_targets.check(t, now=dt.datetime.combine(day, dt.time(16, 40)))
		self.assertEqual(_notes(self.caller, "Call Target"), [])
		self.assertTrue(call(targets.my_target, self.caller)["success"])
		st = call_targets.idle_state(t, call_targets.progress(self.caller, day, t), dt.datetime.combine(day, dt.time(16, 2)),
									 dt.datetime.combine(day, dt.time(16, 2)))
		self.assertTrue(st and st["minutes"] >= 30)
		listing = call(targets.list_targets, self.manager)["data"]["items"]
		self.assertIn(self.caller, [x["user"] for x in listing])

	# ------------------------------------------------------------ daily report

	def test_daily_report(self):
		now = timeutil.now_system()
		day = now.date()
		done = make_task(self.sales2, now.replace(hour=10, minute=0), title="Call Ravi", ref=("Customer", self.customer))
		frappe.db.set_value("CRM Task", done.name, "status", "Done")
		make_task(self.sales2, now.replace(hour=15, minute=0), title="Meet Anu", ref=("Customer", self.customer))
		frappe.db.commit()
		r = call(reports.get_report, self.sales2, date=str(day))
		self.assertTrue(r["success"], r)
		mine = {i["title"]: i["result"] for i in r["data"]["items"]}
		self.assertEqual(mine["Call Ravi"], "Done")
		self.assertEqual(mine["Meet Anu"], "Not done")
		self.assertFalse(call(reports.get_report, self.sales2, user=self.sales)["success"])
		self.assertTrue(call(reports.get_report, self.manager, user=self.sales2)["success"])
		team = call(reports.team_report, self.manager, date=str(day))
		self.assertIn(self.sales2, [x["user"] for x in team["data"]["items"]])
		self.assertFalse(call(reports.team_report, self.sales2)["success"])
		daily_report.send_all(day)
		name = frappe.db.get_value("Hipersign Daily Report", {"user": self.sales2, "report_date": day}, "name")
		self.assertTrue(name)
		self.assertGreaterEqual(len(frappe.get_doc("Hipersign Daily Report", name).items), 2)
		self.assertTrue(_notes(self.sales2, "Daily Report"))
		self.assertTrue(_notes(self.manager, "Daily Report"))
