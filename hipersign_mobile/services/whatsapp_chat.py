"""WhatsApp chats in the app (on top of the Frappe WhatsApp app).

Every WhatsApp Message (received through the webhook, sent from Desk, from an automatic rule
or from the app) updates one "Hipersign WhatsApp Chat" row per customer number: last message,
unread count, when the customer last wrote (WhatsApp allows free text only for 24 hours after
that; later only approved templates) and which customer / lead / deal the number belongs to.

Who sees a chat: its owner's team rules - a salesperson sees chats of the customers / leads /
deals she may open in the app (Customer Visibility) and every number not in ERPNext yet;
managers see all. A new message notifies the owner (lead / deal owner, customer account
manager), or everyone who can see the chat when nobody owns it.

Sending:
- text and templates: a WhatsApp Message is inserted and Frappe WhatsApp sends it to Meta;
- photos, voice messages, videos and documents: uploaded to Meta's media store and sent by
  id (works for any file type and never depends on how the web server labels files), then
  recorded as a WhatsApp Message with the file attached.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import mimetypes
import re
from urllib.parse import unquote

import frappe
from frappe.utils import cint, get_datetime, now_datetime

from hipersign_mobile.services import permissions as perms
from hipersign_mobile.services import settings as hm_settings
from hipersign_mobile.services import whatsapp_rules
from hipersign_mobile.services.party import party_summary
from hipersign_mobile.services.phone import normalize_phone

CHAT = "Hipersign WhatsApp Chat"
MESSAGE = "WhatsApp Message"
WINDOW = dt.timedelta(hours=24)
MEDIA_KINDS = ("image", "audio", "video", "document")
PREVIEW = {
	"image": "📷 Photo",
	"audio": "🎤 Voice message",
	"video": "🎬 Video",
	"document": "📄 Document",
	"location": "📍 Location",
	"contacts": "👤 Contact",
	"contact": "👤 Contact",
	"sticker": "Sticker",
	"order": "🛒 Order",
}
# What WhatsApp accepts (anything else is sent as a document).
IMAGE_TYPES = {"image/jpeg", "image/png"}
VIDEO_TYPES = {"video/mp4", "video/3gpp"}
AUDIO_TYPES = {"audio/aac", "audio/mp4", "audio/mpeg", "audio/amr", "audio/ogg", "audio/m4a", "audio/x-m4a"}
LIMIT_MB = {"image": 5, "audio": 16, "video": 16, "document": 100}


class WhatsAppSendError(frappe.ValidationError):
	pass


def enabled() -> bool:
	return whatsapp_rules.available() and perms.whatsapp_chat_on()


def require_enabled():
	if not enabled():
		raise frappe.ValidationError("WhatsApp chats are not set up on this server.")


# ---------------------------------------------------------------- numbers


def chat_key(raw) -> str | None:
	"""Digits of the international number: '918078368600'."""
	e164 = normalize_phone(raw, hm_settings.get().default_country_code)
	return e164[1:] if e164 else None


def number_variants(key: str) -> list[str]:
	"""How the same number may be stored in WhatsApp Message.from / .to."""
	out = [key, "+" + key]
	cc = hm_settings.get().default_country_code
	if key.startswith(cc) and len(key) - len(cc) == 10:
		out.append(key[len(cc) :])
	return out


def message_number(doc) -> str | None:
	raw = doc.get("from") if doc.get("type") == "Incoming" else doc.get("to")
	return chat_key(raw)


# ---------------------------------------------------------------- party and owner


def find_party(key: str) -> tuple[str, str] | None:
	from hipersign_mobile.api.customers import _party_for_phone

	return _party_for_phone("+" + key)


def owner_of(doctype: str | None, name: str | None) -> str | None:
	if not doctype or not name:
		return None
	field = {"CRM Lead": "lead_owner", "CRM Deal": "deal_owner", "Customer": "account_manager"}.get(doctype)
	if not field or not frappe.get_meta(doctype).has_field(field):
		return None
	user = frappe.db.get_value(doctype, name, field)
	if user and frappe.db.get_value("User", user, "enabled"):
		return user
	return None


def _apply_party(chat, key: str):
	found = find_party(key)
	chat.party_doctype, chat.party = found if found else (None, None)
	summary = party_summary(*found) if found else None
	chat.title = (summary or {}).get("name") or chat.get("profile_name") or "+" + key
	chat.owner_user = owner_of(*found) if found else None


# ---------------------------------------------------------------- incoming / outgoing hook


def preview(doc) -> str:
	kind = doc.get("content_type") or "text"
	if doc.get("template"):
		return "📋 " + (whatsapp_rules.template_title(doc.get("template")) or "Template")
	text = _plain(doc.get("message"))
	if kind == "reaction":
		return f"Reacted {text}".strip()
	if kind in PREVIEW:
		return f"{PREVIEW[kind]}{': ' + text if text else ''}"
	return text or kind.title()


def _plain(value) -> str:
	text = re.sub(r"<br\s*/?>|</p>", "\n", str(value or ""), flags=re.I)
	text = re.sub(r"<[^>]+>", "", text)
	return html.unescape(text).strip() if text else ""


def on_message_insert(doc, method=None):
	"""doc_event: WhatsApp Message after_insert. Never breaks receiving or sending."""
	try:
		chat = touch(doc)
		if chat and doc.type == "Incoming" and doc.get("content_type") != "reaction":
			notify(chat, doc)
	except Exception:
		frappe.log_error(title="Hipersign WhatsApp chat: could not update the chat list")


def touch(doc, count_unread: bool = True):
	key = message_number(doc)
	if not key:
		return None
	name = frappe.db.get_value(CHAT, {"phone": key}, "name")
	if name:
		chat = frappe.get_doc(CHAT, name)
	else:
		chat = frappe.new_doc(CHAT)
		chat.phone = key
	when = get_datetime(doc.creation or now_datetime())
	if doc.type == "Incoming" and doc.get("profile_name"):
		chat.profile_name = doc.profile_name
	if doc.get("whatsapp_account"):
		chat.whatsapp_account = doc.whatsapp_account
	if not chat.last_message_at or when >= get_datetime(chat.last_message_at):
		chat.last_message = preview(doc)[:500]
		chat.last_message_at = when
		chat.last_direction = doc.type
	if doc.type == "Incoming":
		if not chat.last_incoming_at or when > get_datetime(chat.last_incoming_at):
			chat.last_incoming_at = when
		if count_unread and doc.get("content_type") != "reaction":
			chat.unread_count = cint(chat.unread_count) + 1
	_apply_party(chat, key)
	chat.flags.ignore_permissions = True
	try:
		chat.save() if not chat.is_new() else chat.insert()
	except (frappe.DuplicateEntryError, frappe.UniqueValidationError):
		# Two messages of a new number at the same moment: the other one created the row.
		chat = frappe.get_doc(CHAT, frappe.db.get_value(CHAT, {"phone": key}, "name"))
		if doc.type == "Incoming" and count_unread:
			frappe.db.sql(f"update `tab{CHAT}` set unread_count = unread_count + 1 where name=%s", chat.name)
	return chat


def backfill_chats():
	"""First install: build the chat list from the messages already received / sent."""
	if not enabled() or not frappe.db.table_exists(CHAT) or frappe.db.count(CHAT):
		return
	try:
		for name in frappe.get_all(MESSAGE, order_by="creation asc", pluck="name", limit=20000):
			touch(frappe.get_doc(MESSAGE, name), count_unread=False)
	except Exception:
		frappe.log_error(title="Hipersign WhatsApp chat: backfill")


# ---------------------------------------------------------------- who sees / who is notified


class Viewer:
	"""Visibility rules for one user, computed once per request."""

	def __init__(self, user: str):
		self.user = user
		self.caps = perms.capabilities(user)
		self.all = "team_view" in self.caps or hm_settings.get().customer_scope != "Own Records"
		self._own: dict[str, set] = {}

	def own(self, doctype: str) -> set:
		if doctype not in self._own:
			self._own[doctype] = perms.own_party_names(doctype, self.user)
		return self._own[doctype]

	def can_see(self, chat) -> bool:
		if "whatsapp" not in self.caps:
			return False
		if self.all or chat.get("owner_user") == self.user:
			return True
		doctype, party = chat.get("party_doctype"), chat.get("party")
		if not doctype or not party:
			return True  # a number that is not in ERPNext yet
		return party in self.own(doctype)


def recipients(chat) -> list[str]:
	if chat.owner_user and "whatsapp" in perms.capabilities(chat.owner_user):
		return [chat.owner_user]
	users = frappe.get_all(
		"Has Role", filters={"role": ["in", perms.MOBILE_ROLES], "parenttype": "User"}, pluck="parent"
	)
	users = frappe.get_all("User", filters={"name": ["in", list(set(users)) or [""]], "enabled": 1}, pluck="name")
	return [u for u in users if Viewer(u).can_see(chat)]


def notify(chat, doc):
	from hipersign_mobile.notifications import queue

	body = preview(doc)
	for user in recipients(chat):
		queue.enqueue(
			user,
			"WhatsApp",
			chat.title or "+" + chat.phone,
			body[:300],
			dedup_key=f"wa:{doc.name}:{user}",
			reference_doctype=CHAT,
			reference_name=chat.name,
		)


# ---------------------------------------------------------------- reading


def window_open(chat) -> bool:
	last = chat.get("last_incoming_at") if chat else None
	return bool(last) and now_datetime() - get_datetime(last) < WINDOW


def window_closes_at(chat):
	last = chat.get("last_incoming_at") if chat else None
	return get_datetime(last) + WINDOW if last else None


def target(phone, viewer: Viewer) -> frappe._dict:
	"""The chat for a number the caller may use (existing or not yet)."""
	key = chat_key(phone)
	if not key:
		raise frappe.ValidationError("Enter a valid WhatsApp number.")
	name = frappe.db.get_value(CHAT, {"phone": key}, "name")
	chat = frappe.get_doc(CHAT, name) if name else None
	probe = chat
	if not probe:
		probe = frappe._dict(phone=key)
		_apply_party(probe, key)
	if not viewer.can_see(probe):
		raise frappe.DoesNotExistError("Chat not found.")
	return frappe._dict(key=key, chat=chat, probe=probe)


def chat_payload(chat, viewer: Viewer | None = None) -> dict:
	party = None
	if chat.get("party_doctype") and chat.get("party"):
		s = party_summary(chat.party_doctype, chat.party)
		if s:
			party = {"kind": s["kind"], "doctype": s["doctype"], "id": s["id"], "name": s["name"]}
	closes = window_closes_at(chat)
	return {
		"phone": "+" + chat.phone,
		"key": chat.phone,
		"title": chat.get("title") or chat.get("profile_name") or "+" + chat.phone,
		"profile_name": chat.get("profile_name"),
		"party": party,
		"owner": chat.get("owner_user"),
		"owner_name": frappe.utils.get_fullname(chat.owner_user) if chat.get("owner_user") else None,
		"last_message": chat.get("last_message"),
		"last_message_at": _iso(chat.get("last_message_at")),
		"last_direction": chat.get("last_direction"),
		"unread": cint(chat.get("unread_count")),
		"window_open": window_open(chat),
		"window_closes_at": _iso(closes) if closes and window_open(chat) else None,
	}


def _iso(value):
	from hipersign_mobile.services import timeutil

	return timeutil.to_iso(value) if value else None


MESSAGE_FIELDS = [
	"name",
	"type",
	"from",
	"to",
	"message",
	"content_type",
	"message_type",
	"template",
	"template_parameters",
	"body_param",
	"attach",
	"status",
	"message_id",
	"reply_to_message_id",
	"is_reply",
	"creation",
	"owner",
	"profile_name",
]


def messages_for(key: str, before=None, limit: int = 40) -> list:
	variants = number_variants(key)
	filters = []
	if before:
		filters.append(["creation", "<", get_datetime(before)])
	return frappe.get_all(
		MESSAGE,
		filters=filters,
		or_filters=[["from", "in", variants], ["to", "in", variants]],
		fields=[f"`{f}`" if f == "from" else f for f in MESSAGE_FIELDS],
		order_by="creation desc",
		limit=limit,
	)


def render_template(template: str | None, params) -> str | None:
	if not template:
		return None
	body = frappe.db.get_value("WhatsApp Templates", template, "template")
	if not body:
		return None
	values = []
	if isinstance(params, str) and params.strip():
		try:
			params = json.loads(params)
		except ValueError:
			params = None
	if isinstance(params, dict):
		values = [str(v) for _k, v in sorted(params.items(), key=lambda kv: cint(kv[0]))]
	elif isinstance(params, list):
		values = [str(v) for v in params]
	for i, v in enumerate(values):
		body = body.replace("{{%d}}" % (i + 1), v)
	return body


def file_ext(file_url: str | None) -> str | None:
	if not file_url:
		return None
	tail = file_url.split("?")[0].rsplit("/", 1)[-1]
	if "." not in tail:
		return None
	return tail.rsplit(".", 1)[-1].split(";")[0].strip().lower()[:8] or None


def message_payload(m) -> dict:
	kind = m.content_type or "text"
	is_template = bool(m.template)
	text = _plain(m.message)
	media = None
	if m.attach and (kind in MEDIA_KINDS or is_template):
		ext = file_ext(m.attach)
		media_kind = kind if kind in MEDIA_KINDS else ("image" if ext in ("jpg", "jpeg", "png", "webp") else "document")
		name = m.attach.split("?")[0].rsplit("/", 1)[-1]
		media = {"kind": media_kind, "ext": ext, "name": unquote(name)[:120]}
	status = (m.status or "").strip().lower()
	if status == "success":
		status = "sent"
	return {
		"id": m.name,
		"direction": "in" if m.type == "Incoming" else "out",
		"kind": "template" if is_template else kind,
		"text": text or None,
		"template": whatsapp_rules.template_title(m.template) if is_template else None,
		"template_text": render_template(m.template, m.body_param or m.template_parameters) if is_template else None,
		"media": media,
		"status": status or None,
		"at": _iso(m.creation),
		"wa_id": m.message_id,
		"reply_to": m.reply_to_message_id if cint(m.is_reply) or kind == "reaction" else None,
		"by": frappe.utils.get_fullname(m.owner) if m.type == "Outgoing" and m.owner not in (None, "Guest") else None,
	}


def mark_read(chat):
	if not chat or not cint(chat.unread_count):
		return
	frappe.db.set_value(CHAT, chat.name, "unread_count", 0, update_modified=False)
	last = frappe.get_all(
		MESSAGE,
		filters={"type": "Incoming", "from": ["in", number_variants(chat.phone)], "message_id": ["is", "set"]},
		fields=["name"],
		order_by="creation desc",
		limit=1,
	)
	if last:
		frappe.enqueue(
			"hipersign_mobile.services.whatsapp_chat.send_read_receipt",
			queue="short",
			enqueue_after_commit=True,
			message=last[0].name,
		)


def send_read_receipt(message: str):
	"""Blue ticks on the customer's phone (best effort)."""
	try:
		doc = frappe.get_doc(MESSAGE, message)
		acc = _account(doc.whatsapp_account)
		if acc:
			_meta_post(acc, "messages", {"messaging_product": "whatsapp", "status": "read", "message_id": doc.message_id})
	except Exception:
		pass


# ---------------------------------------------------------------- templates


def default_header(template_name: str, template_sample: str | None) -> str | None:
	"""The picture an automatic rule uses for this template, else the template's sample."""
	rows = frappe.get_all(
		"Hipersign WhatsApp Rule",
		filters={"whatsapp_template": template_name, "header_image": ["is", "set"]},
		pluck="header_image",
		limit=1,
	) if frappe.get_meta("Hipersign WhatsApp Rule").has_field("whatsapp_template") else []
	return (rows[0] if rows else None) or template_sample or None


def templates_for(key: str | None) -> list[dict]:
	ctx = {}
	if key:
		found = find_party(key)
		if found:
			ctx = whatsapp_rules.context(found[0], found[1], "+" + key, frappe.session.user, None)
	out = []
	for t in frappe.get_all(
		"WhatsApp Templates",
		filters={"status": "APPROVED"},
		fields=["name", "template_name", "language_code", "category", "template", "header_type", "header", "sample", "sample_values"],
		order_by="template_name asc",
		limit=200,
	):
		count = whatsapp_rules.template_param_count(t)
		header_type = (t.header_type or "").upper()
		needs_file = header_type in ("IMAGE", "DOCUMENT")
		default = default_header(t.name, t.sample) if needs_file else None
		samples = [s.strip() for s in (t.sample_values or "").split(",")] if count else []
		suggested = [ctx.get("name") or ""] + [""] * (count - 1) if count else []
		out.append(
			{
				"id": t.name,
				"title": whatsapp_rules.template_title(t.name),
				"language": t.language_code,
				"category": (t.category or "").title() or None,
				"body": t.template or "",
				"header_type": header_type or None,
				"header_text": t.header if header_type == "TEXT" else None,
				"params": count,
				"samples": samples,
				"suggested": suggested,
				"has_default_header": bool(default),
			}
		)
	return out


# ---------------------------------------------------------------- sending


def _account(name: str | None):
	from frappe_whatsapp.utils import get_whatsapp_account

	if name and frappe.db.exists("WhatsApp Account", name):
		return frappe.get_doc("WhatsApp Account", name)
	return get_whatsapp_account(account_type="outgoing") or get_whatsapp_account(account_type="incoming")


def _base(acc) -> str:
	return f"{acc.url}/{acc.version}/{acc.phone_id}"


def _meta_error(r) -> str:
	try:
		err = r.json().get("error", {})
		return err.get("error_user_msg") or err.get("message") or f"WhatsApp refused it ({r.status_code})."
	except Exception:
		return f"WhatsApp refused it ({r.status_code})."


def _meta_post(acc, path: str, payload: dict) -> dict:
	import requests

	r = requests.post(
		f"{_base(acc)}/{path}",
		headers={"Authorization": f"Bearer {acc.get_password('token')}", "Content-Type": "application/json"},
		data=json.dumps(payload),
		timeout=30,
	)
	if r.status_code >= 400:
		raise WhatsAppSendError(_meta_error(r))
	return r.json()


def _meta_upload(acc, content: bytes, filename: str, mime: str) -> str:
	import requests

	r = requests.post(
		f"{_base(acc)}/media",
		headers={"Authorization": f"Bearer {acc.get_password('token')}"},
		files={"file": (filename, content, mime)},
		data={"messaging_product": "whatsapp", "type": mime},
		timeout=120,
	)
	if r.status_code >= 400:
		raise WhatsAppSendError(_meta_error(r))
	media_id = (r.json() or {}).get("id")
	if not media_id:
		raise WhatsAppSendError("WhatsApp did not accept the file.")
	return media_id


def _link(t) -> dict:
	p = t.probe
	return {"reference_doctype": p.party_doctype, "reference_name": p.party} if p.get("party") else {}


def require_window(t):
	if not window_open(t.chat):
		raise frappe.ValidationError(
			"The customer has not written in the last 24 hours, so WhatsApp only allows an approved template now."
		)


def send_text(t, text: str, reply_to: str | None = None):
	require_window(t)
	doc = frappe.get_doc(
		{
			"doctype": MESSAGE,
			"type": "Outgoing",
			"to": t.key,
			"message": text,
			"content_type": "text",
			"message_type": "Manual",
			"whatsapp_account": (t.chat.whatsapp_account if t.chat else None) or None,
			"is_reply": 1 if reply_to else 0,
			"reply_to_message_id": reply_to or None,
			**_link(t),
		}
	)
	doc.insert(ignore_permissions=True)
	return doc


def send_template(t, template: str, params: list[str], header_file: str | None = None):
	tpl = frappe.db.get_value(
		"WhatsApp Templates", template, ["name", "status", "header_type", "sample", "sample_values"], as_dict=True
	)
	if not tpl or (tpl.status or "").upper() != "APPROVED":
		raise frappe.ValidationError("Pick an approved template.")
	count = whatsapp_rules.template_param_count(tpl)
	params = [str(p or "").strip() for p in (params or [])]
	if count and (len(params) != count or not all(params)):
		raise frappe.ValidationError(f"Fill in all {count} values of the template.")
	msg = {
		"doctype": MESSAGE,
		"type": "Outgoing",
		"to": t.key,
		"use_template": 1,
		"message_type": "Template",
		"template": tpl.name,
		"content_type": "text",
		"whatsapp_account": (t.chat.whatsapp_account if t.chat else None) or None,
		**_link(t),
	}
	if count:
		msg["body_param"] = json.dumps({str(i + 1): v for i, v in enumerate(params)})
	if (tpl.header_type or "").upper() in ("IMAGE", "DOCUMENT"):
		header = header_file or default_header(tpl.name, tpl.sample)
		if not header:
			raise frappe.ValidationError("This template needs a picture. Choose one to send with it.")
		msg["attach"] = whatsapp_rules.public_url(header)
	doc = frappe.get_doc(msg)
	doc.insert(ignore_permissions=True)
	return doc


def media_kind(mime: str) -> str:
	mime = (mime or "").split(";")[0].strip().lower()
	if mime in IMAGE_TYPES:
		return "image"
	if mime in VIDEO_TYPES:
		return "video"
	if mime in AUDIO_TYPES or mime.startswith("audio/"):
		return "audio"
	return "document"


def guess_mime(filename: str, given: str | None) -> str:
	given = (given or "").split(";")[0].strip().lower()
	if given and given != "application/octet-stream":
		return given
	ext = (filename.rsplit(".", 1)[-1] if "." in filename else "").lower()
	return {
		"m4a": "audio/mp4",
		"aac": "audio/aac",
		"ogg": "audio/ogg",
		"opus": "audio/ogg",
		"amr": "audio/amr",
		"mp3": "audio/mpeg",
	}.get(ext) or mimetypes.guess_type(filename)[0] or "application/octet-stream"


def send_media(t, content: bytes, filename: str, mime: str, caption: str | None = None, reply_to: str | None = None):
	require_window(t)
	kind = media_kind(mime)
	if kind == "audio" and mime in ("audio/m4a", "audio/x-m4a"):
		mime = "audio/mp4"
	limit_mb = min(LIMIT_MB[kind], max(hm_settings.get().whatsapp_max_media_mb, 1) if kind == "document" else LIMIT_MB[kind])
	if len(content) > limit_mb * 1024 * 1024:
		raise frappe.ValidationError(f"The file is too big for WhatsApp ({limit_mb} MB at most).")
	acc = _account(t.chat.whatsapp_account if t.chat else None)
	if not acc:
		raise frappe.ValidationError("No WhatsApp Account is set up on the server.")
	media_id = _meta_upload(acc, content, filename, mime)
	body = {"id": media_id}
	if caption and kind != "audio":
		body["caption"] = caption
	if kind == "document":
		body["filename"] = filename
	payload = {"messaging_product": "whatsapp", "to": t.key, "type": kind, kind: body}
	if reply_to:
		payload["context"] = {"message_id": reply_to}
	res = _meta_post(acc, "messages", payload)
	wa_id = ((res.get("messages") or [{}])[0]).get("id")

	# Record it. message_type "Template" + message_id tells Frappe WhatsApp it is already sent.
	doc = frappe.get_doc(
		{
			"doctype": MESSAGE,
			"type": "Outgoing",
			"to": t.key,
			"message": caption or "",
			"content_type": kind,
			"message_type": "Template",
			"message_id": wa_id,
			"status": "Success",
			"whatsapp_account": acc.name,
			"is_reply": 1 if reply_to else 0,
			"reply_to_message_id": reply_to or None,
			**_link(t),
		}
	)
	doc.insert(ignore_permissions=True)
	frappe.db.set_value(MESSAGE, doc.name, "message_type", "Manual", update_modified=False)
	f = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": filename,
			"content": content,
			"is_private": 1,
			"attached_to_doctype": MESSAGE,
			"attached_to_name": doc.name,
			"attached_to_field": "attach",
		}
	)
	f.flags.ignore_permissions = True
	f.save()
	frappe.db.set_value(MESSAGE, doc.name, "attach", f.file_url, update_modified=False)
	doc.attach, doc.message_type = f.file_url, "Manual"
	return doc


def save_upload(content: bytes, filename: str) -> str:
	"""A template's header picture chosen in the app (Meta downloads it, so it is public)."""
	f = frappe.get_doc({"doctype": "File", "file_name": filename, "content": content, "is_private": 0})
	f.flags.ignore_permissions = True
	f.save()
	return f.file_url


# ---------------------------------------------------------------- media download


def media_bytes(m) -> tuple[bytes, str, str]:
	"""(content, filename, mime) of a message's file."""
	url = m.attach
	ext = file_ext(url) or "bin"
	name = unquote(url.split("?")[0].rsplit("/", 1)[-1]).split(";")[0] or f"file.{ext}"
	mime = guess_mime(f"x.{ext}", None)
	site = frappe.utils.get_url()
	if url.startswith(site):
		url = url[len(site) :]
	if url.startswith("http"):
		import requests

		r = requests.get(url, timeout=20)
		if r.status_code != 200:
			raise frappe.DoesNotExistError("The file is no longer available.")
		return r.content, name, (r.headers.get("content-type") or mime).split(";")[0]
	file_name = frappe.db.get_value("File", {"file_url": url}, "name") or frappe.db.get_value(
		"File", {"file_url": unquote(url)}, "name"
	)
	if not file_name:
		raise frappe.DoesNotExistError("The file is no longer available.")
	f = frappe.get_doc("File", file_name)
	with open(f.get_full_path(), "rb") as fh:  # bytes, even when the content happens to be text
		return fh.read(), name, mime
