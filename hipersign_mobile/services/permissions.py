"""Server-side authorization for the mobile API.

Roles are ordinary Frappe roles that an administrator assigns on the User record.
The phone never tells the server what it may do; it asks (auth.get_me) and every
endpoint re-checks here.
"""

from __future__ import annotations

import frappe

from hipersign_mobile.services import settings as hm_settings

ROLE_SALES = "Hipersign Sales"
ROLE_MANAGER = "Hipersign Manager"
ROLE_TRAINER = "Hipersign Trainer"
ROLE_COLD_CALLER = "Hipersign Cold Caller"
MOBILE_ROLES = (ROLE_SALES, ROLE_MANAGER, ROLE_TRAINER, ROLE_COLD_CALLER)

SALES_CAPS = {
	"calendar",
	"followups",
	"meetings",
	"customers",
	"create_activity",
	"trial_booking",
	"call_recordings",
}
MANAGER_CAPS = SALES_CAPS | {"team_view", "assign_others", "manage_trainer_slots"}
TRAINER_CAPS = {"manage_trainer_slots"}

ALL_CAPS = sorted(MANAGER_CAPS | TRAINER_CAPS)


def user_roles(user: str | None = None) -> set[str]:
	return set(frappe.get_roles(user or frappe.session.user))


def capabilities(user: str | None = None) -> set[str]:
	roles = user_roles(user)
	caps: set[str] = set()
	if "System Manager" in roles or ROLE_MANAGER in roles:
		caps |= MANAGER_CAPS
	if ROLE_SALES in roles or ROLE_COLD_CALLER in roles:
		caps |= SALES_CAPS
	if ROLE_TRAINER in roles:
		caps |= TRAINER_CAPS
	if "call_recordings" in caps and not hm_settings.get().enable_call_recordings:
		caps.discard("call_recordings")
	return caps


def workspaces(caps: set[str]) -> list[str]:
	spaces = []
	if "followups" in caps:
		spaces.append("sales")
	# Future: "cold_caller", "trainer", "hr", "manager"
	return spaces


def require(cap: str, user: str | None = None):
	if cap not in capabilities(user):
		raise frappe.PermissionError("You are not allowed to do this.")


def require_active_user(user: str | None = None):
	user = user or frappe.session.user
	if user in (None, "", "Guest"):
		raise frappe.AuthenticationError("Please sign in.")
	if not frappe.db.get_value("User", user, "enabled"):
		raise frappe.AuthenticationError("This account is disabled.")


# ---------------------------------------------------------------- activities


def can_access_task(task: dict | frappe.model.document.Document, user: str | None = None, caps=None) -> bool:
	user = user or frappe.session.user
	caps = capabilities(user) if caps is None else caps
	if "team_view" in caps:
		return True
	return task.get("assigned_to") == user


def get_task_for(task_id, user: str | None = None, for_update: bool = False):
	"""Load a CRM Task the caller is allowed to act on, or raise."""
	user = user or frappe.session.user
	try:
		task_name = int(str(task_id))
	except (TypeError, ValueError):
		raise frappe.DoesNotExistError("Activity not found.")
	if not frappe.db.exists("CRM Task", task_name):
		raise frappe.DoesNotExistError("Activity not found.")
	if for_update:
		# Row lock: concurrent completes/reschedules of the same activity serialize here.
		frappe.db.sql("select name from `tabCRM Task` where name=%s for update", task_name)
	doc = frappe.get_doc("CRM Task", task_name)
	if not can_access_task(doc, user):
		# Same answer as "missing" so IDs cannot be probed.
		raise frappe.DoesNotExistError("Activity not found.")
	return doc


# ---------------------------------------------------------------- customers / leads

PARTY_DOCTYPES = ("Customer", "CRM Lead", "CRM Deal")


def own_party_names(doctype: str, user: str) -> set[str]:
	"""Parties a user 'owns' when Customer Visibility = Own Records."""
	names = set(
		frappe.get_all(
			"CRM Task",
			filters={"assigned_to": user, "reference_doctype": doctype},
			pluck="reference_docname",
		)
	)
	if doctype == "CRM Lead":
		names |= set(frappe.get_all("CRM Lead", filters={"lead_owner": user}, pluck="name"))
	elif doctype == "CRM Deal":
		names |= set(frappe.get_all("CRM Deal", filters={"deal_owner": user}, pluck="name"))
	elif doctype == "Customer":
		names |= set(
			frappe.get_all(
				"Trial Booking", filters={"booked_by": user, "customer": ["is", "set"]}, pluck="customer"
			)
		)
		names |= set(frappe.get_all("Customer", filters={"account_manager": user}, pluck="name"))
	return {n for n in names if n}


def can_access_party(doctype: str, name: str, user: str | None = None, caps=None) -> bool:
	user = user or frappe.session.user
	caps = capabilities(user) if caps is None else caps
	if doctype not in PARTY_DOCTYPES or not name:
		return False
	if not frappe.db.exists(doctype, name):
		return False
	if "customers" not in caps and "team_view" not in caps:
		return False
	if "team_view" in caps or hm_settings.get().customer_scope != "Own Records":
		return True
	return name in own_party_names(doctype, user)


def require_party(doctype: str, name: str, user: str | None = None):
	if not can_access_party(doctype, name, user):
		raise frappe.DoesNotExistError(f"{doctype} not found.")
