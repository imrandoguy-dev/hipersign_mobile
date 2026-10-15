from __future__ import annotations

import frappe
from frappe.utils import cint

DEFAULTS = {
	"default_country_code": "91",
	"customer_scope": "All Records",
	"restrict_task_visibility": 0,
	"default_activity_minutes": 30,
	"max_page_size": 50,
	"enable_push": 0,
	"default_reminder_minutes": 15,
	"max_push_attempts": 5,
	"notification_retention_days": 60,
	"enable_call_recordings": 1,
	"max_recording_mb": 50,
	"enable_google_lookup": 0,
	"google_lead_source": "Google Maps",
	"push_show_details": 1,
	"auto_trainer_slots": 1,
	"trainer_working_days": "Mon,Tue,Wed,Thu,Fri,Sat",
	"trainer_day_start": "09:30:00",
	"trainer_day_end": "18:00:00",
	"trial_slot_minutes": 60,
	"lunch_break_start": "13:00:00",
	"lunch_break_end": "14:00:00",
	"enable_whatsapp_chat": 1,
	"whatsapp_max_media_mb": 16,
}


def get() -> frappe._dict:
	"""Settings with sane fall-backs (works even before the Single is saved)."""
	try:
		doc = frappe.get_cached_doc("Hipersign Mobile Settings")
		values = {k: doc.get(k) for k in DEFAULTS}
	except Exception:
		values = {}
	out = frappe._dict(DEFAULTS)
	for key, value in values.items():
		if value not in (None, ""):
			out[key] = value
	for key in (
		"default_activity_minutes",
		"max_page_size",
		"default_reminder_minutes",
		"max_push_attempts",
		"notification_retention_days",
		"max_recording_mb",
		"trial_slot_minutes",
		"whatsapp_max_media_mb",
	):
		out[key] = cint(out[key]) or DEFAULTS[key]
	out.enable_push = cint(out.enable_push)
	out.enable_call_recordings = cint(out.enable_call_recordings)
	out.enable_google_lookup = cint(out.enable_google_lookup)
	out.push_show_details = cint(out.push_show_details)
	out.auto_trainer_slots = cint(out.auto_trainer_slots)
	out.enable_whatsapp_chat = cint(out.enable_whatsapp_chat)
	out.restrict_task_visibility = cint(out.restrict_task_visibility)
	out.default_country_code = "".join(ch for ch in str(out.default_country_code) if ch.isdigit()) or "91"
	return out
