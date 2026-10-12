"""Give pre-existing CRM Tasks an activity type.

Tasks with a Visit Type (nexus365's custom_visit_type: client meeting, site
visit...) are meetings; everything else is a follow-up. Only fills empty values.
"""

import frappe


def execute():
	if not frappe.db.has_column("CRM Task", "hm_activity_type"):
		return
	if frappe.db.has_column("CRM Task", "custom_visit_type"):
		frappe.db.sql(
			"""update `tabCRM Task` set hm_activity_type='Meeting'
			where (hm_activity_type is null or hm_activity_type='')
			and custom_visit_type is not null and custom_visit_type != ''"""
		)
	frappe.db.sql(
		"""update `tabCRM Task` set hm_activity_type='Follow-up'
		where hm_activity_type is null or hm_activity_type=''"""
	)
