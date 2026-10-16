"""Development tasks for the app: list, details, assign, update status / deadline, notes."""

from __future__ import annotations

import frappe
from frappe.utils import cint

from hipersign_mobile.api._common import (
	choice,
	mobile_api,
	optional_str,
	page_args,
	page_result,
	require_str,
)
from hipersign_mobile.services import development as dev
from hipersign_mobile.services import permissions as perms
from hipersign_mobile.services import timeutil

SCOPES = ("mine", "assigned", "team")
FILTERS = ("open", "late", "done", "all")


def _get(task_id: str, me: str):
	name = require_str(task_id, "Task", 40)
	if not frappe.db.exists(dev.DOCTYPE, name):
		raise frappe.DoesNotExistError("Development task not found.")
	doc = frappe.get_doc(dev.DOCTYPE, name)
	if "assign_development" not in perms.capabilities(me) and me not in (doc.developer, doc.assigned_by):
		raise frappe.DoesNotExistError("Development task not found.")
	return doc


@mobile_api(methods=("GET",))
def list_tasks(scope: str | None = None, filter: str | None = None, cursor=None, page_size=None):
	"""mine = assigned to me; assigned = I assigned them; team = everyone's (managers)."""
	me = frappe.session.user
	perms.require("development", me)
	caps = perms.capabilities(me)
	scope = choice(scope, SCOPES, "scope", default="mine")
	if scope == "team" and "assign_development" not in caps:
		raise frappe.PermissionError("Only managers can see everyone's development tasks.")
	kind = choice(filter, FILTERS, "filter", default="open")
	offset, limit = page_args(cursor, page_size)
	filters = []
	if scope == "mine":
		filters.append(["developer", "=", me])
	elif scope == "assigned":
		filters.append(["assigned_by", "=", me])
	if kind == "open":
		filters.append(["status", "in", dev.OPEN])
	elif kind == "late":
		filters += [["status", "in", dev.OPEN], ["deadline", "<", timeutil.now_system()]]
	elif kind == "done":
		filters.append(["status", "in", ("Done", "Cancelled")])
	order = "deadline desc" if kind == "done" else "deadline asc"
	names = frappe.get_all(dev.DOCTYPE, filters=filters, pluck="name", order_by=order, start=offset, limit=limit + 1)
	items = [dev.serialize(frappe.get_doc(dev.DOCTYPE, n), me) for n in names[:limit]]
	counts = {
		"open": frappe.db.count(dev.DOCTYPE, {"developer": me, "status": ["in", dev.OPEN]}),
		"late": frappe.db.count(
			dev.DOCTYPE, {"developer": me, "status": ["in", dev.OPEN], "deadline": ["<", timeutil.now_system()]}
		),
	}
	return {**page_result(items, offset, limit, len(names) > limit), "counts": counts}


@mobile_api(methods=("GET",))
def get_task(task_id: str | None = None):
	me = frappe.session.user
	perms.require("development", me)
	doc = _get(task_id, me)
	out = dev.serialize(doc, me)
	out["notes"] = [
		{
			"text": frappe.utils.strip_html(c.content or ""),
			"by": frappe.utils.get_fullname(c.owner),
			"at": timeutil.to_iso(c.creation),
		}
		for c in frappe.get_all(
			"Comment",
			filters={"reference_doctype": dev.DOCTYPE, "reference_name": doc.name, "comment_type": "Comment"},
			fields=["content", "owner", "creation"],
			order_by="creation desc",
			limit=50,
		)
	]
	return out


@mobile_api(methods=("GET",))
def developers():
	"""People who can be given development work."""
	me = frappe.session.user
	perms.require("development", me)
	users = set(
		frappe.get_all("Has Role", filters={"role": perms.ROLE_DEVELOPER, "parenttype": "User"}, pluck="parent")
	)
	if "assign_development" not in perms.capabilities(me):
		users &= {me}
	rows = frappe.get_all(
		"User",
		filters={"name": ["in", list(users) or [""]], "enabled": 1},
		fields=["name", "full_name"],
		order_by="full_name asc",
	)
	return {"items": [{"id": r.name, "name": r.full_name or r.name} for r in rows]}


def _apply(doc, me, title, description, developer, deadline, priority, reminders, project, customer):
	if title is not None:
		doc.title = require_str(title, "Title", 200)
	if description is not None:
		doc.description = optional_str(description, 20000)
	if developer is not None:
		developer = require_str(developer, "Developer", 140)
		if "assign_development" not in perms.capabilities(me) and developer != me:
			raise frappe.PermissionError("Only managers can give development work to someone else.")
		if perms.ROLE_DEVELOPER not in frappe.get_roles(developer):
			raise frappe.ValidationError("Choose a developer (a user with the Hipersign Developer role).")
		doc.developer = developer
	if deadline is not None:
		doc.deadline = timeutil.parse_client_datetime(deadline, me)
	if priority is not None:
		doc.priority = choice(priority, dev.PRIORITIES, "priority")
	if reminders is not None:
		doc.reminders = optional_str(",".join(str(r) for r in reminders) if isinstance(reminders, list) else reminders, 100)
	if project is not None:
		doc.project_name = optional_str(project, 140)
	if customer is not None:
		doc.customer = customer if customer and frappe.db.exists("Customer", customer) else None


@mobile_api(methods=("POST",))
def create_task(
	title: str | None = None,
	description: str | None = None,
	developer: str | None = None,
	deadline: str | None = None,
	priority: str | None = None,
	reminders=None,
	project: str | None = None,
	customer: str | None = None,
):
	me = frappe.session.user
	perms.require("development", me)
	if not deadline:
		raise frappe.ValidationError("Set a deadline.")
	doc = frappe.new_doc(dev.DOCTYPE)
	doc.assigned_by = me
	doc.status = "Open"
	_apply(doc, me, title, description, developer or me, deadline, priority or "Medium", reminders, project, customer)
	if doc.deadline <= timeutil.now_system():
		raise frappe.ValidationError("The deadline must be in the future.")
	doc.insert(ignore_permissions=True)
	return dev.serialize(doc, me)


@mobile_api(methods=("POST",))
def update_task(
	task_id: str | None = None,
	status: str | None = None,
	title: str | None = None,
	description: str | None = None,
	developer: str | None = None,
	deadline: str | None = None,
	priority: str | None = None,
	reminders=None,
	project: str | None = None,
	customer: str | None = None,
	note: str | None = None,
):
	"""The developer can change the status and reminders and add notes; whoever assigned it
	(or a manager) can change everything."""
	me = frappe.session.user
	perms.require("development", me)
	doc = _get(task_id, me)
	editing = any(v is not None for v in (title, description, developer, deadline, priority, project, customer))
	if editing and not dev.can_edit(doc, me):
		raise frappe.PermissionError("Only the person who assigned this task can change it.")
	_apply(doc, me, title, description, developer, deadline, priority, reminders, project, customer)
	if status is not None:
		doc.status = choice(status, dev.STATUSES, "status")
	doc.save(ignore_permissions=True)
	if note:
		doc.add_comment("Comment", optional_str(note, 4000))
	return dev.serialize(doc, me)
