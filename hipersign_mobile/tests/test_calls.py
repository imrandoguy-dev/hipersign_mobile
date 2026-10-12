from __future__ import annotations

import datetime as dt
import json
import uuid

import frappe

try:
	from frappe.tests import IntegrationTestCase as TestCase
except ImportError:  # Frappe v15
	from frappe.tests.utils import FrappeTestCase as TestCase

from hipersign_mobile.api import calls
from hipersign_mobile.hipersign_mobile.report.calling_activity import calling_activity as report
from hipersign_mobile.tests.utils import call, make_customer, make_user

NUM = "+91 98950 55001"
E164 = "+919895055001"


def _at(minutes_ago: int) -> str:
	return (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=minutes_ago)).isoformat()


def _call(key, minutes_ago, duration, number=NUM, direction="outgoing", via_app=False):
	return {"key": key, "number": number, "started_at": _at(minutes_ago), "duration": duration,
			"direction": direction, "via_app": via_app}


class TestCalls(TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.caller = make_user("sales1")
		cls.other = make_user("sales2")
		cls.manager = make_user("manager")
		make_customer("HM Call Customer", NUM)

	def setUp(self):
		frappe.db.delete("Mobile Call Log", {"phone": ["in", [E164, "+919895055999"]]})
		frappe.db.commit()
		self.p = uuid.uuid4().hex[:6]

	def _sync(self, user, items):
		r = call(calls.sync, user, calls=json.dumps(items))
		frappe.db.commit()
		return r

	def test_new_until_connected_then_follow_up(self):
		r = self._sync(self.caller, [
			_call(f"{self.p}1", 50, 0),    # not answered -> New (attempt)
			_call(f"{self.p}2", 40, 95),   # first connected -> New
			_call(f"{self.p}3", 30, 0),    # after a connected call -> Follow-up (not connected)
			_call(f"{self.p}4", 20, 60),   # Follow-up, connected
		])
		self.assertTrue(r["success"], r)
		self.assertEqual(r["data"]["saved"], 4)
		logs = frappe.get_all("Mobile Call Log", filters={"phone": E164}, fields=["call_type", "connected", "party", "source"],
							  order_by="call_datetime asc")
		self.assertEqual([(l.call_type, l.connected) for l in logs], [("New", 0), ("New", 1), ("Follow-up", 0), ("Follow-up", 1)])
		self.assertEqual(logs[0].party, "HM Call Customer")
		self.assertEqual(logs[0].source, "Phone App")
		# Re-sending the same calls stores nothing twice.
		again = self._sync(self.caller, [_call(f"{self.p}1", 50, 0)])
		self.assertEqual(again["data"]["saved"], 0)

	def test_follow_up_counts_company_wide_and_late_sync_promotes(self):
		self._sync(self.other, [_call(f"{self.p}a", 30, 0)])  # other caller, unanswered: New
		self._sync(self.caller, [_call(f"{self.p}b", 60, 40)])  # earlier connected call synced later
		later = frappe.get_value("Mobile Call Log", {"sync_key": f"{self.other}:{self.p}a"}, "call_type")
		self.assertEqual(later, "Follow-up")

	def test_personal_calls_are_never_stored(self):
		r = self._sync(self.caller, [_call(f"{self.p}x", 10, 30, number="+91 98950 55999")])
		self.assertEqual(r["data"], {"saved": 0, "skipped": 1})
		# ...unless it was made from Hipersign (e.g. dial pad to a new number).
		r = self._sync(self.caller, [_call(f"{self.p}y", 10, 30, number="+91 98950 55999", via_app=True)])
		self.assertEqual(r["data"]["saved"], 1)
		self.assertEqual(frappe.db.get_value("Mobile Call Log", {"sync_key": f"{self.caller}:{self.p}y"}, "source"), "Hipersign")

	def test_visibility(self):
		self._sync(self.caller, [_call(f"{self.p}v", 5, 20)])
		name = frappe.db.get_value("Mobile Call Log", {"sync_key": f"{self.caller}:{self.p}v"})
		frappe.set_user(self.other)
		try:
			self.assertNotIn(name, frappe.get_list("Mobile Call Log", pluck="name"))
		finally:
			frappe.set_user("Administrator")
		frappe.set_user(self.manager)
		try:
			self.assertIn(name, frappe.get_list("Mobile Call Log", pluck="name", limit=1000))
		finally:
			frappe.set_user("Administrator")

	def test_report_and_today_summary(self):
		self._sync(self.caller, [
			_call(f"{self.p}r1", 30, 0),
			_call(f"{self.p}r2", 20, 120),
			_call(f"{self.p}r3", 10, 60),
			_call(f"{self.p}r4", 5, 45, direction="incoming"),
		])
		today = frappe.utils.today()
		cols, rows, _msg, chart, summary = report.execute(
			{"from_date": today, "to_date": today, "caller": self.caller, "group_by": "Day and Caller"}
		)
		row = rows[0]
		self.assertGreaterEqual(row["total"], 3)
		self.assertEqual(row["total"], row["new_calls"] + row["followups"])
		self.assertGreaterEqual(row["connected"], 2)
		self.assertGreaterEqual(row["talk_time"], 180)
		self.assertGreaterEqual(row["incoming"], 1)
		self.assertEqual(row["caller_name"], frappe.utils.get_fullname(self.caller))
		self.assertIn("Calls Made", [s["label"] for s in summary])
		self.assertEqual(len(chart["data"]["labels"]), 1)
		for g in ("Caller", "Day"):
			self.assertTrue(report.execute({"from_date": today, "to_date": today, "group_by": g})[1])
		s = call(calls.today_summary, self.caller)
		self.assertGreaterEqual(s["data"]["total"], 3)
