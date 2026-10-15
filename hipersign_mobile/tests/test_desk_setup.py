from __future__ import annotations

import json

import frappe

try:
	from frappe.tests import IntegrationTestCase as TestCase
except ImportError:  # Frappe v15
	from frappe.tests.utils import FrappeTestCase as TestCase

from hipersign_mobile import desk_setup


class TestDeskWorkspace(TestCase):
	def test_workspace_sidebar_and_icon(self):
		before = frappe.db.count("Error Log")
		desk_setup.setup()
		desk_setup.setup()  # idempotent
		self.assertEqual(frappe.db.count("Error Log"), before, frappe.get_all("Error Log", fields=["error"], limit=1, order_by="creation desc"))
		ws = frappe.get_doc("Workspace", "Hipersign Mobile")
		self.assertTrue(ws.public)
		content = json.loads(ws.content)
		cards = [b["data"]["card_name"] for b in content if b["type"] == "card"]
		self.assertIn("Calls", cards)
		self.assertIn("Trials & trainers", cards)
		links = [(l.type, l.label) for l in ws.links]
		self.assertIn(("Link", "Calling Activity"), links)
		# every card break's count matches its links
		i = 0
		while i < len(ws.links):
			if ws.links[i].type == "Card Break":
				n = ws.links[i].link_count
				self.assertTrue(all(l.type == "Link" for l in ws.links[i + 1 : i + 1 + n]))
				i += n
			i += 1
		self.assertNotIn("WhatsApp Messages", [l.label for l in ws.links] if not frappe.db.exists("DocType", "WhatsApp Message") else [])
		if frappe.db.exists("DocType", "Workspace Sidebar") and not frappe.db.exists("DocType", "Sidebar"):
			sb = frappe.get_doc("Workspace Sidebar", "Hipersign Mobile")
			self.assertEqual(sb.items[0].link_to, "Hipersign Mobile")
			self.assertIn("Trials", [x.label for x in sb.items])
		if frappe.db.exists("DocType", "Desktop Icon") and not frappe.db.exists("DocType", "Sidebar"):
			self.assertEqual(frappe.db.get_value("Desktop Icon", "Hipersign Mobile", "link_to"), "Hipersign Mobile")
		# the desk still boots with it (a bad workspace breaks boot for everyone)
		from frappe.boot import get_bootinfo

		frappe.set_user("Administrator")
		get_bootinfo()

	def test_older_frappe_sidebar_and_desktop_icon(self):
		"""Sites on the Frappe 16 build with Workspace Sidebar + Desktop Icon (e.g. group)."""
		if not frappe.db.exists("DocType", "Workspace Sidebar"):
			self.skipTest("no Workspace Sidebar here")
		frappe.flags.in_migrate = True  # newer builds keep it read-only outside migrate
		try:
			desk_setup._sidebar()
			desk_setup._sidebar()
			if frappe.db.exists("DocType", "Desktop Icon"):
				desk_setup._desktop_icon()
				self.assertEqual(frappe.db.get_value("Desktop Icon", "Hipersign Mobile", "link_type"), "Workspace Sidebar")
		finally:
			frappe.flags.in_migrate = False
		sb = frappe.get_doc("Workspace Sidebar", "Hipersign Mobile")
		kinds = [(x.type, x.label) for x in sb.items]
		self.assertEqual(kinds[0], ("Link", "Home"))
		self.assertIn(("Section Break", "Calls"), kinds)
		self.assertIn(("Link", "Calling Activity"), kinds)
		frappe.db.rollback()
