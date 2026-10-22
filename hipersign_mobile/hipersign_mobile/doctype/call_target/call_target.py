# Copyright (c) 2026, Hipersign Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import cint


class CallTarget(Document):
	def validate(self):
		from hipersign_mobile.services.call_targets import parse_times

		self.user_name = frappe.utils.get_fullname(self.user)
		times = parse_times(self.check_times)
		self.check_times = ",".join(t.strftime("%H:%M") for t in times) or None
		if cint(self.daily_target) < 0:
			frappe.throw("Calls per day can't be negative.")
		if cint(self.idle_minutes) < 0:
			frappe.throw("Minutes can't be negative.")
		if not self.set_by and frappe.session.user != "Guest":
			self.set_by = frappe.session.user
