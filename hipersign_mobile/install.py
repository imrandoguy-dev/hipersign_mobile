"""Install / migrate: roles, CRM Task custom fields and indexes. Idempotent."""

from __future__ import annotations

import frappe
from frappe.custom.doctype.custom_field.custom_field import create_custom_fields

from hipersign_mobile.services.permissions import MOBILE_ROLES

CRM_TASK_FIELDS = {
	"CRM Task": [
		{
			"fieldname": "hm_section",
			"fieldtype": "Section Break",
			"label": "Hipersign Mobile",
			"insert_after": "description",
			"collapsible": 1,
		},
		{
			"fieldname": "hm_activity_type",
			"fieldtype": "Select",
			"label": "Activity Type",
			"options": "Follow-up\nMeeting\nTrial",
			"default": "Follow-up",
			"insert_after": "hm_section",
			"in_standard_filter": 1,
		},
		{
			"fieldname": "hm_end_datetime",
			"fieldtype": "Datetime",
			"label": "End Time",
			"insert_after": "hm_activity_type",
			"description": "Optional. When empty, the default duration from Hipersign Mobile Settings is used.",
		},
		{
			"fieldname": "hm_meeting_link",
			"fieldtype": "Data",
			"options": "URL",
			"label": "Meeting Link",
			"insert_after": "hm_end_datetime",
		},
		{
			"fieldname": "hm_outcome",
			"fieldtype": "Data",
			"label": "Outcome",
			"insert_after": "hm_meeting_link",
		},
		{
			"fieldname": "hm_completion_notes",
			"fieldtype": "Small Text",
			"label": "Completion Notes",
			"insert_after": "hm_outcome",
		},
		{
			"fieldname": "hm_column_break",
			"fieldtype": "Column Break",
			"insert_after": "hm_completion_notes",
		},
		{
			"fieldname": "hm_reschedule_count",
			"fieldtype": "Int",
			"label": "Times Rescheduled",
			"default": "0",
			"read_only": 1,
			"no_copy": 1,
			"insert_after": "hm_column_break",
		},
		{
			"fieldname": "hm_completed_at",
			"fieldtype": "Datetime",
			"label": "Completed At",
			"read_only": 1,
			"no_copy": 1,
			"insert_after": "hm_reschedule_count",
		},
		{
			"fieldname": "hm_completed_by",
			"fieldtype": "Link",
			"options": "User",
			"label": "Completed By",
			"read_only": 1,
			"no_copy": 1,
			"insert_after": "hm_completed_at",
		},
		{
			"fieldname": "hm_trial_booking",
			"fieldtype": "Link",
			"options": "Trial Booking",
			"label": "Trial Booking",
			"read_only": 1,
			"no_copy": 1,
			"insert_after": "hm_completed_by",
		},
		{
			"fieldname": "hm_idempotency_key",
			"fieldtype": "Data",
			"label": "Mobile Request Key",
			"read_only": 1,
			"hidden": 1,
			"no_copy": 1,
			"unique": 1,
			"insert_after": "hm_trial_booking",
		},
	]
}


# Leads created from a call to a business found on Google Maps.
CRM_LEAD_FIELDS = {
	"CRM Lead": [
		{
			"fieldname": "hm_google_place_id",
			"fieldtype": "Data",
			"label": "Google Place ID",
			"insert_after": "source",
			"read_only": 1,
			"no_copy": 1,
			"search_index": 1,
		},
		{
			"fieldname": "hm_google_maps_url",
			"fieldtype": "Data",
			"options": "URL",
			"label": "Google Maps Link",
			"insert_after": "hm_google_place_id",
			"read_only": 1,
			"no_copy": 1,
		},
	]
}


def after_install():
	setup()


def after_migrate():
	setup()


def setup():
	create_roles()
	grant_task_permissions()
	grant_lead_permissions()
	create_custom_fields(CRM_TASK_FIELDS, update=True)
	create_custom_fields(CRM_LEAD_FIELDS, update=True)
	add_indexes()
	apply_new_setting_defaults()
	frappe.clear_cache(doctype="CRM Task")
	# install-app marks patches as done without running them, so backfill here too (idempotent).
	from hipersign_mobile.patches.v0_1.set_activity_type_for_existing_tasks import execute as backfill_types

	backfill_types()


# Settings added in later versions: on an existing site the new fields start empty (an
# unticked checkbox), so give them their intended default once - never overriding a saved value.
NEW_SETTING_DEFAULTS = {"enable_call_recordings": "1", "max_recording_mb": "50"}


def apply_new_setting_defaults():
	doctype = "Hipersign Mobile Settings"
	saved = set(
		frappe.db.sql("select field from `tabSingles` where doctype=%s", doctype, pluck=True)
	)
	changed = False
	for field, value in NEW_SETTING_DEFAULTS.items():
		if field not in saved:
			frappe.db.set_single_value(doctype, field, value)
			changed = True
	if changed:
		frappe.clear_document_cache(doctype, doctype)


def create_roles():
	for role in MOBILE_ROLES:
		if not frappe.db.exists("Role", role):
			frappe.get_doc({"doctype": "Role", "role_name": role, "desk_access": 1}).insert(
				ignore_permissions=True
			)


def grant_task_permissions():
	"""Hipersign roles may read/write CRM Task (row-level limits live in permissions.py)."""
	from frappe.permissions import add_permission, update_permission_property

	for role, ptypes in (
		("Hipersign Sales", ("read", "write")),
		("Hipersign Trainer", ("read", "write")),
		("Hipersign Manager", ("read", "write", "create", "report")),
	):
		if not frappe.db.exists("Custom DocPerm", {"parent": "CRM Task", "role": role, "permlevel": 0}):
			add_permission("CRM Task", role, 0)
		for ptype in ptypes:
			update_permission_property("CRM Task", role, 0, ptype, 1, validate=False)


def grant_lead_permissions():
	"""Salespeople create leads from calls; Frappe CRM then assigns each lead to its owner,
	which needs the owner to be able to read and edit CRM Lead."""
	from frappe.permissions import add_permission, update_permission_property

	for role, ptypes in (
		("Hipersign Sales", ("read", "write", "create")),
		("Hipersign Manager", ("read", "write", "create", "report")),
	):
		if not frappe.db.exists("Custom DocPerm", {"parent": "CRM Lead", "role": role, "permlevel": 0}):
			add_permission("CRM Lead", role, 0)
		for ptype in ptypes:
			update_permission_property("CRM Lead", role, 0, ptype, 1, validate=False)


def add_indexes():
	# Calendar queries: "my tasks in this date range".
	frappe.db.add_index("CRM Task", ["assigned_to", "due_date"], index_name="hm_assigned_due")
	frappe.db.add_index("CRM Task", ["reference_doctype", "reference_docname"], index_name="hm_reference")
	frappe.db.add_index("Mobile Notification", ["status", "scheduled_for"], index_name="hm_status_scheduled")
	frappe.db.add_index("Trial Booking", ["trainer", "start_datetime"], index_name="hm_trainer_start")


def before_uninstall():
	for fields in (CRM_TASK_FIELDS, CRM_LEAD_FIELDS):
		for dt, rows in fields.items():
			for fieldname in [f["fieldname"] for f in rows]:
				name = frappe.db.get_value("Custom Field", {"dt": dt, "fieldname": fieldname})
				if name:
					frappe.delete_doc("Custom Field", name, ignore_permissions=True)
