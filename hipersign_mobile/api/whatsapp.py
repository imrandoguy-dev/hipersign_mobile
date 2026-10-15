"""WhatsApp chats for the app: list, messages, replies, templates, photos / files / voice."""

from __future__ import annotations

import json

import frappe
from frappe.utils import cint

from hipersign_mobile.api._common import (
	limit_per_user,
	mobile_api,
	optional_str,
	page_args,
	page_result,
	require_str,
)
from hipersign_mobile.services import permissions as perms
from hipersign_mobile.services import whatsapp_chat as wc


def _viewer() -> wc.Viewer:
	wc.require_enabled()
	perms.require("whatsapp")
	return wc.Viewer(frappe.session.user)


@mobile_api(methods=("GET",))
def chats(query: str | None = None, unread: str | int | None = None, cursor=None, page_size=None):
	"""Newest chats first (only the ones this user may see)."""
	viewer = _viewer()
	offset, limit = page_args(cursor, page_size)
	filters = []
	if cint(unread):
		filters.append(["unread_count", ">", 0])
	or_filters = None
	q = (query or "").strip()
	if q:
		digits = "".join(ch for ch in q if ch.isdigit())
		or_filters = [["title", "like", f"%{q}%"], ["profile_name", "like", f"%{q}%"]]
		if len(digits) >= 3:
			or_filters.append(["phone", "like", f"%{digits}%"])
	rows = frappe.get_all(
		wc.CHAT,
		filters=filters,
		or_filters=or_filters,
		fields=["*"],
		order_by="last_message_at desc",
		limit=1000,
	)
	visible = [r for r in rows if viewer.can_see(r)]
	page = visible[offset : offset + limit + 1]
	return page_result([wc.chat_payload(r) for r in page[:limit]], offset, limit, len(page) > limit)


@mobile_api(methods=("GET",))
def unread_count():
	"""For the badge on the WhatsApp tab."""
	if not wc.enabled() or "whatsapp" not in perms.capabilities():
		return {"chats": 0, "messages": 0}
	viewer = wc.Viewer(frappe.session.user)
	rows = frappe.get_all(
		wc.CHAT,
		filters={"unread_count": [">", 0]},
		fields=["name", "phone", "party_doctype", "party", "owner_user", "unread_count"],
		limit=1000,
	)
	rows = [r for r in rows if viewer.can_see(r)]
	return {"chats": len(rows), "messages": sum(cint(r.unread_count) for r in rows)}


@mobile_api(methods=("GET",))
def messages(phone: str | None = None, before: str | None = None, limit: str | int | None = None):
	"""A chat's messages, oldest first (40 at a time; `before` loads older ones)."""
	viewer = _viewer()
	t = wc.target(phone, viewer)
	n = min(max(cint(limit) or 40, 1), 100)
	rows = wc.messages_for(t.key, before=before, limit=n + 1)
	more = len(rows) > n
	rows = list(reversed(rows[:n]))
	chat = t.chat or t.probe
	return {
		"chat": wc.chat_payload(chat if t.chat else _blank(t)),
		"messages": [wc.message_payload(m) for m in rows],
		"has_more": more,
	}


def _blank(t):
	p = t.probe
	return frappe._dict(
		phone=t.key,
		title=p.title,
		party_doctype=p.party_doctype,
		party=p.party,
		owner_user=p.owner_user,
		unread_count=0,
	)


@mobile_api(methods=("POST",))
def mark_read(phone: str | None = None):
	viewer = _viewer()
	t = wc.target(phone, viewer)
	wc.mark_read(t.chat)
	return {"ok": True}


def _sent(t, doc) -> dict:
	frappe.db.commit()  # the message is with WhatsApp now; keep the record even if a later step fails
	return {"message": wc.message_payload(frappe.get_doc(wc.MESSAGE, doc.name)), "chat": _chat_now(t)}


def _chat_now(t):
	name = frappe.db.get_value(wc.CHAT, {"phone": t.key}, "name")
	return wc.chat_payload(frappe.get_doc(wc.CHAT, name)) if name else wc.chat_payload(_blank(t))


@mobile_api(methods=("POST",))
def send_text(phone: str | None = None, text: str | None = None, reply_to: str | None = None):
	viewer = _viewer()
	limit_per_user("wa_send", 30, 60)
	t = wc.target(phone, viewer)
	text = require_str(text, "Message", 4096)
	doc = wc.send_text(t, text, optional_str(reply_to, 200))
	return _sent(t, doc)


@mobile_api(methods=("GET",))
def templates(phone: str | None = None):
	viewer = _viewer()
	key = wc.target(phone, viewer).key if phone else None
	return {"items": wc.templates_for(key)}


def _upload():
	f = frappe.request.files.get("file") if frappe.request and frappe.request.files else None
	if not f:
		return None, None, None
	max_bytes = 100 * 1024 * 1024
	content = f.stream.read(max_bytes + 1)
	if not content:
		raise frappe.ValidationError("The file is empty.")
	if len(content) > max_bytes:
		raise frappe.ValidationError("The file is too big.")
	name = (f.filename or "file").replace("/", "_").replace("\\", "_")[-120:]
	return content, name, f.mimetype or f.content_type


@mobile_api(methods=("POST",))
def send_template(phone: str | None = None, template: str | None = None, params: str | list | None = None):
	"""Approved template; `params` = JSON list of the {{1}}, {{2}}... values. A picture for an
	image-header template may come as the multipart `file`."""
	viewer = _viewer()
	limit_per_user("wa_send", 30, 60)
	t = wc.target(phone, viewer)
	template = require_str(template, "Template", 200)
	if isinstance(params, str):
		try:
			params = json.loads(params) if params.strip() else []
		except ValueError:
			raise frappe.ValidationError("params must be a JSON list.")
	if params is not None and not isinstance(params, list):
		raise frappe.ValidationError("params must be a list.")
	header = None
	content, name, mime = _upload()
	if content:
		if wc.media_kind(mime or wc.guess_mime(name, None)) not in ("image", "document"):
			raise frappe.ValidationError("The template picture must be a JPG or PNG image.")
		header = wc.save_upload(content, name)
	doc = wc.send_template(t, template, params or [], header)
	return _sent(t, doc)


@mobile_api(methods=("POST",))
def send_media(phone: str | None = None, caption: str | None = None, reply_to: str | None = None):
	"""Photo / video / voice message / document as the multipart `file`."""
	viewer = _viewer()
	limit_per_user("wa_send", 30, 60)
	t = wc.target(phone, viewer)
	content, name, mime = _upload()
	if not content:
		raise frappe.ValidationError("Choose a file to send.")
	doc = wc.send_media(
		t, content, name, wc.guess_mime(name, mime), optional_str(caption, 1024), optional_str(reply_to, 200)
	)
	return _sent(t, doc)


@frappe.whitelist(methods=["GET"])
def media(message: str | None = None):
	"""The photo / voice / file of a message (raw bytes; same visibility as the chat)."""
	perms.require_active_user()
	viewer = _viewer()
	name = require_str(message, "Message", 140)
	m = frappe.db.get_value(wc.MESSAGE, name, ["name", "type", "from", "to", "attach"], as_dict=True)
	if not m or not m.attach:
		raise frappe.DoesNotExistError("File not found.")
	wc.target("+" + (wc.message_number(m) or "0"), viewer)  # raises when not visible
	content, filename, mime = wc.media_bytes(m)
	frappe.local.response.filename = filename
	frappe.local.response.filecontent = content
	frappe.local.response.type = "download"
	frappe.local.response.display_content_as = "inline"
	frappe.local.response.content_type = mime
