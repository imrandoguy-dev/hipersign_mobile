"""Session & profile.

Sign-in uses Frappe's standard endpoint so lockouts, IP restrictions and 2FA
policies keep applying:

    POST /api/method/login   {"usr": "...", "pwd": "..."}   -> sid cookie

The app keeps the sid in Keychain / Keystore-backed secure storage and sends it
as a Cookie header. No API keys or secrets are ever embedded in the app.
"""

from __future__ import annotations

import frappe

import hipersign_mobile
from hipersign_mobile.api._common import API_VERSION, mobile_api
from hipersign_mobile.services import permissions as perms
from hipersign_mobile.services import settings as hm_settings
from hipersign_mobile.services import timeutil


@mobile_api(methods=("GET",))
def get_me():
	user = frappe.session.user
	caps = perms.capabilities(user)
	info = (
		frappe.db.get_value("User", user, ["full_name", "user_image", "email", "mobile_no"], as_dict=True)
		or {}
	)
	roles = sorted(
		perms.user_roles(user) & (set(perms.MOBILE_ROLES) | {"System Manager", "Sales Manager", "Sales User"})
	)
	return {
		"user": user,
		"full_name": info.get("full_name") or user,
		"email": info.get("email"),
		"image": info.get("user_image"),
		"roles": roles,
		"capabilities": {cap: cap in caps for cap in perms.ALL_CAPS},
		"workspaces": perms.workspaces(caps),
		"timezone": str(timeutil.user_tz(user)),
		"server_time": timeutil.to_iso(timeutil.now_system()),
	}


@mobile_api(methods=("GET",), allow_guest=True)
def get_config():
	"""Public, non-sensitive configuration (used before sign-in to validate the server URL)."""
	s = hm_settings.get()
	return {
		"api_version": API_VERSION,
		"app_version": hipersign_mobile.__version__,
		"site_name": frappe.local.site,
		"company_name": frappe.db.get_single_value("Website Settings", "app_name") or "Hipersign",
		"default_country_code": s.default_country_code,
		"default_activity_minutes": s.default_activity_minutes,
		"push_enabled": bool(s.enable_push),
		"min_supported_app_version": "0.1.0",
	}


@mobile_api(methods=("POST",))
def logout_mobile(device_id: str | None = None):
	"""Deactivate this phone's push registration, then end the session."""
	user = frappe.session.user
	if device_id:
		for name in frappe.get_all(
			"Mobile Device", filters={"user": user, "device_id": device_id}, pluck="name"
		):
			frappe.db.set_value("Mobile Device", name, "is_active", 0)
	frappe.local.login_manager.logout()
	frappe.db.commit()
	return {"logged_out": True}
