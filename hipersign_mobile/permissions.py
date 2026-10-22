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


# ---------------------------------------------------------------- call recordings


def _sees_all_recordings(user: str) -> bool:
	return user == "Administrator" or bool(set(frappe.get_roles(user)) & UNRESTRICTED)


def call_recording_query(user: str | None = None, doctype: str | None = None) -> str:
	user = user or frappe.session.user
	if _sees_all_recordings(user):
		return ""
	return f"(`tabCall Recording`.recorded_by = {frappe.db.escape(user)})"


def call_recording_has_permission(doc, ptype=None, user=None, debug=False):
	user = user or frappe.session.user
	if _sees_all_recordings(user):
		return True
	if getattr(doc, "is_new", None) and doc.is_new():
		return True
	return doc.get("recorded_by") == user


# ---------------------------------------------------------------- call logs (same rule as recordings)


def call_log_query(user: str | None = None, doctype: str | None = None) -> str:
	user = user or frappe.session.user
	if _sees_all_recordings(user):
		return ""
	return f"(`tabMobile Call Log`.caller = {frappe.db.escape(user)})"


def call_log_has_permission(doc, ptype=None, user=None, debug=False):
	user = user or frappe.session.user
	if _sees_all_recordings(user):
		return True
	if getattr(doc, "is_new", None) and doc.is_new():
		return True
	return doc.get("caller") == user


# Development tasks: developers see the ones assigned to them or by them; managers see all.
def _dev_all(user: str) -> bool:
	return user == "Administrator" or bool({"System Manager", "Hipersign Manager"} & set(frappe.get_roles(user)))


def development_task_query(user: str | None = None, doctype: str | None = None) -> str:
	user = user or frappe.session.user
	if _dev_all(user):
		return ""
	u = frappe.db.escape(user)
	return f"(`tabDevelopment Task`.developer = {u} or `tabDevelopment Task`.assigned_by = {u})"


def development_task_has_permission(doc, ptype=None, user=None, debug=False):
	user = user or frappe.session.user
	if _dev_all(user) or (getattr(doc, "is_new", None) and doc.is_new()):
		return True
	return user in (doc.get("developer"), doc.get("assigned_by"))


# ---------------------------------------------------------------- team tasks


def _tasks_all(user: str) -> bool:
	return user == "Administrator" or bool(
		{"System Manager", "Hipersign Manager", "Hipersign CEO", "Hipersign HR"} & set(frappe.get_roles(user))
	)


def team_task_query(user: str | None = None, doctype: str | None = None) -> str:
	user = user or frappe.session.user
	if _tasks_all(user):
		return ""
	u = frappe.db.escape(user)
	return f"(`tabTeam Task`.assigned_to = {u} or `tabTeam Task`.assigned_by = {u})"


def team_task_has_permission(doc, ptype=None, user=None, debug=False):
	user = user or frappe.session.user
	if _tasks_all(user) or (getattr(doc, "is_new", None) and doc.is_new()):
		return True
	return user in (doc.get("assigned_to"), doc.get("assigned_by"))


# ---------------------------------------------------------------- concerns (private)


def concern_audiences(user: str) -> tuple[str, ...]:
	"""Which concerns (besides their own) a user may read."""
	if user == "Administrator":
		return ("CEO Only", "HR & Managers")
	roles = set(frappe.get_roles(user))
	if "Hipersign CEO" in roles:
		return ("CEO Only", "HR & Managers")
	if {"Hipersign HR", "Hipersign Manager"} & roles:
		return ("HR & Managers",)
	return ()


def concern_query(user: str | None = None, doctype: str | None = None) -> str:
	user = user or frappe.session.user
	u = frappe.db.escape(user)
	aud = concern_audiences(user)
	if len(aud) == 2:
		return ""
	if aud:
		return f"(`tabHipersign Concern`.audience = 'HR & Managers' or `tabHipersign Concern`.raised_by = {u})"
	return f"(`tabHipersign Concern`.raised_by = {u})"


def concern_has_permission(doc, ptype=None, user=None, debug=False):
	user = user or frappe.session.user
	if doc.get("raised_by") == user and ptype in (None, "read", "print"):
		return True
	return doc.get("audience") in concern_audiences(user)


# ---------------------------------------------------------------- daily reports


def _reports_all(user: str) -> bool:
	return user == "Administrator" or bool(
		{"System Manager", "Hipersign Manager", "Hipersign CEO", "Hipersign HR"} & set(frappe.get_roles(user))
	)


def daily_report_query(user: str | None = None, doctype: str | None = None) -> str:
	user = user or frappe.session.user
	if _reports_all(user):
		return ""
	return f"(`tabHipersign Daily Report`.user = {frappe.db.escape(user)})"


def daily_report_has_permission(doc, ptype=None, user=None, debug=False):
	user = user or frappe.session.user
	return _reports_all(user) or doc.get("user") == user
