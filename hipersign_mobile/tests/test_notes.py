from __future__ import annotations

import frappe

try:
	from frappe.tests import IntegrationTestCase as TestCase
except ImportError:  # Frappe v15
	from frappe.tests.utils import FrappeTestCase as TestCase

from hipersign_mobile.api import notes
from hipersign_mobile.tests.utils import call, make_customer, make_lead, make_user


class TestNotes(TestCase):
	@classmethod
	def setUpClass(cls):
		super().setUpClass()
		cls.s1 = make_user("sales1")
		cls.s2 = make_user("sales2")
		cls.mgr = make_user("manager")
		cls.cust = make_customer("HM Notes Customer")
		cls.lead = make_lead("HM Notes Lead", "9061200077")

	def _add(self, user, text, doctype="Customer", name=None, title=None):
		r = call(notes.add, user, doctype=doctype, name=name or self.cust, text=text, title=title)
		frappe.db.commit()
		return r

	def test_add_list_shows_in_frappe_crm_format(self):
		r = self._add(self.s1, "Asked for a quote <b>next week</b>.\nPrefers WhatsApp.\n\nBudget 5 lakh")
		self.assertTrue(r["success"], r)
		n = frappe.get_doc("FCRM Note", r["data"]["id"])
		self.assertEqual(n.reference_doctype, "Customer")
		self.assertEqual(n.title, "Asked for a quote <b>next week</b>.")
		self.assertIn("&lt;b&gt;", n.content)  # typed text is escaped, not run as HTML
		self.assertIn("<br>", n.content)
		listed = call(notes.get_list, self.s2, doctype="Customer", name=self.cust)
		item = next(i for i in listed["data"]["items"] if i["id"] == n.name)
		self.assertEqual(item["text"], "Asked for a quote <b>next week</b>.\nPrefers WhatsApp.\n\nBudget 5 lakh")
		self.assertFalse(item["can_edit"])  # someone else's note
		self.assertEqual(item["owner_name"], frappe.utils.get_fullname(self.s1))

	def test_notes_written_in_frappe_crm_read_as_text(self):
		frappe.get_doc(
			{
				"doctype": "FCRM Note",
				"title": "Site visit",
				"content": "<p>Roof is <strong>south</strong> facing</p><ul><li>3 phase</li><li>10 kW</li></ul>",
				"reference_doctype": "CRM Lead",
				"reference_docname": self.lead,
			}
		).insert(ignore_permissions=True)
		frappe.db.commit()
		r = call(notes.get_list, self.s1, doctype="CRM Lead", name=self.lead)
		text = r["data"]["items"][0]["text"]
		self.assertIn("Roof is south facing", text)
		self.assertIn("• 3 phase", text)

	def test_only_owner_or_manager_edits(self):
		note = self._add(self.s1, "Original")["data"]["id"]
		self.assertEqual(call(notes.update, self.s2, id=note, text="Hacked")["_http_status"], 403)
		self.assertEqual(call(notes.delete, self.s2, id=note)["_http_status"], 403)
		self.assertTrue(call(notes.update, self.s1, id=note, text="Edited")["success"])
		frappe.db.commit()
		self.assertEqual(notes.html_to_text(frappe.db.get_value("FCRM Note", note, "content")), "Edited")
		self.assertTrue(call(notes.update, self.mgr, id=note, text="Manager fix")["success"])
		self.assertTrue(call(notes.delete, self.mgr, id=note)["success"])
		self.assertFalse(frappe.db.exists("FCRM Note", note))

	def test_validation(self):
		self.assertEqual(self._add(self.s1, "   ")["_http_status"], 422)
		self.assertEqual(self._add(self.s1, "x", doctype="Item")["_http_status"], 422)
		self.assertEqual(self._add(self.s1, "x", name="No Such Customer")["_http_status"], 404)
