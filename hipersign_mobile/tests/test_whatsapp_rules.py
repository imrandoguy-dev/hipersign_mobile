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

from hipersign_mobile.api import calls, customers
from hipersign_mobile.services import whatsapp_rules as wr
from hipersign_mobile.tests.utils import call, make_customer, make_user

NUM = "+91 98950 61237"
E164 = "+919895061237"


def rule(name, trigger, parties="Customers, Leads and Deals", enabled=1, template="follow_up", parameters=None):
	return frappe._dict(
		name=name, trigger=trigger, parties=parties, enabled=enabled, whatsapp_template=template,
		parameters=parameters, repeat_after_hours=24, only_once=0,
	)


def _at(minutes_ago: int) -> str:
	return (dt.datetime.now(dt.UTC) - dt.timedelta(minutes=minutes_ago)).isoformat()


class TestWhatsAppRules(TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.caller = make_user("sales1")
		cls.customer = make_customer("HM WA Customer", NUM)

	def setUp(self):
		frappe.db.delete("Mobile Call Log", {"phone": E164})
		frappe.db.commit()
		self.p = uuid.uuid4().hex[:6]
		self.rules = [
			rule("r-any", wr.AFTER_ANY),
			rule("r-missed", wr.AFTER_NOT_CONNECTED),
			rule("r-conn", wr.AFTER_CONNECTED),
			rule("r-leads", wr.AFTER_ANY, parties="Leads only"),
			rule("r-off", wr.AFTER_ANY, enabled=0),
			rule("r-notpl", wr.AFTER_ANY, template=None),
			rule("r-lead", wr.NEW_LEAD),
		]

	def _sync(self, items):
		with patch.object(wr, "_all_rules", return_value=self.rules), patch.object(frappe, "enqueue") as enq:
			r = call(calls.sync, self.caller, calls=json.dumps(items))
		frappe.db.commit()
		self.assertTrue(r["success"], r)
		return sorted(c.kwargs["rule"] for c in enq.call_args_list), enq

	def test_not_connected_call_queues_matching_rules(self):
		rules, enq = self._sync([{"key": f"{self.p}1", "number": NUM, "started_at": _at(5), "duration": 0,
								  "direction": "outgoing"}])
		self.assertEqual(rules, ["r-any", "r-missed"])
		kw = enq.call_args_list[0].kwargs
		self.assertEqual((kw["party_doctype"], kw["party"], kw["phone"]), ("Customer", self.customer, E164))
		self.assertTrue(kw["enqueue_after_commit"])

	def test_connected_call(self):
		rules, _ = self._sync([{"key": f"{self.p}2", "number": NUM, "started_at": _at(5), "duration": 70,
								"direction": "outgoing"}])
		self.assertEqual(rules, ["r-any", "r-conn"])

	def test_incoming_and_old_calls_send_nothing(self):
		rules, _ = self._sync([
			{"key": f"{self.p}3", "number": NUM, "started_at": _at(5), "duration": 70, "direction": "incoming"},
			{"key": f"{self.p}4", "number": NUM, "started_at": _at(60 * 5), "duration": 0, "direction": "outgoing"},
		])
		self.assertEqual(rules, [])

	def test_new_lead_rule(self):
		with patch.object(wr, "_all_rules", return_value=self.rules), patch.object(frappe, "enqueue") as enq:
			r = call(customers.create_lead, self.caller, phone=f"+91 98950 7{self.p[:4].translate(str.maketrans('abcdef', '123456'))}",
					 name=f"WA Lead {self.p}")
		self.assertTrue(r["success"], r)
		sent = [c.kwargs for c in enq.call_args_list]
		self.assertEqual([k["rule"] for k in sent], ["r-lead"])
		self.assertEqual(sent[0]["party_doctype"], "CRM Lead")
		frappe.db.rollback()

	def test_parameters(self):
		tpl = frappe._dict(sample_values="Thoufeek,Monday")
		with patch.object(wr, "context", return_value={"name": "Amal Traders", "salesperson": "Karuppa",
														"first_name": "Amal"}):
			self.assertEqual(wr.build_parameters("", tpl, "Customer", "x", E164, None, None),
							 ["Amal Traders", "Amal Traders"])
			self.assertEqual(wr.build_parameters("Hi {first_name}, {salesperson}, extra", tpl, "Customer", "x", E164, None, None),
							 ["Hi Amal", "Karuppa"])
			self.assertEqual(wr.build_parameters("{salesperson}", tpl, "Customer", "x", E164, None, None),
							 ["Karuppa", "Amal Traders"])
			self.assertIsNone(wr.build_parameters("{name}", frappe._dict(sample_values=""), "Customer", "x", E164, None, None))

	def test_context_values(self):
		ctx = wr.context("Customer", self.customer, E164, self.caller, "2026-10-14 15:05:00")
		self.assertEqual(ctx["name"], "HM WA Customer")
		self.assertEqual(ctx["first_name"], "HM")
		self.assertEqual(ctx["date"], "14 Oct 2026")
		self.assertEqual(ctx["time"], "3:05 PM")

	def test_send_is_noop_without_frappe_whatsapp(self):
		with patch.object(wr, "available", return_value=False):
			self.assertIsNone(wr.send("r-any", "Customer", self.customer, E164))

	def test_once_per_customer_also_matches_phone(self):
		r = frappe._dict(name="r-mkt", only_once=1, repeat_after_hours=24)
		calls_seen = []

		def fake_get_all(doctype, filters=None, **kw):
			calls_seen.append(filters)
			# Sent earlier to the same number while it was a lead.
			return ["WA-1"] if "to" in filters else []

		with patch.object(frappe, "get_all", side_effect=fake_get_all):
			self.assertTrue(wr.already_sent(r, "CRM Deal", "DEAL-1", E164))
		self.assertNotIn("creation", calls_seen[0])  # only-once: no time window
		self.assertEqual(calls_seen[1]["to"], ["in", [E164[1:], E164]])

		with patch.object(frappe, "get_all", return_value=[]):
			self.assertFalse(wr.already_sent(r, "CRM Deal", "DEAL-1", E164))
		r.only_once, r.repeat_after_hours = 0, 0
		self.assertFalse(wr.already_sent(r, "CRM Deal", "DEAL-1", E164))

	def test_after_call_screen_status(self):
		frappe.cache.delete_value(wr._outcome_key("Customer", self.customer))
		started = _at(5)
		self._sync([{"key": f"{self.p}9", "number": NUM, "started_at": started, "duration": 0, "direction": "outgoing"}])
		with patch.object(wr, "_all_rules", return_value=self.rules):
			r = call(calls.whatsapp_status, self.caller, doctype="Customer", name=self.customer, since=started)
		self.assertTrue(r["success"], r)
		self.assertTrue(r["data"]["enabled"])
		self.assertEqual(sorted(i["rule"] for i in r["data"]["items"]), ["r-any", "r-missed"])
		self.assertEqual({i["status"] for i in r["data"]["items"]}, {"queued"})
		self.assertEqual(r["data"]["items"][0]["title"], "Follow up")

		# the background job's outcome replaces "queued"
		wr.record("r-any", "Customer", self.customer, "skipped", template="follow_up")
		with patch.object(wr, "available", return_value=False):
			wr.send("r-missed", "Customer", self.customer, E164)
		items = {i["rule"]: i for i in wr.outcomes("Customer", self.customer)}
		self.assertEqual(items["r-any"]["status"], "skipped")
		self.assertEqual(items["r-missed"]["status"], "failed")
		self.assertIn("not set up", items["r-missed"]["error"])

	def test_status_only_for_rules_that_apply(self):
		with patch.object(wr, "_all_rules", return_value=[rule("x", wr.AFTER_ANY, parties="Leads only")]):
			self.assertFalse(wr.call_rules_enabled("Customer"))
			self.assertTrue(wr.call_rules_enabled("CRM Lead"))
