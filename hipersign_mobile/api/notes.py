"""Notes on a customer, lead or deal - the same FCRM Note records Frappe CRM shows.

A note added in the app appears in Frappe CRM's Notes tab (and the other way round).
A deal also shows the notes written on the lead it came from.
"""

from __future__ import annotations

import html
import re

import frappe

from hipersign_mobile.api._common import choice, limit_per_user, mobile_api, optional_str, require_str
from hipersign_mobile.permissions import _sees_all_recordings as _is_manager
from hipersign_mobile.services import permissions as perms
from hipersign_mobile.services import timeutil

MAX_LEN = 20000


def html_to_text(value: str | None) -> str:
	"""Note HTML (Text Editor) -> readable plain text with line breaks kept."""
	if not value:
		return ""
	text = re.sub(r"(?i)<br\s*/?>", "\n", value)
	text = re.sub(r"(?i)</(p|div|h[1-6]|blockquote|ul|ol)>", "\n\n", text)
	text = re.sub(r"(?i)</li>", "\n", text)
	text = re.sub(r"(?i)<li[^>]*>", "• ", text)
	text = re.sub(r"<[^>]+>", "", text)
	text = html.unescape(text).replace("\xa0", " ")
	text = re.sub(r"\n{3,}", "\n\n", text)
	return text.strip()


def text_to_html(text: str) -> str:
	"""Plain text typed on the phone -> simple HTML paragraphs for Frappe CRM."""
	paragraphs = [p for p in re.split(r"\n\s*\n", text.strip()) if p.strip()]
	return "".join(
		"<p>" + "<br>".join(html.escape(line) for line in p.split("\n")) + "</p>" for p in paragraphs
	)


def _title_from(text: str) -> str:
	first = text.strip().split("\n", 1)[0].strip()
	return (first[:60] + "...") if len(first) > 60 else first or "Note"


def _serialize(n, me: str, manager: bool) -> dict:
	return {
		"id": n.name,
		"title": n.title,
		"text": html_to_text(n.content),
		"reference_doctype": n.reference_doctype,
		"reference_docname": n.reference_docname,
		"owner": n.owner,
		"owner_name": frappe.utils.get_fullname(n.owner),
		"created": timeutil.to_iso(n.creation),
		"modified": timeutil.to_iso(n.modified),
		"can_edit": manager or n.owner == me,
	}


def _note_for(note_id: str, me: str, write: bool = False):
	if not note_id or not frappe.db.exists("FCRM Note", note_id):
		raise frappe.DoesNotExistError("Note not found.")
	note = frappe.get_doc("FCRM Note", note_id)
	if note.reference_doctype not in perms.PARTY_DOCTYPES or not perms.can_access_party(
		note.reference_doctype, note.reference_docname, me
	):
		raise frappe.DoesNotExistError("Note not found.")
	if write and not (_is_manager(me) or note.owner == me):
		raise frappe.PermissionError("You can only change your own notes.")
	return note


@mobile_api(methods=("GET",))
def get_list(doctype: str | None = None, name: str | None = None):
	"""Newest first. For a deal, the notes of the lead it was converted from are included."""
	me = frappe.session.user
	perms.require("customers", me)
	doctype = choice(doctype, perms.PARTY_DOCTYPES, "doctype")
	name = require_str(name, "name")
	perms.require_party(doctype, name, me)

	refs = [(doctype, name)]
	if doctype == "CRM Deal":
		lead = frappe.db.get_value("CRM Deal", name, "lead")
		if lead:
			refs.append(("CRM Lead", lead))
	cond = " or ".join(["(reference_doctype = %s and reference_docname = %s)"] * len(refs))
	params = [v for pair in refs for v in pair]
	rows = frappe.db.sql(
		f"""select name, title, content, reference_doctype, reference_docname, owner, creation, modified
		from `tabFCRM Note` where {cond} order by creation desc limit 200""",
		params,
		as_dict=True,
	)
	manager = _is_manager(me)
	return {"items": [_serialize(r, me, manager) for r in rows]}


@mobile_api(methods=("POST",))
def add(doctype: str | None = None, name: str | None = None, text: str | None = None, title: str | None = None):
	me = frappe.session.user
	perms.require("customers", me)
	limit_per_user("notes_add", 30, 60)
	doctype = choice(doctype, perms.PARTY_DOCTYPES, "doctype")
	name = require_str(name, "name")
	perms.require_party(doctype, name, me)
	text = require_str(text, "Note", MAX_LEN)
	note = frappe.get_doc(
		{
			"doctype": "FCRM Note",
			"title": (optional_str(title, 140) or _title_from(text)),
			"content": text_to_html(text),
			"reference_doctype": doctype,
			"reference_docname": name,
		}
	).insert(ignore_permissions=True)
	return _serialize(note, me, _is_manager(me))


@mobile_api(methods=("POST",))
def update(id: str | None = None, text: str | None = None, title: str | None = None):
	me = frappe.session.user
	perms.require("customers", me)
	note = _note_for(id, me, write=True)
	text = require_str(text, "Note", MAX_LEN)
	note.content = text_to_html(text)
	note.title = optional_str(title, 140) or _title_from(text)
	note.save(ignore_permissions=True)
	return _serialize(note, me, _is_manager(me))


@mobile_api(methods=("POST",))
def delete(id: str | None = None):
	me = frappe.session.user
	perms.require("customers", me)
	note = _note_for(id, me, write=True)
	frappe.delete_doc("FCRM Note", note.name, ignore_permissions=True)
	return {"deleted": note.name}

