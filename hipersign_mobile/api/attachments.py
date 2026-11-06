"""Files and voice notes on tasks, development work, trials / trainings, meetings / follow-ups,
customers, suggestions and concerns.

Stored as ordinary private Frappe Files attached to the record, so they also show in ERPNext
(the paperclip on the form). Who may see / add them follows the record itself.
"""

from __future__ import annotations

import mimetypes

import frappe

from hipersign_mobile.api._common import choice, mobile_api, require_str
from hipersign_mobile.services import permissions as perms
from hipersign_mobile.services import timeutil

DOCTYPES = (
	"Development Task", "Team Task", "Trial Booking", "Hipersign Feedback", "Hipersign Concern",
	"Customer", "CRM Lead", "CRM Deal",  # voice notes / files on a customer
	"CRM Task",  # meetings / follow-ups (one set of files shared by everyone in a meeting)
)
MAX_MB = 50  # the web server's own upload limit is about this size too
AUDIO = ("m4a", "aac", "mp3", "ogg", "opus", "wav", "amr", "3gp")
IMAGE = ("jpg", "jpeg", "png", "webp", "gif", "heic")
VIDEO = ("mp4", "mov", "mkv", "webm")


def _record(doctype: str, name: str, user: str, write: bool = False):
	"""The record, when `user` may see it (and add files to it when write=True); else raises."""
	doctype = choice(doctype, DOCTYPES, "doctype")
	name = require_str(name, "Record", 140)
	if not frappe.db.exists(doctype, name):
		raise frappe.DoesNotExistError("Not found.")
	if doctype in perms.PARTY_DOCTYPES:
		perms.require_party(doctype, name, user)  # raises when not allowed
		return frappe.get_doc(doctype, name)
	if doctype == "CRM Task":
		return _task_record(name, user)
	doc = frappe.get_doc(doctype, name)
	if doctype in ("Development Task", "Team Task"):
		from hipersign_mobile.services import development as dev

		ok = dev.can_see(doc, user)
	elif doctype == "Trial Booking":
		from hipersign_mobile.services import booking_service

		booking_service._get_booking_for(user, name)  # raises when not allowed
		ok = True
	elif doctype == "Hipersign Feedback":
		ok = "feedback" in perms.capabilities(user)
		if write:
			ok = ok and (doc.posted_by == user or "team_view" in perms.capabilities(user))
	else:
		from hipersign_mobile.permissions import concern_has_permission

		ok = concern_has_permission(doc, "read", user)
	if not ok:
		raise frappe.DoesNotExistError("Not found.")
	return doc


def _task_record(name: str, user: str):
	"""A meeting's files live on the meeting's first copy, so everyone in it sees the same ones."""
	task = frappe.get_doc("CRM Task", name)
	group = task.get("hm_meeting_group")
	ok = perms.can_access_task(task, user)
	if not ok and group:
		ok = bool(frappe.db.exists("CRM Task", {"hm_meeting_group": group, "assigned_to": user}))
	if not ok:
		raise frappe.DoesNotExistError("Not found.")
	if group and str(group) != str(task.name) and frappe.db.exists("CRM Task", group):
		return frappe.get_doc("CRM Task", group)
	return task


def kind_of(file_name: str) -> str:
	ext = (file_name.rsplit(".", 1)[-1] if "." in file_name else "").lower()
	if ext in AUDIO:
		return "audio"
	if ext in IMAGE:
		return "image"
	if ext in VIDEO:
		return "video"
	return "document"


def serialize(f) -> dict:
	return {
		"id": f.name,
		"name": f.file_name,
		"kind": kind_of(f.file_name or ""),
		"size": f.file_size,
		"by": frappe.utils.get_fullname(f.owner),
		"by_user": f.owner,
		"at": timeutil.to_iso(f.creation),
	}


def files_of(doctype: str, name: str) -> list[dict]:
	rows = frappe.get_all(
		"File",
		filters={"attached_to_doctype": doctype, "attached_to_name": name, "is_folder": 0},
		fields=["name", "file_name", "file_size", "owner", "creation"],
		order_by="creation asc",
		limit=100,
	)
	return [serialize(r) for r in rows]


@mobile_api(methods=("GET",))
def list_files(doctype: str | None = None, name: str | None = None):
	me = frappe.session.user
	doc = _record(doctype, name, me)
	return {"items": files_of(doc.doctype, doc.name)}


@mobile_api(methods=("POST",))
def upload(doctype: str | None = None, name: str | None = None, voice: str | int | None = None):
	"""Multipart `file`. voice=1 names a recorded voice note "Voice note <time>.m4a"."""
	me = frappe.session.user
	doc = _record(doctype, name, me, write=True)
	f = frappe.request.files.get("file") if frappe.request and frappe.request.files else None
	if not f:
		raise frappe.ValidationError("Choose a file.")
	max_bytes = MAX_MB * 1024 * 1024
	content = f.stream.read(max_bytes + 1)
	if not content:
		raise frappe.ValidationError("The file is empty.")
	if len(content) > max_bytes:
		raise frappe.ValidationError(f"The file is too big (max {MAX_MB} MB).")
	file_name = (f.filename or "file").replace("/", "_").replace("\\", "_")[-120:]
	if str(voice) in ("1", "true", "True"):
		ext = file_name.rsplit(".", 1)[-1] if "." in file_name else "m4a"
		stamp = frappe.utils.format_datetime(timeutil.now_system(), "d MMM h.mm a")
		file_name = f"Voice note {stamp}.{ext}"
	# Frappe's own file limit (System Settings, default 25 MB) would refuse videos; the size is
	# already checked above for these attachments.
	import frappe.core.api.file as file_api

	site_limit = file_api.get_max_file_size
	file_api.get_max_file_size = lambda: max(site_limit(), max_bytes)
	try:
		saved = frappe.get_doc(
			{
				"doctype": "File",
				"file_name": file_name,
				"attached_to_doctype": doc.doctype,
				"attached_to_name": doc.name,
				"is_private": 1,
				"content": content,
			}
		).insert(ignore_permissions=True)
	except (frappe.ValidationError, frappe.PermissionError):
		raise
	except Exception:
		frappe.log_error(title="Hipersign attachment upload")
		raise frappe.ValidationError("This file could not be saved. Is it damaged? Try another file.")
	finally:
		file_api.get_max_file_size = site_limit
	doc.add_comment("Attachment", f"{frappe.utils.get_fullname(me)} added {frappe.utils.escape_html(file_name)}")
	return serialize(frappe.db.get_value("File", saved.name, ["name", "file_name", "file_size", "owner", "creation"], as_dict=True))


@mobile_api(methods=("POST",))
def delete_file(file_id: str | None = None):
	me = frappe.session.user
	fid = require_str(file_id, "File", 140)
	f = frappe.db.get_value("File", fid, ["name", "attached_to_doctype", "attached_to_name", "owner"], as_dict=True)
	if not f or f.attached_to_doctype not in DOCTYPES:
		raise frappe.DoesNotExistError("File not found.")
	_record(f.attached_to_doctype, f.attached_to_name, me)
	if f.owner != me and "team_view" not in perms.capabilities(me):
		raise frappe.PermissionError("Only the person who added this file can remove it.")
	frappe.delete_doc("File", f.name, ignore_permissions=True)
	return {"deleted": f.name}


# ---------------------------------------------------------------- everything on one customer

def _party_records(doctype: str, name: str) -> list[tuple[str, str, str, str]]:
	"""(section, doctype, name, label) of every record about this customer / lead / deal."""
	from hipersign_mobile.services import journey

	rel = journey.related(doctype, name)
	out = []
	labels = {"Customer": "Customer", "CRM Lead": "Lead", "CRM Deal": "Deal"}
	for dt in ("Customer", "CRM Deal", "CRM Lead"):
		for n in sorted(rel[dt]):
			out.append(("Customer", dt, n, labels[dt]))
	refs = [(dt, list(names)) for dt, names in rel.items() if names]
	seen = set()
	for dt, names in refs:
		for t in frappe.get_all(
			"CRM Task",
			filters={"reference_doctype": dt, "reference_docname": ["in", names]},
			fields=["name", "title", "hm_activity_type", "due_date", "hm_meeting_group"],
			order_by="due_date desc",
			limit=500,
		):
			key = str(t.hm_meeting_group or t.name)
			if key in seen:
				continue
			seen.add(key)
			section = {"Meeting": "Meetings", "Trial": "Trainings"}.get(t.hm_activity_type, "Follow-ups")
			when = frappe.utils.format_date(t.due_date, "d MMM") if t.due_date else ""
			out.append((section, "CRM Task", key, " · ".join(x for x in (t.title, when) if x)))
	customers = list(rel["Customer"])
	if customers:
		for dt, section in (("Development Task", "Development"), ("Team Task", "Tasks")):
			if frappe.db.exists("DocType", dt):
				for r in frappe.get_all(dt, filters={"customer": ["in", customers]}, fields=["name", "title"], limit=500):
					out.append((section, dt, r.name, r.title or r.name))
	trial_or = []
	if customers:
		trial_or.append(["customer", "in", customers])
	if rel["CRM Lead"]:
		trial_or.append(["lead", "in", list(rel["CRM Lead"])])
	if trial_or and frappe.db.exists("DocType", "Trial Booking"):
		for r in frappe.get_all("Trial Booking", or_filters=trial_or, fields=["name", "start_datetime"], limit=500):
			when = frappe.utils.format_date(r.start_datetime, "d MMM") if r.start_datetime else r.name
			out.append(("Trainings", "Trial Booking", r.name, f"Trial booking · {when}"))
	return out


SECTIONS = ("Customer", "Meetings", "Follow-ups", "Trainings", "Development", "Tasks")


def party_files(doctype: str, name: str) -> list[dict]:
	records = _party_records(doctype, name)
	by_record = {}
	for sec, dt, n, label in records:
		by_record.setdefault((dt, str(n)), (sec, label))
	sections = {s: [] for s in SECTIONS}
	for dt in {dt for _, dt, _, _ in records}:
		names = [n for (d, n) in by_record if d == dt]
		for f in frappe.get_all(
			"File",
			filters={"attached_to_doctype": dt, "attached_to_name": ["in", names], "is_folder": 0},
			fields=["name", "file_name", "file_size", "owner", "creation", "attached_to_name"],
			order_by="creation desc",
			limit=500,
		):
			sec, label = by_record[(dt, str(f.attached_to_name))]
			item = serialize(f)
			item["record"] = label
			item["record_doctype"] = dt
			item["record_name"] = f.attached_to_name
			sections[sec].append(item)
	return [{"title": s, "items": sections[s]} for s in SECTIONS if sections[s]]


@mobile_api(methods=("GET",))
def customer_files(doctype: str | None = None, name: str | None = None):
	"""All voice notes and files about a customer, section by section (customer, meetings,
	follow-ups, trainings, development, tasks). Anyone who can open the customer sees them."""
	me = frappe.session.user
	doctype = choice(doctype, perms.PARTY_DOCTYPES, "doctype")
	name = require_str(name, "Customer", 140)
	perms.require_party(doctype, name, me)
	sections = party_files(doctype, name)
	return {"sections": sections, "count": sum(len(s["items"]) for s in sections)}


def _can_open(f, user: str, party_doctype=None, party_name=None):
	try:
		_record(f.attached_to_doctype, f.attached_to_name, user)
		return
	except (frappe.DoesNotExistError, frappe.PermissionError):
		if not (party_doctype and party_name):
			raise
	# Opened from the customer's page: allowed when the file is about that customer.
	party_doctype = choice(party_doctype, perms.PARTY_DOCTYPES, "party_doctype")
	perms.require_party(party_doctype, party_name, user)
	ids = {i["id"] for s in party_files(party_doctype, party_name) for i in s["items"]}
	if f.name not in ids:
		raise frappe.DoesNotExistError("File not found.")


@frappe.whitelist(methods=["GET"])
def file(file_id: str | None = None, party_doctype: str | None = None, party_name: str | None = None):
	"""Raw bytes of an attachment (same visibility as its record, or its customer's page)."""
	perms.require_active_user()
	me = frappe.session.user
	fid = require_str(file_id, "File", 140)
	f = frappe.db.get_value("File", fid, ["name", "attached_to_doctype", "attached_to_name", "file_name"], as_dict=True)
	if not f or f.attached_to_doctype not in DOCTYPES:
		raise frappe.DoesNotExistError("File not found.")
	_can_open(f, me, party_doctype, party_name)
	doc = frappe.get_doc("File", f.name)
	with open(doc.get_full_path(), "rb") as fh:
		content = fh.read()
	frappe.local.response.filename = f.file_name
	frappe.local.response.filecontent = content
	frappe.local.response.type = "download"
	frappe.local.response.display_content_as = "inline"
	frappe.local.response.content_type = mimetypes.guess_type(f.file_name or "")[0] or "application/octet-stream"
