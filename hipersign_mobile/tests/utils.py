"""Fixtures shared by the backend tests."""

from __future__ import annotations

import datetime as dt

import frappe

from hipersign_mobile.services import permissions as perms
from hipersign_mobile.services import timeutil

USERS = {
	"sales1": ("hm.sales1@example.com", [perms.ROLE_SALES]),
	"sales2": ("hm.sales2@example.com", [perms.ROLE_SALES]),
	"manager": ("hm.manager@example.com", [perms.ROLE_MANAGER]),
	"trainer": ("hm.trainer@example.com", [perms.ROLE_TRAINER]),
	"trainer2": ("hm.trainer2@example.com", [perms.ROLE_TRAINER]),
	"norole": ("hm.norole@example.com", []),
	"dev1": ("hm.dev1@example.com", [perms.ROLE_DEVELOPER]),
	"dev2": ("hm.dev2@example.com", [perms.ROLE_DEVELOPER]),
	"ceo": ("hm.ceo@example.com", [perms.ROLE_CEO]),
	"hr": ("hm.hr@example.com", [perms.ROLE_HR]),
	"caller": ("hm.caller@example.com", [perms.ROLE_COLD_CALLER]),
	"accounts": ("hm.accounts@example.com", ["Employee"]),
}


def make_user(key: str) -> str:
	email, roles = USERS[key]
	if not frappe.db.exists("User", email):
		u = frappe.get_doc(
			{
				"doctype": "User",
				"email": email,
				"first_name": key.title(),
				"send_welcome_email": 0,
				"user_type": "System User",
				"enabled": 1,
			}
		)
		u.insert(ignore_permissions=True)
		u.add_roles(*roles) if roles else None
		frappe.db.commit()
	return email


def ensure_masters():
	for doctype, name, extra in (
		(
			"Customer Group",
			"All Customer Groups",
			{"customer_group_name": "All Customer Groups", "is_group": 1},
		),
		("Territory", "All Territories", {"territory_name": "All Territories", "is_group": 1}),
		(
			"Customer Group",
			"HM Group",
			{"customer_group_name": "HM Group", "parent_customer_group": "All Customer Groups"},
		),
		(
			"Territory",
			"HM Territory",
			{"territory_name": "HM Territory", "parent_territory": "All Territories"},
		),
	):
		if not frappe.db.exists(doctype, name):
			frappe.get_doc({"doctype": doctype, **extra}).insert(ignore_permissions=True)


def make_customer(name: str, phone: str | None = None) -> str:
	ensure_masters()
	if not frappe.db.exists("Customer", name):
		c = frappe.get_doc(
			{
				"doctype": "Customer",
				"customer_name": name,
				"customer_group": "HM Group",
				"territory": "HM Territory",
				"customer_type": "Company",
			}
		).insert(ignore_permissions=True)
		if phone:
			frappe.get_doc(
				{
					"doctype": "Contact",
					"first_name": name,
					"phone_nos": [{"phone": phone, "is_primary_mobile_no": 1}],
					"links": [{"link_doctype": "Customer", "link_name": c.name}],
				}
			).insert(ignore_permissions=True)
		frappe.db.commit()
	return name


def make_lead(first_name: str, mobile: str | None = None, owner: str | None = None) -> str:
	existing = frappe.db.get_value("CRM Lead", {"first_name": first_name}, "name")
	if existing:
		return existing
	name = (
		frappe.get_doc(
			{
				"doctype": "CRM Lead",
				"first_name": first_name,
				"mobile_no": mobile,
				"lead_owner": owner,
				"status": "New",
			}
		)
		.insert(ignore_permissions=True)
		.name
	)
	frappe.db.commit()
	return name


def make_task(
	assigned_to: str, due: dt.datetime | None, title="Test follow-up", ref=("Customer", None), **extra
):
	doc = frappe.get_doc(
		{
			"doctype": "CRM Task",
			"title": title,
			"status": "Todo",
			"assigned_to": assigned_to,
			"due_date": due,
			"reference_doctype": ref[0],
			"reference_docname": ref[1],
			**extra,
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.commit()
	return doc


def make_slot(trainer: str, start: dt.datetime, minutes: int = 60, slot_type="Both") -> str:
	frappe.set_user("Administrator")
	name = (
		frappe.get_doc(
			{
				"doctype": "Trainer Slot",
				"trainer": trainer,
				"start_datetime": start,
				"end_datetime": start + dt.timedelta(minutes=minutes),
				"slot_type": slot_type,
				"status": "Available",
			}
		)
		.insert(ignore_permissions=True)
		.name
	)
	frappe.db.commit()
	return name


def future(days=1, hour=10, minute=0) -> dt.datetime:
	base = timeutil.now_system().replace(hour=hour, minute=minute, second=0, microsecond=0)
	return base + dt.timedelta(days=days)


def call(fn, _as_user: str, **kwargs) -> dict:
	"""Call a mobile endpoint as `_as_user`, like an HTTP request would."""
	frappe.set_user(_as_user)
	frappe.local.response = frappe._dict({"docs": []})
	frappe.local.hm_party_cache = {}
	try:
		out = fn(**kwargs)
	finally:
		status = frappe.local.response.get("http_status_code") or 200
		frappe.set_user("Administrator")
	out["_http_status"] = status
	return out
