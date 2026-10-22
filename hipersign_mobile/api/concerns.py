"""Problems employees face at work (a manager, a colleague, workload...), raised privately.

The person chooses who can read it: "CEO Only" (users with the Hipersign CEO role) or
"HR & Managers" (Hipersign HR, Hipersign Manager and the CEO). Nobody else - not even
colleagues with access to ERPNext - can see it; the person who raised it sees their own.
"""

from __future__ import annotations

import frappe

from hipersign_mobile.api._common import choice, limit_per_user, mobile_api, optional_str, page_args, page_result, require_str
from hipersign_mobile.permissions import concern_audiences
from hipersign_mobile.services import permissions as perms
from hipersign_mobile.services import timeutil

DOCTYPE = "Hipersign Concern"
AUDIENCES = ("CEO Only", "HR & Managers")
CATEGORIES = (
	"Manager", "Colleague", "Workload", "Salary / Payments", "Work Environment", "Tools / Equipment", "Customer", "Other",
)
STATUSES = ("Open", "In Review", "Resolved", "Closed")


def _readers(audience: str) -> set[str]:
	roles = (perms.ROLE_CEO,) if audience == "CEO Only" else (perms.ROLE_CEO, perms.ROLE_HR, perms.ROLE_MANAGER)
	users = set(frappe.get_all("Has Role", filters={"role": ["in", roles], "parenttype": "User"}, pluck="parent"))
	enabled = set(frappe.get_all("User", filters={"name": ["in", list(users) or [""]], "enabled": 1}, pluck="name"))
	return enabled - {"Administrator"}


def _get(name: str, me: str):
	name = require_str(name, "Concern", 40)
	if not frappe.db.exists(DOCTYPE, name):
		raise frappe.DoesNotExistError("Not found.")
	doc = frappe.get_doc(DOCTYPE, name)
	if doc.raised_by != me and doc.audience not in concern_audiences(me):
		raise frappe.DoesNotExistError("Not found.")
	return doc


def serialize(doc, me: str, with_files: bool = False) -> dict:
	from hipersign_mobile.api.attachments import files_of

	out = {
		"id": doc.name,
		"audience": doc.audience,
		"category": doc.category,
		"subject": doc.subject,
		"details": doc.details,
		"status": doc.status,
		"response": doc.response,
		"responded_by": frappe.utils.get_fullname(doc.responded_by) if doc.responded_by else None,
		"by": doc.raised_by_name or frappe.utils.get_fullname(doc.raised_by),
		"is_mine": doc.raised_by == me,
		"can_respond": doc.audience in concern_audiences(me) and doc.raised_by != me,
		"at": timeutil.to_iso(doc.creation),
		"modified": timeutil.to_iso(doc.modified),
	}
	if with_files:
		out["files"] = files_of(DOCTYPE, doc.name)
	return out


@mobile_api(methods=("GET",))
def options():
	me = frappe.session.user
	perms.require("concerns", me)
	return {
		"audiences": list(AUDIENCES),
		"categories": list(CATEGORIES),
		"can_review": bool(concern_audiences(me)),
		"readers": {a: len(_readers(a)) for a in AUDIENCES},
	}


@mobile_api(methods=("GET",))
def list_concerns(scope: str | None = None, cursor=None, page_size=None):
	"""mine = raised by me; inbox = the ones I may read (CEO / HR / managers)."""
	me = frappe.session.user
	perms.require("concerns", me)
	scope = choice(scope, ("mine", "inbox"), "scope", default="mine")
	offset, limit = page_args(cursor, page_size)
	if scope == "mine":
		filters = [["raised_by", "=", me]]
	else:
		aud = concern_audiences(me)
		if not aud:
			raise frappe.PermissionError("You are not allowed to see others' concerns.")
		filters = [["audience", "in", aud], ["raised_by", "!=", me]]
	names = frappe.get_all(DOCTYPE, filters=filters, pluck="name", order_by="creation desc", start=offset, limit=limit + 1)
	items = [serialize(frappe.get_doc(DOCTYPE, n), me) for n in names[:limit]]
	return page_result(items, offset, limit, len(names) > limit)


@mobile_api(methods=("GET",))
def get_concern(concern_id: str | None = None):
	me = frappe.session.user
	perms.require("concerns", me)
	return serialize(_get(concern_id, me), me, with_files=True)


@mobile_api(methods=("POST",))
def create_concern(
	audience: str | None = None, category: str | None = None, subject: str | None = None, details: str | None = None
):
	me = frappe.session.user
	perms.require("concerns", me)
	limit_per_user("concern", 10, 3600)
	doc = frappe.get_doc(
		{
			"doctype": DOCTYPE,
			"audience": choice(audience, AUDIENCES, "audience", default="HR & Managers"),
			"category": choice(category, CATEGORIES, "category", default="Other"),
			"subject": require_str(subject, "Subject", 200),
			"details": optional_str(details, 20000),
			"status": "Open",
			"raised_by": me,
			"raised_by_name": frappe.utils.get_fullname(me),
		}
	).insert(ignore_permissions=True)
	from hipersign_mobile.notifications import queue

	for user in _readers(doc.audience) - {me}:
		try:
			queue.enqueue(
				user, "Concern", f"New concern ({doc.category})"[:140],
				f"{doc.subject} - from {doc.raised_by_name}",
				dedup_key=f"con:{doc.name}:new:{user}", reference_doctype=DOCTYPE, reference_name=doc.name,
			)
		except Exception:
			frappe.log_error(title="Hipersign concern alert")
	return serialize(doc, me, with_files=True)


@mobile_api(methods=("POST",))
def respond(concern_id: str | None = None, response: str | None = None, status: str | None = None):
	"""CEO / HR / managers (whoever may read it) answer and set the status."""
	me = frappe.session.user
	perms.require("concerns", me)
	doc = _get(concern_id, me)
	if doc.audience not in concern_audiences(me) or doc.raised_by == me:
		raise frappe.PermissionError("You can't respond to this.")
	if response is not None:
		doc.response = optional_str(response, 20000)
		doc.responded_by = me
	if status is not None:
		doc.status = choice(status, STATUSES, "status")
	elif response and doc.status == "Open":
		doc.status = "In Review"
	doc.save(ignore_permissions=True)
	from hipersign_mobile.notifications import queue

	queue.enqueue(
		doc.raised_by, "Concern", f"Your concern: {doc.status}"[:140],
		doc.subject + (f" - {doc.response[:200]}" if doc.response else ""),
		dedup_key=f"con:{doc.name}:{doc.status}:{doc.modified}"[:140],
		reference_doctype=DOCTYPE, reference_name=doc.name,
	)
	return serialize(doc, me, with_files=True)


@mobile_api(methods=("POST",))
def add_detail(concern_id: str | None = None, text: str | None = None):
	"""The person who raised it adds more to it (appended to the details)."""
	me = frappe.session.user
	perms.require("concerns", me)
	doc = _get(concern_id, me)
	if doc.raised_by != me:
		raise frappe.PermissionError("Only the person who raised it can add to it.")
	text = require_str(text, "Text", 4000)
	stamp = frappe.utils.format_datetime(timeutil.now_system(), "d MMM, h:mm a")
	doc.details = ((doc.details or "") + f"\n\n[{stamp}] {text}").strip()
	if doc.status in ("Resolved", "Closed"):
		doc.status = "Open"
	doc.save(ignore_permissions=True)
	for user in _readers(doc.audience) - {me}:
		frappe.get_attr("hipersign_mobile.notifications.queue.enqueue")(
			user, "Concern", "Concern updated", f"{doc.subject} - {text[:150]}",
			dedup_key=f"con:{doc.name}:add:{doc.modified}:{user}"[:140], reference_doctype=DOCTYPE, reference_name=doc.name,
		)
	return serialize(doc, me, with_files=True)
