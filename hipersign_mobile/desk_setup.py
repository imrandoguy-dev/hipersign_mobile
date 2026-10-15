"""The "Hipersign Mobile" workspace on the computer (Desk): everything the app does, in one place.

Built in code on every migrate (instead of shipped JSON files) because Frappe 16 builds differ in
how the desktop menu is stored: this uses whatever the site has - Workspace, Workspace Sidebar and
Desktop Icon - and only links doctypes / reports that exist on the site. Safe to run repeatedly.
"""

from __future__ import annotations

import json

import frappe

NAME = "Hipersign Mobile"
APP = "hipersign_mobile"
ICON = "smartphone"

# (shortcut label, doctype or report, kind, colour)
SHORTCUTS = [
	("Follow-ups & Meetings", "CRM Task", "DocType", "Blue"),
	("Calls", "Mobile Call Log", "DocType", "Green"),
	("Calling Activity", "Calling Activity", "Report", "Orange"),
	("Call Recordings", "Call Recording", "DocType", "Purple"),
	("Trials", "Trial Booking", "DocType", "Cyan"),
	("App Settings", "Hipersign Mobile Settings", "DocType", "Grey"),
]

# section -> [(label, doctype or report, kind, sidebar icon)]
SECTIONS = [
	(
		"Sales work",
		"briefcase",
		[
			("Follow-ups & Meetings", "CRM Task", "DocType", "list-todo"),
			("Activity History", "Sales Activity History", "DocType", "history"),
			("Notes", "FCRM Note", "DocType", "notebook-pen"),
			("Leads", "CRM Lead", "DocType", "user-plus"),
			("Deals", "CRM Deal", "DocType", "handshake"),
			("Customers", "Customer", "DocType", "users-round"),
		],
	),
	(
		"Calls",
		"phone",
		[
			("Calls", "Mobile Call Log", "DocType", "phone"),
			("Calling Activity", "Calling Activity", "Report", "chart-column"),
			("Call Recordings", "Call Recording", "DocType", "mic"),
			("WhatsApp Messages", "WhatsApp Message", "DocType", "message-circle"),
		],
	),
	(
		"Trials & trainers",
		"graduation-cap",
		[
			("Trials", "Trial Booking", "DocType", "graduation-cap"),
			("Trainer Slots", "Trainer Slot", "DocType", "calendar-clock"),
			("Shift Types", "Shift Type", "DocType", "clock"),
			("Shift Assignments", "Shift Assignment", "DocType", "calendar-check"),
			("Holiday Lists", "Holiday List", "DocType", "calendar-x"),
		],
	),
	(
		"App setup",
		"settings",
		[
			("App Settings", "Hipersign Mobile Settings", "DocType", "settings"),
			("Phones", "Mobile Device", "DocType", "smartphone"),
			("Notifications", "Mobile Notification", "DocType", "bell"),
			("Notification Preferences", "Mobile User Preference", "DocType", "bell-ring"),
			("Role Profiles", "Role Profile", "DocType", "shield"),
			("WhatsApp Templates", "WhatsApp Templates", "DocType", "message-square-text"),
		],
	),
]


def _exists(kind: str, name: str) -> bool:
	return bool(frappe.db.exists("Report" if kind == "Report" else "DocType", name))


def _set(doc, values: dict):
	"""Only set fields this site's doctype has (Frappe 16 builds differ)."""
	for k, v in values.items():
		if doc.meta.has_field(k) or k in ("name",):
			doc.set(k, v)


def setup():
	try:
		_workspace()
		if frappe.db.exists("DocType", "Sidebar"):
			# Newer Frappe 16 navigation: each module gets its sidebar computed from its
			# workspace and doctypes, and the old Workspace Sidebar is a read-only archive.
			pass
		elif frappe.db.exists("DocType", "Workspace Sidebar"):
			_sidebar()
			if frappe.db.exists("DocType", "Desktop Icon"):
				_desktop_icon()
		frappe.clear_cache()
	except Exception:
		# Never let the menu break an update.
		frappe.log_error(title="Hipersign Mobile: could not set up the workspace")


def _workspace():
	doc = (
		frappe.get_doc("Workspace", NAME)
		if frappe.db.exists("Workspace", NAME)
		else frappe.new_doc("Workspace")
	)
	_set(
		doc,
		{
			"label": NAME,
			"title": NAME,
			"module": "Hipersign Mobile",
			"app": APP,
			"public": 1,
			"is_hidden": 0,
			"icon": ICON,
			"type": "Workspace",
			"indicator_color": "blue",
		},
	)
	if doc.meta.has_field("link_type"):
		doc.link_type = None  # a stale link_type can break the whole desk
	if doc.is_new():
		doc.name = NAME

	shortcuts = [s for s in SHORTCUTS if _exists(s[2], s[1])]
	doc.set("shortcuts", [])
	for label, target, kind, color in shortcuts:
		doc.append(
			"shortcuts",
			{
				"type": kind,
				"link_to": target,
				"label": label,
				"color": color,
				"doc_view": "List" if kind == "DocType" else "",
			},
		)

	doc.set("links", [])
	cards = []
	for section, _icon, links in SECTIONS:
		present = [x for x in links if _exists(x[2], x[1])]
		if not present:
			continue
		cards.append(section)
		doc.append("links", {"type": "Card Break", "label": section, "link_count": len(present), "hidden": 0})
		for label, target, kind, _i in present:
			doc.append(
				"links",
				{
					"type": "Link",
					"label": label,
					"link_type": kind,
					"link_to": target,
					"is_query_report": 1 if kind == "Report" else 0,
					"onboard": 0,
					"hidden": 0,
				},
			)

	blocks = [
		{
			"id": "hm-h1",
			"type": "header",
			"data": {"text": '<span class="h4"><b>Hipersign mobile app</b></span>', "col": 12},
		}
	]
	for i, (label, *_rest) in enumerate(shortcuts):
		blocks.append({"id": f"hm-s{i}", "type": "shortcut", "data": {"shortcut_name": label, "col": 4}})
	blocks.append({"id": "hm-sp", "type": "spacer", "data": {"col": 12}})
	blocks.append(
		{
			"id": "hm-h2",
			"type": "header",
			"data": {"text": '<span class="h4"><b>Everything in the app</b></span>', "col": 12},
		}
	)
	for i, card in enumerate(cards):
		blocks.append({"id": f"hm-c{i}", "type": "card", "data": {"card_name": card, "col": 4}})
	doc.content = json.dumps(blocks)
	doc.flags.ignore_links = True
	doc.save(ignore_permissions=True) if not doc.is_new() else doc.insert(ignore_permissions=True)


def _sidebar():
	dt = "Workspace Sidebar"
	doc = frappe.get_doc(dt, NAME) if frappe.db.exists(dt, NAME) else frappe.new_doc(dt)
	_set(doc, {"title": NAME, "header_icon": ICON, "app": APP, "module": "Hipersign Mobile", "standard": 0})
	if doc.is_new():
		doc.name = NAME
	doc.set("items", [])
	doc.append(
		"items",
		{
			"type": "Link",
			"label": "Home",
			"link_type": "Workspace",
			"link_to": NAME,
			"icon": "house",
			"child": 0,
			"indent": 0,
			"collapsible": 1,
		},
	)
	for section, icon, links in SECTIONS:
		present = [x for x in links if _exists(x[2], x[1])]
		if not present:
			continue
		doc.append(
			"items",
			{
				"type": "Section Break",
				"label": section,
				"icon": icon,
				"child": 0,
				"indent": 1,
				"collapsible": 1,
				"keep_closed": 0,
			},
		)
		for label, target, kind, link_icon in present:
			doc.append(
				"items",
				{
					"type": "Link",
					"label": label,
					"link_type": kind,
					"link_to": target,
					"icon": link_icon,
					"child": 1,
					"indent": 0,
					"collapsible": 1,
				},
			)
	doc.flags.ignore_links = True
	doc.save(ignore_permissions=True) if not doc.is_new() else doc.insert(ignore_permissions=True)


def _desktop_icon():
	dt = "Desktop Icon"
	doc = frappe.get_doc(dt, NAME) if frappe.db.exists(dt, NAME) else frappe.new_doc(dt)
	_set(
		doc,
		{
			"label": NAME,
			"icon_type": "Link",
			"link_type": "Workspace Sidebar",
			"link_to": NAME,
			"app": APP,
			"icon": ICON,
			"standard": 0,
			"hidden": 0,
			"bg_color": "blue",
		},
	)
	if doc.is_new():
		doc.name = NAME
	doc.flags.ignore_links = True
	doc.save(ignore_permissions=True) if not doc.is_new() else doc.insert(ignore_permissions=True)
