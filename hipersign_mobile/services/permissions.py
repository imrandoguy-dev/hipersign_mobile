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
ROLE_DEVELOPER = "Hipersign Developer"
ROLE_CEO = "Hipersign CEO"
ROLE_HR = "Hipersign HR"
MOBILE_ROLES = (ROLE_SALES, ROLE_MANAGER, ROLE_TRAINER, ROLE_COLD_CALLER, ROLE_DEVELOPER, ROLE_CEO, ROLE_HR)

SALES_CAPS = {
	"calendar",
	"followups",
	"meetings",
	"customers",
	"create_activity",
	"trial_booking",
	"call_recordings",
	"quotations",
}
MANAGER_CAPS = SALES_CAPS | {"team_view", "assign_others", "manage_trainer_slots", "development", "assign_development"}
TRAINER_CAPS = {"manage_trainer_slots", "calendar"}  # trainings she gives are on her calendar
DEVELOPER_CAPS = {"development"}
# Everyone who signs in: their own tasks, today's report, app suggestions, raising a concern.
EVERYONE_CAPS = {"tasks", "daily_report", "feedback", "concerns"}
MANAGER_CAPS = MANAGER_CAPS | {"assign_tasks", "concerns_hr", "call_targets"}
HR_CAPS = {"assign_tasks", "concerns_hr", "team_reports"}
CEO_CAPS = MANAGER_CAPS | {"concerns_ceo"}

ALL_CAPS = sorted(
	MANAGER_CAPS | TRAINER_CAPS | DEVELOPER_CAPS | EVERYONE_CAPS | HR_CAPS | CEO_CAPS | {"whatsapp", "call_target"}
)


def user_roles(user: str | None = None) -> set[str]:
	return set(frappe.get_roles(user or frappe.session.user))


def capabilities(user: str | None = None) -> set[str]:
	user = user or frappe.session.user
	roles = user_roles(user)
	caps: set[str] = set(EVERYONE_CAPS) if user not in (None, "", "Guest") else set()
	if "System Manager" in roles or ROLE_MANAGER in roles:
		caps |= MANAGER_CAPS
	if ROLE_CEO in roles:
		caps |= CEO_CAPS
	if ROLE_HR in roles:
		caps |= HR_CAPS
	if ROLE_SALES in roles or ROLE_COLD_CALLER in roles:
		caps |= SALES_CAPS
	if ROLE_TRAINER in roles:
		caps |= TRAINER_CAPS
	if ROLE_DEVELOPER in roles:
		caps |= DEVELOPER_CAPS
	if "call_recordings" in caps and not hm_settings.get().enable_call_recordings:
		caps.discard("call_recordings")
	if ROLE_COLD_CALLER in roles or _has_call_target(user):
		caps.add("call_target")  # daily call target with alerts
	if "team_view" in caps:
		caps.add("team_reports")
	if "customers" in caps and whatsapp_chat_on():
		caps.add("whatsapp")  # WhatsApp chats with customers (needs the Frappe WhatsApp app)
	return caps


def _has_call_target(user: str) -> bool:
	try:
		return bool(frappe.db.exists("Call Target", {"user": user, "enabled": 1}))
	except Exception:
		return False  # before the doctype exists (first migrate)


def whatsapp_chat_on() -> bool:
	return bool(hm_settings.get().enable_whatsapp_chat) and "frappe_whatsapp" in frappe.get_installed_apps()


def workspaces(caps: set[str]) -> list[str]:
	spaces = []
	if "followups" in caps:
		spaces.append("sales")
	if "development" in caps:
		spaces.append("development")
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
	if task.get("assigned_to") == user:
		return True
	# The trial's trainer and the salesperson who booked it can open it too.
	booking = task.get("hm_trial_booking")
	if not booking:
		return False
	b = frappe.db.get_value("Trial Booking", booking, ["trainer", "booked_by"], as_dict=True)
	return bool(b and user in (b.trainer, b.booked_by))


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
