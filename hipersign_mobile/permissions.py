"""Record-level CRM Task permissions for mobile-only users.

"Hipersign Sales" / "Hipersign Trainer" get read+write on CRM Task (Frappe CRM's
assignment needs the assignee to be able to read the task), but only for tasks
assigned to or created by them. This applies to every path (Desk, Frappe CRM UI,
/api/resource), not just the mobile API.

Users who also hold Sales User keep Frappe CRM's normal "see all tasks" behaviour,
unless "Restrict Task Visibility" is ticked in Hipersign Mobile Settings (opt-in).
Managers (System Manager, Sales Manager, Hipersign Manager) are never restricted.
"""

from __future__ import annotations

import frappe

UNRESTRICTED = {"System Manager", "Sales Manager", "Hipersign Manager", "Administrator"}
MOBILE = {"Hipersign Sales", "Hipersign Trainer", "Hipersign Cold Caller"}


def _restricted(user: str) -> bool:
	if user == "Administrator":
		return False
	roles = set(frappe.get_roles(user))
	if roles & UNRESTRICTED or not roles & MOBILE:
		return False
	if "Sales User" in roles:
		from hipersign_mobile.services import settings as hm_settings

		return bool(hm_settings.get().restrict_task_visibility)
	return True


def crm_task_query(user: str | None = None, doctype: str | None = None) -> str:
	user = user or frappe.session.user
	if not _restricted(user):
		return ""
	u = frappe.db.escape(user)
	return f"(`tabCRM Task`.assigned_to = {u} or `tabCRM Task`.owner = {u})"


def crm_task_has_permission(doc, ptype=None, user=None, debug=False):
	user = user or frappe.session.user
	if not _restricted(user):
		return True
	if getattr(doc, "is_new", None) and doc.is_new():
		return True  # creation is governed by role permissions + the mobile API checks
	return doc.get("assigned_to") == user or doc.get("owner") == user
