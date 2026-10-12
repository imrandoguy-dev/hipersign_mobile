# Copyright (c) 2026, Hipersign Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

from hipersign_mobile.services.party import party_summary


class CallRecording(Document):
	def validate(self):
		if self.party_doctype not in ("Customer", "CRM Lead", "CRM Deal"):
			frappe.throw("Party Type must be Customer, CRM Lead or CRM Deal.")
		summary = party_summary(self.party_doctype, self.party)
		if summary:
			self.party_name = summary.get("name")
