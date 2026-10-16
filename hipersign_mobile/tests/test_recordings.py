from __future__ import annotations

import datetime
import io
import uuid

import frappe
from werkzeug.datastructures import FileStorage

try:
	from frappe.tests import IntegrationTestCase as TestCase
except ImportError:  # Frappe v15
	from frappe.tests.utils import FrappeTestCase as TestCase

from hipersign_mobile.api import recordings
from hipersign_mobile.services import permissions as perms
from hipersign_mobile.tests.utils import call, future, make_customer, make_lead, make_task, make_user


class _FakeRequest:
	host = "test.local"
	host_url = "http://test.local/"
	scheme = "http"

	def __init__(self, files=None):
		self.headers = {}
		self.files = files or {}


def _audio(name=None, size=2048):
	name = name or f"Call_9847012345_{uuid.uuid4().hex[:8]}.m4a"  # phones name each recording uniquely
	return FileStorage(stream=io.BytesIO(b"\x00\x00\x00\x18ftypM4A " + b"a" * size), filename=name)


def upload(user, file=None, **kw):
	params = {
		"party_doctype": "Customer",
		"party": "HM Rec Customer",
		"phone": "98470 12345",
		"started_at": datetime.datetime.now(datetime.UTC).isoformat(),  # newest first in lists
		"duration": 133,
		"client_key": uuid.uuid4().hex,
	}
	params.update(kw)
	frappe.local.request = _FakeRequest({"file": file} if file is not None else {})
	try:
		out = call(recordings.upload, user, **params)
		frappe.db.commit()  # like a real request; a later failing call rolls back only itself
		return out
	finally:
		frappe.local.request = None


def _set(**values):
	s = frappe.get_single("Hipersign Mobile Settings")
	s.update(values)
	s.save(ignore_permissions=True)
	frappe.db.commit()


class TestRecordings(TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.sales1 = make_user("sales1")
		cls.sales2 = make_user("sales2")
		cls.manager = make_user("manager")
		make_customer("HM Rec Customer", "+91 98470 12345")
		_set(enable_call_recordings=1, max_recording_mb=1)

	@classmethod
	def tearDownClass(cls):
		_set(enable_call_recordings=1, max_recording_mb=50)
		super().tearDownClass()

	def test_upload_links_customer_activity_and_is_idempotent(self):
		task = make_task(self.sales1, future(), ref=("Customer", "HM Rec Customer"))
		key = uuid.uuid4().hex
		r = upload(self.sales1, _audio(), activity=task.name, client_key=key)
		self.assertTrue(r["success"], r)
		d = r["data"]
		self.assertEqual(d["party_name"], "HM Rec Customer")
		self.assertEqual(d["activity"], str(task.name))
		self.assertEqual(d["duration_seconds"], 133)
		self.assertEqual(d["phone"], "+919847012345")
		self.assertEqual(d["ext"], "m4a")

		doc = frappe.get_doc("Call Recording", d["id"])
		f = frappe.get_doc("File", {"file_url": doc.audio_file})
		self.assertTrue(f.is_private)
		self.assertEqual(f.attached_to_name, doc.name)
		self.assertTrue(
			frappe.db.exists(
				"Comment", {"reference_doctype": "Customer", "reference_name": "HM Rec Customer", "content": ["like", f"%{doc.name}%"]}
			)
		)
		# Retry after a dropped connection: same record, no duplicate.
		again = upload(self.sales1, _audio(), activity=task.name, client_key=key)
		self.assertEqual(again["data"]["id"], d["id"])
		self.assertEqual(frappe.db.count("Call Recording", {"client_key": key}), 1)

	def test_works_for_leads(self):
		lead = make_lead("HM Rec Lead", "9061234567")
		r = upload(self.sales1, _audio(), party_doctype="CRM Lead", party=lead)
		self.assertTrue(r["success"], r)
		self.assertEqual(r["data"]["party_doctype"], "CRM Lead")

	def test_rejects_bad_files(self):
		self.assertEqual(upload(self.sales1, None)["_http_status"], 422)
		self.assertEqual(upload(self.sales1, _audio("notes.pdf"))["_http_status"], 422)
		big = upload(self.sales1, _audio(size=1024 * 1024 + 10))
		self.assertEqual(big["_http_status"], 422)
		self.assertIn("larger", big["error"]["message"])

	def test_cannot_attach_to_someone_elses_activity(self):
		task = make_task(self.sales2, future(), ref=("Customer", "HM Rec Customer"))
		r = upload(self.sales1, _audio(), activity=task.name)
		self.assertEqual(r["_http_status"], 404)

	def test_visibility_list_download_delete(self):
		mine = upload(self.sales1, _audio())["data"]["id"]

		own = call(recordings.get_list, self.sales1, party_doctype="Customer", party="HM Rec Customer")
		self.assertIn(mine, [i["id"] for i in own["data"]["items"]])
		other = call(recordings.get_list, self.sales2, party_doctype="Customer", party="HM Rec Customer")
		self.assertNotIn(mine, [i["id"] for i in other["data"]["items"]])
		mgr = call(recordings.get_list, self.manager)
		self.assertIn(mine, [i["id"] for i in mgr["data"]["items"]])

		# Desk / REST follow the same rule.
		frappe.set_user(self.sales2)
		try:
			self.assertNotIn(mine, frappe.get_list("Call Recording", pluck="name"))
			self.assertFalse(frappe.has_permission("Call Recording", doc=frappe.get_doc("Call Recording", mine)))
		finally:
			frappe.set_user("Administrator")

		frappe.set_user(self.sales1)
		frappe.local.response = frappe._dict()
		try:
			recordings.download(mine)
			self.assertTrue(frappe.local.response.filecontent.startswith(b"\x00\x00\x00\x18ftyp"))
			self.assertEqual(frappe.local.response.content_type, "audio/mp4")
		finally:
			frappe.set_user("Administrator")
		frappe.set_user(self.sales2)
		try:
			with self.assertRaises(frappe.DoesNotExistError):
				recordings.download(mine)
		finally:
			frappe.set_user("Administrator")

		self.assertEqual(call(recordings.delete, self.sales2, name=mine)["_http_status"], 404)
		res = call(recordings.delete, self.sales1, name=mine)
		self.assertTrue(res["success"], res)
		self.assertFalse(frappe.db.exists("Call Recording", mine))

	def test_switch_off_in_settings(self):
		_set(enable_call_recordings=0)
		try:
			self.assertNotIn("call_recordings", perms.capabilities(self.sales1))
			self.assertEqual(upload(self.sales1, _audio())["_http_status"], 422)
		finally:
			_set(enable_call_recordings=1)
		self.assertIn("call_recordings", perms.capabilities(self.sales1))

	def test_unknown_number_is_saved_and_linked_when_the_lead_appears(self):
		number = "90" + uuid.uuid4().hex[:0] + str(uuid.uuid4().int)[:8]
		r = upload(self.sales1, _audio(), party_doctype=None, party=None, phone=number)
		self.assertTrue(r["success"], r)
		self.assertIsNone(r["data"]["party"])
		rec = r["data"]["id"]
		lead = make_lead(f"HM Rec Later {number}", number)
		self.assertEqual(frappe.db.get_value("Call Recording", rec, "party"), lead)

	def test_known_number_is_linked_by_the_server(self):
		r = upload(self.sales1, _audio(), party_doctype=None, party=None, phone="98470 12345")
		self.assertEqual(r["data"]["party"], "HM Rec Customer")

	def test_same_phone_file_twice_is_saved_once(self):
		name = f"_919847012345_{uuid.uuid4().hex[:6]}.m4a"
		a = upload(self.sales1, _audio(name))["data"]["id"]
		b = upload(self.sales1, _audio(name))["data"]["id"]  # another entry on the phone, new client key
		self.assertEqual(a, b)
