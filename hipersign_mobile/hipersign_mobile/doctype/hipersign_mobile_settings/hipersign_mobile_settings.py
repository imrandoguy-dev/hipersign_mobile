# Copyright (c) 2026, Hipersign Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document


class HipersignMobileSettings(Document):
	def on_update(self):
		# New token / repo / tag takes effect immediately.
		frappe.cache.delete_value("hipersign_mobile:latest_release")
