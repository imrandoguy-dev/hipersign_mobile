"""Fill "Deal Made By" on existing deals: from the "Converted from lead ... in Hipersign by
<name>" note the app leaves, else the deal's owner (when a real person made it in the desk)."""

import re

import frappe


def execute():
	if not frappe.db.exists("DocType", "CRM Deal") or not frappe.get_meta("CRM Deal").has_field("hm_created_by"):
		return
	by_name = {}
	for u in frappe.get_all("User", filters={"enabled": 1, "user_type": "System User"}, fields=["name", "full_name"]):
		by_name.setdefault((u.full_name or "").strip(), u.name)
	notes = frappe.get_all(
		"Comment",
		filters={"reference_doctype": "CRM Deal", "comment_type": "Info", "content": ["like", "Converted from lead % in Hipersign by %"]},
		fields=["reference_name", "content"],
	)
	for n in notes:
		m = re.search(r"in Hipersign by (.+)$", frappe.utils.strip_html(n.content or "").strip())
		user = by_name.get(m.group(1).strip()) if m else None
		if user:
			frappe.db.set_value("CRM Deal", n.reference_name, "hm_created_by", user, update_modified=False)
	frappe.db.sql(
		"""update `tabCRM Deal` set hm_created_by = owner
		where ifnull(hm_created_by, '') = '' and owner not in ('Administrator', 'Guest', '')"""
	)
