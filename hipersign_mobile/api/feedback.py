"""App bugs & improvement ideas: everyone can post and vote; the team replies and sets the status."""

from __future__ import annotations

import json

import frappe

from hipersign_mobile.api._common import choice, limit_per_user, mobile_api, optional_str, page_args, page_result, require_str
from hipersign_mobile.services import permissions as perms
from hipersign_mobile.services import timeutil

DOCTYPE = "Hipersign Feedback"
KINDS = ("Bug", "Improvement", "Idea")
STATUSES = ("New", "Planned", "In Progress", "Done", "Not Planned")
OPEN = ("New", "Planned", "In Progress")


def _can_manage(user: str) -> bool:
	caps = perms.capabilities(user)
	return "team_view" in caps or "development" in caps


def _voters(doc) -> list:
	try:
		return list(json.loads(doc.get("voters") or "[]"))
	except ValueError:
		return []


def serialize(doc, me: str, with_files: bool = False) -> dict:
	from hipersign_mobile.api.attachments import files_of

	voters = _voters(doc)
	out = {
		"id": doc.name,
		"kind": doc.kind,
		"title": doc.title,
		"details": doc.details,
		"status": doc.status,
		"reply": doc.reply,
		"votes": len(voters),
		"voted": me in voters,
		"by": doc.posted_by_name or frappe.utils.get_fullname(doc.posted_by),
		"is_mine": doc.posted_by == me,
		"at": timeutil.to_iso(doc.creation),
		"app_version": doc.app_version,
		"can_manage": _can_manage(me),
	}
	if with_files:
		out["files"] = files_of(DOCTYPE, doc.name)
	return out


@mobile_api(methods=("GET",))
def list_feedback(filter: str | None = None, sort: str | None = None, cursor=None, page_size=None):
	me = frappe.session.user
	perms.require("feedback", me)
	f = choice(filter, ("open", "done", "mine", "all"), "filter", default="open")
	order = "votes desc, creation desc" if choice(sort, ("top", "new"), "sort", default="top") == "top" else "creation desc"
	filters = []
	if f == "open":
		filters.append(["status", "in", OPEN])
	elif f == "done":
		filters.append(["status", "in", ("Done", "Not Planned")])
	elif f == "mine":
		filters.append(["posted_by", "=", me])
	offset, limit = page_args(cursor, page_size)
	names = frappe.get_all(DOCTYPE, filters=filters, pluck="name", order_by=order, start=offset, limit=limit + 1)
	items = [serialize(frappe.get_doc(DOCTYPE, n), me) for n in names[:limit]]
	return page_result(items, offset, limit, len(names) > limit)


@mobile_api(methods=("GET",))
def get_feedback(feedback_id: str | None = None):
	me = frappe.session.user
	perms.require("feedback", me)
	name = require_str(feedback_id, "Suggestion", 40)
	if not frappe.db.exists(DOCTYPE, name):
		raise frappe.DoesNotExistError("Not found.")
	return serialize(frappe.get_doc(DOCTYPE, name), me, with_files=True)


@mobile_api(methods=("POST",))
def create_feedback(kind: str | None = None, title: str | None = None, details: str | None = None, app_version: str | None = None):
	me = frappe.session.user
	perms.require("feedback", me)
	limit_per_user("feedback", 20, 3600)
	doc = frappe.get_doc(
		{
			"doctype": DOCTYPE,
			"kind": choice(kind, KINDS, "kind", default="Improvement"),
			"title": require_str(title, "Title", 200),
			"details": optional_str(details, 10000),
			"status": "New",
			"posted_by": me,
			"posted_by_name": frappe.utils.get_fullname(me),
			"app_version": optional_str(app_version, 40),
			"votes": 1,
			"voters": json.dumps([me]),
		}
	).insert(ignore_permissions=True)
	_tell_team(doc, me)
	return serialize(doc, me, with_files=True)


def _tell_team(doc, me):
	from hipersign_mobile.notifications import queue

	team = set(
		frappe.get_all(
			"Has Role",
			filters={"role": ["in", (perms.ROLE_DEVELOPER, perms.ROLE_MANAGER, perms.ROLE_CEO)], "parenttype": "User"},
			pluck="parent",
		)
	) - {me}
	for user in team:
		try:
			queue.enqueue(
				user, "Feedback", f"App {doc.kind.lower()}: {doc.title}"[:140],
				f"From {doc.posted_by_name}" + (f" - {doc.details[:200]}" if doc.details else ""),
				dedup_key=f"fb:{doc.name}:new:{user}", reference_doctype=DOCTYPE, reference_name=doc.name,
			)
		except Exception:
			frappe.log_error(title="Hipersign feedback alert")


@mobile_api(methods=("POST",))
def vote(feedback_id: str | None = None):
	"""Toggle my vote ("I want this too")."""
	me = frappe.session.user
	perms.require("feedback", me)
	name = require_str(feedback_id, "Suggestion", 40)
	frappe.db.sql(f"select name from `tab{DOCTYPE}` where name=%s for update", name)
	doc = frappe.get_doc(DOCTYPE, name)
	voters = _voters(doc)
	if me in voters:
		voters.remove(me)
	else:
		voters.append(me)
	doc.db_set({"voters": json.dumps(voters), "votes": len(voters)}, update_modified=False)
	return serialize(doc, me)


@mobile_api(methods=("POST",))
def update_feedback(
	feedback_id: str | None = None, status: str | None = None, reply: str | None = None,
	title: str | None = None, details: str | None = None,
):
	"""The team sets the status / replies; the poster can still fix their own text."""
	me = frappe.session.user
	perms.require("feedback", me)
	name = require_str(feedback_id, "Suggestion", 40)
	if not frappe.db.exists(DOCTYPE, name):
		raise frappe.DoesNotExistError("Not found.")
	doc = frappe.get_doc(DOCTYPE, name)
	manage = _can_manage(me)
	if (status is not None or reply is not None) and not manage:
		raise frappe.PermissionError("Only the team can change the status or reply.")
	if (title is not None or details is not None) and not (manage or doc.posted_by == me):
		raise frappe.PermissionError("You can only change your own suggestions.")
	before = (doc.status, doc.reply)
	if status is not None:
		doc.status = choice(status, STATUSES, "status")
	if reply is not None:
		doc.reply = optional_str(reply, 4000)
	if title is not None:
		doc.title = require_str(title, "Title", 200)
	if details is not None:
		doc.details = optional_str(details, 10000)
	doc.save(ignore_permissions=True)
	if (doc.status, doc.reply) != before and doc.posted_by and doc.posted_by != me:
		from hipersign_mobile.notifications import queue

		queue.enqueue(
			doc.posted_by, "Feedback", f"Your suggestion: {doc.status}"[:140],
			f"{doc.title}" + (f" - {doc.reply[:200]}" if doc.reply else ""),
			dedup_key=f"fb:{doc.name}:{doc.status}:{hash(doc.reply or '')}"[:140],
			reference_doctype=DOCTYPE, reference_name=doc.name,
		)
	return serialize(doc, me, with_files=True)
