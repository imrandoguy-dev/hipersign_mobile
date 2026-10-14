from __future__ import annotations

import json

import frappe

try:
	from frappe.tests import IntegrationTestCase as TestCase
except ImportError:  # Frappe v15
	from frappe.tests.utils import FrappeTestCase as TestCase

from hipersign_mobile.notifications import fcm

KEY = {"type": "service_account", "project_id": "hm-test", "client_email": "x@hm-test.iam.gserviceaccount.com",
	   "private_key": "-----BEGIN PRIVATE KEY-----\nabc\n-----END PRIVATE KEY-----\n"}


class TestFirebaseKeyUpload(TestCase):
	def tearDown(self):
		frappe.db.set_single_value("Hipersign Mobile Settings", "fcm_key_file", None)
		frappe.db.rollback()

	def test_uploaded_key_is_used(self):
		f = frappe.get_doc({"doctype": "File", "file_name": "hm-firebase-key.json", "is_private": 1,
							"content": json.dumps(KEY)}).insert(ignore_permissions=True)
		frappe.db.set_single_value("Hipersign Mobile Settings", "fcm_key_file", f.file_url)
		self.assertEqual(fcm._credentials()["project_id"], "hm-test")
		self.assertTrue(fcm.is_configured())

	def test_no_key(self):
		frappe.db.set_single_value("Hipersign Mobile Settings", "fcm_key_file", None)
		if not frappe.conf.get("hipersign_mobile_fcm_credentials"):
			self.assertFalse(fcm.is_configured())

	def test_bad_file(self):
		f = frappe.get_doc({"doctype": "File", "file_name": "hm-bad.json", "is_private": 1,
							"content": "not json"}).insert(ignore_permissions=True)
		frappe.db.set_single_value("Hipersign Mobile Settings", "fcm_key_file", f.file_url)
		with self.assertRaises(fcm.FCMConfigError):
			fcm._credentials()
