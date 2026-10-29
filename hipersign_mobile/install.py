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
		{
			"fieldname": "hm_meeting_group",
			"fieldtype": "Data",
			"label": "Meeting Group",
			"description": "Same value on every person's copy of one meeting.",
			"read_only": 1,
			"no_copy": 1,
			"search_index": 1,
			"insert_after": "hm_idempotency_key",
		},
		{
			"fieldname": "hm_reminder_offsets",
			"fieldtype": "Data",
			"label": "Reminders (Minutes Before)",
			"insert_after": "hm_completion_notes",
			"description": "Comma separated, e.g. 15,0 = 15 minutes before and at the start time. Empty = the person's own reminder setting.",
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
		{
			"fieldname": "hm_address",
			"fieldtype": "Small Text",
			"label": "Address",
			"insert_after": "hm_google_maps_url",
			"description": "Shown in the Hipersign app with a Directions button (Google Maps).",
		},
	],
	# Same fields on deals: Frappe CRM copies same-named fields when a lead becomes a deal.
	"CRM Deal": [
		{
			"fieldname": "hm_google_place_id",
			"fieldtype": "Data",
			"label": "Google Place ID",
			"insert_after": "source",
			"read_only": 1,
			"no_copy": 1,
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
		{
			"fieldname": "hm_address",
			"fieldtype": "Small Text",
			"label": "Address",
			"insert_after": "hm_google_maps_url",
		},
	],
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
	add_whatsapp_template_field()
	add_quotation_lead_field()
	default_quotation_company()
	add_shift_break_fields()
	backfill_trial_periods()
	move_trials_to_trainers()
	from hipersign_mobile import desk_setup

	desk_setup.setup()  # "Hipersign Mobile" workspace on the computer
	backfill_lead_addresses()
	apply_new_setting_defaults()
	from hipersign_mobile.services import whatsapp_chat

	whatsapp_chat.backfill_chats()  # chat list for messages received before this version
	from hipersign_mobile.api.calls import backfill_parties

	backfill_parties()  # calls to numbers that became leads later
	frappe.clear_cache(doctype="CRM Task")
	# install-app marks patches as done without running them, so backfill here too (idempotent).
	from hipersign_mobile.patches.v0_1.set_activity_type_for_existing_tasks import execute as backfill_types

	backfill_types()


# Settings added in later versions: on an existing site the new fields start empty (an
# unticked checkbox), so give them their intended default once - never overriding a saved value.
NEW_SETTING_DEFAULTS = {
	"enable_call_recordings": "1",
	"max_recording_mb": "50",
	"push_show_details": "1",
	"auto_trainer_slots": "1",
	"trainer_working_days": "Mon,Tue,Wed,Thu,Fri,Sat",
	"trainer_day_start": "09:30:00",
	"trainer_day_end": "18:00:00",
	"trial_slot_minutes": "60",
	"lunch_break_start": "13:00:00",
	"lunch_break_end": "14:00:00",
	"enable_whatsapp_chat": "1",
	"whatsapp_max_media_mb": "16",
	"dev_default_reminders": "1440",
	"dev_morning_alert_time": "09:30:00",
}


def apply_new_setting_defaults():
	doctype = "Hipersign Mobile Settings"
	saved = set(frappe.db.sql("select field from `tabSingles` where doctype=%s", doctype, pluck=True))
	changed = False
	for field, value in NEW_SETTING_DEFAULTS.items():
		if field not in saved:
			frappe.db.set_single_value(doctype, field, value)
			changed = True
	if changed:
		frappe.clear_document_cache(doctype, doctype)


def backfill_lead_addresses():
	"""Leads created from Google before the Address field existed: the address is in the
	"Found on Google Maps: ..." comment. Copy it once (never overwrites a typed address)."""
	import html
	import re

	rows = frappe.db.sql(
		"""select c.reference_name, c.content from `tabComment` c
		join `tabCRM Lead` l on l.name = c.reference_name
		where c.reference_doctype = 'CRM Lead' and c.content like 'Found on Google Maps:%%'
		and ifnull(l.hm_address, '') = ''""",
		as_dict=True,
	)
	for r in rows:
		text = r.content[len("Found on Google Maps:") :]
		text = re.split(r"\s+-\s+<a\s", text, maxsplit=1)[0]
		text = html.unescape(re.sub(r"<[^>]+>", "", text)).strip()
		if text and not text.startswith("Open in Google Maps"):
			frappe.db.set_value("CRM Lead", r.reference_name, "hm_address", text[:500], update_modified=False)


def move_trials_to_trainers():
	"""A trial's calendar activity belongs to the trainer (it used to be the salesperson's)."""
	try:
		frappe.db.sql(
			"""update `tabCRM Task` t join `tabTrial Booking` b on b.name = t.hm_trial_booking
			set t.assigned_to = b.trainer
			where b.trainer is not null and b.trainer != '' and t.assigned_to != b.trainer"""
		)
	except Exception:
		frappe.log_error(title="Hipersign: move trials to trainers")


def backfill_trial_periods():
	"""Bookings made before trial periods existed: each one is a 1-day trial."""
	frappe.db.sql(
		"""update `tabTrial Booking`
		set trial_series = name, session_no = 1, trial_days = 1,
			trial_start_date = date(start_datetime), trial_end_date = date(start_datetime)
		where ifnull(trial_series, '') = '' and start_datetime is not null"""
	)


def add_shift_break_fields():
	"""Lunch break per shift (HRMS Shift Type) - only when HRMS is installed."""
	if not frappe.db.exists("DocType", "Shift Type"):
		return
	create_custom_fields(
		{
			"Shift Type": [
				{
					"fieldname": "hm_break_start",
					"fieldtype": "Time",
					"label": "Lunch Break From",
					"insert_after": "end_time",
					"description": "Hipersign: trainers on this shift are not offered for trials during the break. Empty = the break in Hipersign Mobile Settings.",
				},
				{
					"fieldname": "hm_break_end",
					"fieldtype": "Time",
					"label": "Lunch Break To",
					"insert_after": "hm_break_start",
				},
			]
		},
		update=True,
	)


def default_quotation_company():
	"""Quotations start with "Hipersign ERP" when that company exists and nothing is chosen yet."""
	try:
		meta = frappe.get_meta("Hipersign Mobile Settings")
		if not meta.has_field("quotation_company"):
			return
		if not frappe.db.get_single_value("Hipersign Mobile Settings", "quotation_company") and frappe.db.exists(
			"Company", "Hipersign ERP"
		):
			frappe.db.set_single_value("Hipersign Mobile Settings", "quotation_company", "Hipersign ERP")
	except Exception:
		frappe.log_error(title="Hipersign: default quotation company")


def add_quotation_lead_field():
	"""Quotations to a CRM Lead go to an ERPNext Lead made for it; this links the two."""
	if not frappe.db.exists("DocType", "Lead"):
		return
	create_custom_fields(
		{
			"Lead": [
				{
					"fieldname": "hm_crm_lead",
					"fieldtype": "Link",
					"label": "CRM Lead",
					"options": "CRM Lead",
					"insert_after": "lead_name",
					"read_only": 1,
					"search_index": 1,
					"description": "Made by Hipersign for a quotation to this CRM lead.",
				}
			]
		},
		update=True,
	)


def add_whatsapp_template_field():
	"""The rule's template is a Link to Frappe WhatsApp's templates - only when that app is installed."""
	if not frappe.db.exists("DocType", "WhatsApp Templates"):
		return
	create_custom_fields(
		{
			"Hipersign WhatsApp Rule": [
				{
					"fieldname": "whatsapp_template",
					"fieldtype": "Link",
					"label": "WhatsApp Template",
					"options": "WhatsApp Templates",
					"insert_after": "trigger",
					"in_list_view": 1,
					"columns": 3,
					"description": "An APPROVED template from WhatsApp Templates.",
				}
			]
		},
		update=True,
	)


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
		("Hipersign Cold Caller", ("read", "write")),
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
		("Hipersign Cold Caller", ("read", "write", "create")),
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
	name = frappe.db.get_value(
		"Custom Field", {"dt": "Hipersign WhatsApp Rule", "fieldname": "whatsapp_template"}
	)
	if name:
		frappe.delete_doc("Custom Field", name, ignore_permissions=True)
	for fieldname in ("hm_break_start", "hm_break_end"):
		name = frappe.db.get_value("Custom Field", {"dt": "Shift Type", "fieldname": fieldname})
		if name:
			frappe.delete_doc("Custom Field", name, ignore_permissions=True)
