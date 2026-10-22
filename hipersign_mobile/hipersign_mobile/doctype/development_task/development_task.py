# Copyright (c) 2026, Hipersign Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class DevelopmentTask(Document):
	def validate(self):
		from hipersign_mobile.services import development

		development.validate_doc(self)

	def on_update(self):
		from hipersign_mobile.services import development

		try:
			development.on_saved(self, self.get_doc_before_save())
		except Exception:
			frappe.log_error(title="Hipersign development alert")
