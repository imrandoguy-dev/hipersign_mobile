# Copyright (c) 2026, Hipersign Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import cint, get_datetime

from hipersign_mobile.services import timeutil


class DevelopmentTask(Document):
	def validate(self):
		from hipersign_mobile.notifications.queue import offsets_text, parse_offsets

		if not self.assigned_by:
			self.assigned_by = frappe.session.user if frappe.session.user != "Guest" else None
		if self.reminders:
			self.reminders = offsets_text(parse_offsets(self.reminders)) or None
		if self.developer:
			self.developer_name = frappe.utils.get_fullname(self.developer)
		before = self.get_doc_before_save()
		if self.status == "Done":
			if not self.completed_on:
				self.completed_on = timeutil.now_system()
		else:
			self.completed_on = None
		if before and str(before.deadline) != str(self.deadline):
			self.deadline_changes = cint(self.deadline_changes) + 1
			if get_datetime(self.deadline) > timeutil.now_system():
				self.deadline_crossed = 0

	def on_update(self):
		from hipersign_mobile.services import development

		try:
			development.on_saved(self, self.get_doc_before_save())
		except Exception:
			frappe.log_error(title="Hipersign development alert")
