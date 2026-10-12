"""Call recordings made from the app, linked to a Customer / CRM Lead / CRM Deal.

Android does not let ordinary apps record calls, so the phone's own call recorder
does the recording. The app remembers which customer each call was for, finds the
matching recording file afterwards and uploads it here.
"""

from __future__ import annotations

import os
import re

import frappe
from frappe.utils import cint

from hipersign_mobile.api._common import (
	choice,
	limit_per_user,
	mobile_api,
	optional_str,
	page_args,
	page_result,
	require_str,
)
from hipersign_mobile.services import permissions as perms
from hipersign_mobile.services import settings as hm_settings
from hipersign_mobile.services import timeutil
from hipersign_mobile.services.phone import normalize_phone

AUDIO_TYPES = {
	"m4a": "audio/mp4",
	"mp4": "audio/mp4",
	"aac": "audio/aac",
	"mp3": "audio/mpeg",
	"amr": "audio/amr",
	"awb": "audio/amr-wb",
	"3gp": "audio/3gpp",
	"3gpp": "audio/3gpp",
	"wav": "audio/wav",
	"ogg": "audio/ogg",
	"opus": "audio/ogg",
	"flac": "audio/flac",
	"webm": "audio/webm",
}

FIELDS = [
	"name",
	"party_doctype",
	"party",
	"party_name",
	"phone",
	"call_started_at",
	"duration_seconds",
	"recorded_by",
	"activity",
	"file_size",
	"audio_file",
]


def _sees_all(user: str) -> bool:
	from hipersign_mobile.permissions import _sees_all_recordings

	return _sees_all_recordings(user)


def _serialize(row) -> dict:
	return {
		"id": row.name,
		"party_doctype": row.party_doctype,
		"party": row.party,
		"party_name": row.party_name or row.party,
		"phone": row.phone,
		"started_at": timeutil.to_iso(row.call_started_at),
		"duration_seconds": cint(row.duration_seconds),
		"recorded_by": row.recorded_by,
		"recorded_by_name": frappe.utils.get_fullname(row.recorded_by),
		"activity": str(row.activity) if row.activity else None,
		"size": cint(row.file_size),
		"has_audio": bool(row.audio_file),
		"ext": (os.path.splitext(row.audio_file or "")[1].lstrip(".") or None),
	}


def _require_enabled(me: str):
	perms.require("customers", me)
	if not hm_settings.get().enable_call_recordings:
		raise frappe.ValidationError("Call recordings are turned off on this server.")


def _get_for(name: str, me: str):
	if not name or not frappe.db.exists("Call Recording", name):
		raise frappe.DoesNotExistError("Recording not found.")
	doc = frappe.get_doc("Call Recording", name)
	if not (_sees_all(me) or doc.recorded_by == me):
		raise frappe.DoesNotExistError("Recording not found.")
	return doc


@mobile_api(methods=("POST",))
def upload(
	party_doctype: str | None = None,
	party: str | None = None,
	phone: str | None = None,
	started_at: str | None = None,
	duration: str | int | None = None,
	activity: str | int | None = None,
	client_key: str | None = None,
	source_name: str | None = None,
):
	"""Multipart upload: the audio is the `file` part. Safe to retry (client_key)."""
	me = frappe.session.user
	_require_enabled(me)
	limit_per_user("recording_upload", 30, 60)
	client_key = require_str(client_key, "client_key", 80)

	existing = frappe.db.get_value("Call Recording", {"client_key": client_key}, ["name", "recorded_by"], as_dict=True)
	if existing:
		if existing.recorded_by != me:
			raise frappe.ValidationError("This recording key is already used.")
		return _serialize(frappe.get_doc("Call Recording", existing.name))

	party_doctype = choice(party_doctype, perms.PARTY_DOCTYPES, "party_doctype")
	party = require_str(party, "party")
	perms.require_party(party_doctype, party, me)
	task = perms.get_task_for(activity, me) if activity not in (None, "", "null") else None

	upload_file = frappe.request.files.get("file") if frappe.request and frappe.request.files else None
	if not upload_file:
		raise frappe.ValidationError("No recording file was sent.")
	original = os.path.basename(upload_file.filename or "recording")
	ext = os.path.splitext(original)[1].lstrip(".").lower()
	if ext not in AUDIO_TYPES:
		raise frappe.ValidationError(f"Unsupported audio type: .{ext or '?'}")
	max_bytes = hm_settings.get().max_recording_mb * 1024 * 1024
	content = upload_file.stream.read(max_bytes + 1)
	if not content:
		raise frappe.ValidationError("The recording file is empty.")
	if len(content) > max_bytes:
		raise frappe.ValidationError(f"Recording is larger than {hm_settings.get().max_recording_mb} MB.")

	started = timeutil.parse_client_datetime(started_at, me)
	cc = hm_settings.get().default_country_code
	normalized = normalize_phone(phone, cc) if phone else None
	doc = frappe.get_doc(
		{
			"doctype": "Call Recording",
			"party_doctype": party_doctype,
			"party": party,
			"phone": normalized or optional_str(phone, 40),
			"call_started_at": started,
			"duration_seconds": max(cint(duration), 0),
			"recorded_by": me,
			"activity": task.name if task else None,
			"file_size": len(content),
			"source_name": optional_str(original, 140),
			"client_key": client_key,
		}
	).insert(ignore_permissions=True)

	safe_party = re.sub(r"[^A-Za-z0-9]+", "-", doc.party_name or party).strip("-")[:40] or "call"
	file_doc = frappe.get_doc(
		{
			"doctype": "File",
			"file_name": f"{doc.name}-{safe_party}.{ext}",
			"attached_to_doctype": "Call Recording",
			"attached_to_name": doc.name,
			"attached_to_field": "audio_file",
			"is_private": 1,
			"content": content,
		}
	).insert(ignore_permissions=True)
	doc.db_set("audio_file", file_doc.file_url, update_modified=False)

	minutes, seconds = divmod(cint(duration), 60)
	frappe.get_doc(
		{
			"doctype": "Comment",
			"comment_type": "Info",
			"reference_doctype": party_doctype,
			"reference_name": party,
			"content": f'Call recording <a href="/app/call-recording/{doc.name}">{doc.name}</a> '
			f"({minutes}:{seconds:02d}) by {frappe.utils.get_fullname(me)}",
		}
	).insert(ignore_permissions=True)
	return _serialize(doc)


@mobile_api(methods=("GET",))
def get_list(
	party_doctype: str | None = None,
	party: str | None = None,
	cursor: str | int | None = None,
	page_size: str | int | None = None,
):
	"""Newest first. Salespeople get their own recordings; managers get everyone's."""
	me = frappe.session.user
	_require_enabled(me)
	offset, limit = page_args(cursor, page_size)
	filters = {}
	if party:
		party_doctype = choice(party_doctype, perms.PARTY_DOCTYPES, "party_doctype")
		perms.require_party(party_doctype, party, me)
		filters.update({"party_doctype": party_doctype, "party": party})
	if not _sees_all(me):
		filters["recorded_by"] = me
	rows = frappe.get_all(
		"Call Recording",
		filters=filters,
		fields=FIELDS,
		order_by="call_started_at desc",
		start=offset,
		page_length=limit + 1,
		ignore_permissions=True,
	)
	return page_result([_serialize(r) for r in rows[:limit]], offset, limit, len(rows) > limit)


@mobile_api(methods=("POST",))
def delete(name: str | None = None):
	"""Owner (or a manager) can delete a recording, e.g. one matched to the wrong call."""
	me = frappe.session.user
	_require_enabled(me)
	doc = _get_for(name, me)
	frappe.delete_doc("Call Recording", doc.name, ignore_permissions=True)
	return {"deleted": doc.name}


@frappe.whitelist(methods=["GET"])
def download(name: str | None = None):
	"""Raw audio bytes for the in-app player (same access rules as get_list)."""
	me = frappe.session.user
	perms.require_active_user(me)
	perms.require("customers", me)
	doc = _get_for(name, me)
	if not doc.audio_file:
		raise frappe.DoesNotExistError("Recording file not found.")
	file_doc = frappe.get_doc("File", {"file_url": doc.audio_file, "attached_to_name": doc.name})
	ext = os.path.splitext(doc.audio_file)[1].lstrip(".").lower()
	frappe.local.response.filename = f"{doc.name}.{ext}"
	with open(file_doc.get_full_path(), "rb") as fh:
		frappe.local.response.filecontent = fh.read()
	frappe.local.response.type = "download"
	frappe.local.response.display_content_as = "inline"
	frappe.local.response.content_type = AUDIO_TYPES.get(ext, "application/octet-stream")
