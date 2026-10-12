from __future__ import annotations

from unittest.mock import MagicMock, patch

import frappe

try:
	from frappe.tests import IntegrationTestCase as TestCase
except ImportError:  # Frappe v15
	from frappe.tests.utils import FrappeTestCase as TestCase

from hipersign_mobile.api import updates
from hipersign_mobile.tests.utils import call, make_user


def _resp(status=200, json_data=None, headers=None):
	r = MagicMock()
	r.status_code = status
	r.json.return_value = json_data or {}
	r.headers = headers or {}
	return r


RELEASE = {
	"body": "Latest pilot build",
	"assets": [
		{"name": "Hipersign.apk", "url": "https://api.github.com/assets/1", "size": 57000000},
		{"name": "version.json", "url": "https://api.github.com/assets/2", "size": 80},
	],
}
VERSION = {"version_code": 150, "version_name": "0.1.150", "notes": "New calendar colours"}


def fake_get(url, headers=None, timeout=None, allow_redirects=True):
	if url.endswith("/releases/tags/pilot"):
		return _resp(json_data=RELEASE)
	if url.endswith("/assets/2"):
		return _resp(json_data=VERSION)
	if url.endswith("/assets/1"):
		return _resp(status=302, headers={"Location": "https://objects.githubusercontent.com/signed-apk"})
	return _resp(status=404)


class TestUpdates(TestCase):
	def setUp(self):
		self.user = make_user("sales1")
		frappe.cache.delete_value(updates.CACHE_KEY)

	def tearDown(self):
		frappe.conf.pop("hipersign_mobile_github_token", None)
		frappe.cache.delete_value(updates.CACHE_KEY)

	def test_not_configured_is_a_quiet_no(self):
		frappe.conf.pop("hipersign_mobile_github_token", None)
		r = call(updates.check, self.user, version_code=1)
		self.assertTrue(r["success"])
		self.assertFalse(r["data"]["available"])

	def test_newer_build_is_offered_and_link_is_signed_url(self):
		frappe.conf["hipersign_mobile_github_token"] = "x"
		with patch.object(updates.requests, "get", side_effect=fake_get):
			r = call(updates.check, self.user, version_code=120, platform="Android")
			self.assertTrue(r["data"]["available"])
			self.assertEqual(r["data"]["version_name"], "0.1.150")
			same = call(updates.check, self.user, version_code=150)
			self.assertFalse(same["data"]["available"])
			d = call(updates.get_download_url, self.user)
			self.assertEqual(d["data"]["url"], "https://objects.githubusercontent.com/signed-apk")

	def test_token_from_settings_page(self):
		s = frappe.get_single("Hipersign Mobile Settings")
		s.enable_app_updates = 1
		s.github_token = "from-settings"
		s.save()
		try:
			token, repo, tag = updates._conf()
			self.assertEqual(token, "from-settings")
			self.assertEqual((repo, tag), ("imrandoguy-dev/hipersign_mobile_app", "pilot"))
			s.enable_app_updates = 0
			s.save()
			with self.assertRaises(updates.UpdateConfigError):
				updates._conf()
		finally:
			s.reload()
			s.enable_app_updates = 0
			s.save()

	def test_ios_never_offers_apk(self):
		frappe.conf["hipersign_mobile_github_token"] = "x"
		r = call(updates.check, self.user, version_code=1, platform="iOS")
		self.assertFalse(r["data"]["available"])
