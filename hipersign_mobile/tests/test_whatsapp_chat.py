from __future__ import annotations

import datetime as dt
import json
import random
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils import now_datetime

try:
	from frappe.tests import IntegrationTestCase as TestCase
except ImportError:  # Frappe v15
	from frappe.tests.utils import FrappeTestCase as TestCase

from hipersign_mobile.api import whatsapp as api
from hipersign_mobile.services import whatsapp_chat as wc
from hipersign_mobile.tests.utils import call, make_lead, make_user

SEND = "frappe_whatsapp.frappe_whatsapp.doctype.whatsapp_message.whatsapp_message.make_post_request"


def _num() -> str:
	return "9198" + "".join(random.choice("0123456789") for _ in range(8))


def _incoming(number: str, text="Hi", minutes_ago=0, kind="text"):
	doc = frappe.get_doc(
		{
			"doctype": "WhatsApp Message",
			"type": "Incoming",
			"from": number,
			"message": text,
			"content_type": kind,
			"message_id": "wamid.in" + frappe.generate_hash(length=8),
			"profile_name": "Cust Profile",
			"whatsapp_account": "HM Test WA",
		}
	).insert(ignore_permissions=True)
	if minutes_ago:
		when = now_datetime() - dt.timedelta(minutes=minutes_ago)
		frappe.db.set_value("WhatsApp Message", doc.name, "creation", when, update_modified=False)
		frappe.db.set_value(wc.CHAT, {"phone": number}, "last_incoming_at", when, update_modified=False)
	return doc


def _resp(status=200, body=None):
	r = MagicMock()
	r.status_code = status
	r.json.return_value = body or {}
	return r


class TestWhatsAppChat(TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.sales1 = make_user("sales1")
		cls.sales2 = make_user("sales2")
		cls.manager = make_user("manager")
		if not frappe.db.exists("WhatsApp Account", "HM Test WA"):
			frappe.get_doc(
				{
					"doctype": "WhatsApp Account",
					"account_name": "HM Test WA",
					"token": "test-token",
					"url": "https://graph.facebook.com",
					"version": "v21.0",
					"phone_id": "1234567890",
					"business_id": "99",
					"webhook_verify_token": "x",
					"is_default_incoming": 1,
					"is_default_outgoing": 1,
				}
			).insert(ignore_permissions=True)
		for name, params, header in (("hm_greeting-en", "Ravi", ""), ("hm_promo-en", "", "IMAGE")):
			if not frappe.db.exists("WhatsApp Templates", name):
				d = frappe.new_doc("WhatsApp Templates")
				d.update(
					{
						"template_name": name.split("-")[0],
						"actual_name": name.split("-")[0],
						"language_code": "en",
						"category": "UTILITY",
						"status": "APPROVED",
						"template": "Hello {{1}}, thanks for your interest." if params else "Big offer!",
						"sample_values": params,
						"header_type": header,
					}
				)
				d.name = name
				d.db_insert()
		frappe.db.set_single_value(
			"Hipersign Mobile Settings",
			{"enable_whatsapp_chat": 1, "customer_scope": "Own Records", "enable_push": 1},
		)
		frappe.clear_document_cache("Hipersign Mobile Settings", "Hipersign Mobile Settings")
		frappe.db.commit()

	@classmethod
	def tearDownClass(cls):
		frappe.db.set_single_value("Hipersign Mobile Settings", {"customer_scope": "All Records"})
		frappe.clear_document_cache("Hipersign Mobile Settings", "Hipersign Mobile Settings")
		frappe.db.commit()
		super().tearDownClass()

	def setUp(self):
		self.mine, self.theirs, self.new = _num(), _num(), _num()
		self.lead_mine = make_lead(f"HM WA Mine {self.mine}", "+" + self.mine, owner=self.sales1)
		self.lead_theirs = make_lead(f"HM WA Theirs {self.theirs}", "+" + self.theirs, owner=self.sales2)

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _chats(self, user):
		r = call(api.chats, user)
		self.assertTrue(r["success"], r)
		return {c["key"] for c in r["data"]["items"]}

	def test_incoming_builds_chat_and_notifies_owner(self):
		_incoming(self.mine, "Hello, price?")
		chat = frappe.get_doc(wc.CHAT, {"phone": self.mine})
		self.assertEqual((chat.party_doctype, chat.party, chat.owner_user), ("CRM Lead", self.lead_mine, self.sales1))
		self.assertEqual(chat.unread_count, 1)
		self.assertEqual(chat.last_message, "Hello, price?")
		self.assertTrue(wc.window_open(chat))
		got = frappe.get_all(
			"Mobile Notification",
			filters={"reference_doctype": wc.CHAT, "reference_name": chat.name},
			fields=["recipient", "event_type", "body"],
		)
		self.assertEqual([(g.recipient, g.event_type) for g in got], [(self.sales1, "WhatsApp")])

	def test_new_number_notifies_everyone_who_can_see_it(self):
		_incoming(self.new, "Who is this?")
		chat = frappe.get_doc(wc.CHAT, {"phone": self.new})
		self.assertFalse(chat.party)
		people = set(wc.recipients(chat))
		self.assertTrue({self.sales1, self.sales2, self.manager} <= people)

	def test_visibility(self):
		for n in (self.mine, self.theirs, self.new):
			_incoming(n)
		s1 = self._chats(self.sales1)
		self.assertIn(self.mine, s1)
		self.assertIn(self.new, s1)
		self.assertNotIn(self.theirs, s1)
		self.assertTrue({self.mine, self.theirs, self.new} <= self._chats(self.manager))
		r = call(api.messages, self.sales1, phone="+" + self.theirs)
		self.assertEqual(r["error"]["code"], "NOT_FOUND")

	def test_messages_and_mark_read(self):
		_incoming(self.mine, "First")
		_incoming(self.mine, "Second")
		r = call(api.messages, self.sales1, phone="+" + self.mine)
		self.assertTrue(r["success"], r)
		self.assertEqual([m["text"] for m in r["data"]["messages"]], ["First", "Second"])
		self.assertEqual(r["data"]["chat"]["unread"], 2)
		self.assertTrue(r["data"]["chat"]["window_open"])
		self.assertEqual(call(api.unread_count, self.sales1)["data"]["chats"] >= 1, True)
		call(api.mark_read, self.sales1, phone="+" + self.mine)
		self.assertEqual(frappe.db.get_value(wc.CHAT, {"phone": self.mine}, "unread_count"), 0)

	def test_send_text_needs_open_window(self):
		_incoming(self.mine, "Old message", minutes_ago=60 * 25)
		r = call(api.send_text, self.sales1, phone="+" + self.mine, text="Hello")
		self.assertFalse(r["success"])
		self.assertIn("24 hours", r["error"]["message"])

		_incoming(self.mine, "New message")
		with patch(SEND, return_value={"messages": [{"id": "wamid.out1"}]}) as sent:
			r = call(api.send_text, self.sales1, phone="+" + self.mine, text="Sure, here are the prices")
		self.assertTrue(r["success"], r)
		body = json.loads(sent.call_args.kwargs["data"])
		self.assertEqual((body["to"], body["text"]["body"]), (self.mine, "Sure, here are the prices"))
		m = r["data"]["message"]
		self.assertEqual((m["direction"], m["text"], m["status"]), ("out", "Sure, here are the prices", "sent"))
		doc = frappe.get_doc("WhatsApp Message", m["id"])
		self.assertEqual((doc.reference_doctype, doc.reference_name), ("CRM Lead", self.lead_mine))
		self.assertEqual(r["data"]["chat"]["last_message"], "Sure, here are the prices")

	def test_templates(self):
		r = call(api.templates, self.sales1, phone="+" + self.mine)
		items = {t["id"]: t for t in r["data"]["items"]}
		self.assertEqual(items["hm_greeting-en"]["params"], 1)
		self.assertTrue(items["hm_greeting-en"]["suggested"][0].startswith("HM WA Mine"))
		self.assertEqual(items["hm_promo-en"]["header_type"], "IMAGE")

		# no 24-hour window needed for a template; values must be complete
		r = call(api.send_template, self.sales1, phone="+" + self.mine, template="hm_greeting-en", params="[]")
		self.assertFalse(r["success"])
		with patch(SEND, return_value={"messages": [{"id": "wamid.t1"}]}) as sent:
			r = call(api.send_template, self.sales1, phone="+" + self.mine, template="hm_greeting-en",
					 params=json.dumps(["Ravi"]))
		self.assertTrue(r["success"], r)
		body = json.loads(sent.call_args.kwargs["data"])
		self.assertEqual(body["template"]["components"][0]["parameters"], [{"type": "text", "text": "Ravi"}])
		m = r["data"]["message"]
		self.assertEqual(m["kind"], "template")
		self.assertEqual(m["template_text"], "Hello Ravi, thanks for your interest.")

		# an image-header template needs a picture
		r = call(api.send_template, self.sales1, phone="+" + self.mine, template="hm_promo-en")
		self.assertFalse(r["success"])
		self.assertIn("picture", r["error"]["message"])

	def test_voice_message(self):
		_incoming(self.mine, "Send me a voice note")
		calls = []

		def fake_post(url, **kw):
			calls.append((url, kw))
			if url.endswith("/media"):
				return _resp(200, {"id": "media-1"})
			return _resp(200, {"messages": [{"id": "wamid.voice"}]})

		frappe.set_user(self.sales1)
		t = wc.target("+" + self.mine, wc.Viewer(self.sales1))
		with patch("requests.post", side_effect=fake_post):
			doc = wc.send_media(t, b"\x00\x01fake-audio", "voice.m4a", wc.guess_mime("voice.m4a", None))
		frappe.set_user("Administrator")
		self.assertEqual(calls[0][1]["files"]["file"][2], "audio/mp4")
		payload = json.loads(calls[1][1]["data"])
		self.assertEqual((payload["type"], payload["audio"]), ("audio", {"id": "media-1"}))
		doc = frappe.get_doc("WhatsApp Message", doc.name)
		self.assertEqual((doc.content_type, doc.message_type, doc.message_id), ("audio", "Manual", "wamid.voice"))
		self.assertTrue(doc.attach.startswith("/private/files/"))
		self.assertEqual(frappe.db.get_value(wc.CHAT, {"phone": self.mine}, "last_message"), "🎤 Voice message")

		# the app downloads it through the API
		frappe.set_user(self.sales1)
		frappe.local.response = frappe._dict()
		api.media(message=doc.name)
		frappe.set_user("Administrator")
		self.assertEqual(frappe.local.response.filecontent, b"\x00\x01fake-audio")
		self.assertEqual(frappe.local.response.content_type, "audio/mp4")

	def test_media_rejected_by_whatsapp(self):
		_incoming(self.mine)
		t = wc.target("+" + self.mine, wc.Viewer(self.sales1))
		with patch("requests.post", return_value=_resp(400, {"error": {"message": "Unsupported type"}})):
			with self.assertRaises(wc.WhatsAppSendError):
				wc.send_media(t, b"x", "a.bin", "application/octet-stream")

	def test_capability(self):
		from hipersign_mobile.services import permissions as perms

		self.assertIn("whatsapp", perms.capabilities(self.sales1))
		frappe.db.set_single_value("Hipersign Mobile Settings", "enable_whatsapp_chat", 0)
		frappe.clear_document_cache("Hipersign Mobile Settings", "Hipersign Mobile Settings")
		try:
			self.assertNotIn("whatsapp", perms.capabilities(self.sales1))
		finally:
			frappe.db.set_single_value("Hipersign Mobile Settings", "enable_whatsapp_chat", 1)
			frappe.clear_document_cache("Hipersign Mobile Settings", "Hipersign Mobile Settings")
