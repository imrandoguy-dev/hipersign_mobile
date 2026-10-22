"""Development tasks (kind=dev, for developers) and team tasks (kind=team: accounts, marketing,
hiring...) for the app: list, details, assign, update status / deadline, notes."""

from __future__ import annotations

import frappe

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


def _kind(kind: str | None, me: str):
	k = dev.kind(choice(kind, tuple(dev.KINDS), "kind", default="dev"))
	perms.require(k.cap, me)
	return k


def _get(k, task_id: str, me: str):
	name = require_str(task_id, "Task", 40)
	if not frappe.db.exists(k.doctype, name):
		raise frappe.DoesNotExistError("Task not found.")
	doc = frappe.get_doc(k.doctype, name)
	if not dev.can_see(doc, me):
		raise frappe.DoesNotExistError("Task not found.")
	return doc


@mobile_api(methods=("GET",))
def list_tasks(
	scope: str | None = None, filter: str | None = None, cursor=None, page_size=None, kind: str | None = None,
	team: str | None = None,
):
	"""mine = assigned to me; assigned = I assigned them; team = everyone's (managers)."""
	me = frappe.session.user
	k = _kind(kind, me)
	caps = perms.capabilities(me)
	scope = choice(scope, SCOPES, "scope", default="mine")
	if scope == "team" and k.assign_cap not in caps:
		raise frappe.PermissionError("Only managers can see everyone's tasks.")
	kind_filter = choice(filter, FILTERS, "filter", default="open")
	offset, limit = page_args(cursor, page_size)
	filters = []
	if scope == "mine":
		filters.append([k.person, "=", me])
	elif scope == "assigned":
		filters.append(["assigned_by", "=", me])
	if team and k.doctype == dev.TEAM_DOCTYPE:
		filters.append(["team", "=", optional_str(team, 140)])
	if kind_filter == "open":
		filters.append(["status", "in", dev.OPEN])
	elif kind_filter == "late":
		filters += [["status", "in", dev.OPEN], ["deadline", "<", timeutil.now_system()]]
	elif kind_filter == "done":
		filters.append(["status", "in", ("Done", "Cancelled")])
	order = "deadline desc" if kind_filter == "done" else "deadline asc"
	names = frappe.get_all(k.doctype, filters=filters, pluck="name", order_by=order, start=offset, limit=limit + 1)
	items = [dev.serialize(frappe.get_doc(k.doctype, n), me) for n in names[:limit]]
	counts = {
		"open": frappe.db.count(k.doctype, {k.person: me, "status": ["in", dev.OPEN]}),
		"late": frappe.db.count(
			k.doctype, {k.person: me, "status": ["in", dev.OPEN], "deadline": ["<", timeutil.now_system()]}
		),
	}
	out = {**page_result(items, offset, limit, len(names) > limit), "counts": counts}
	if k.doctype == dev.TEAM_DOCTYPE:
		out["teams"] = dev.teams()
	return out


@mobile_api(methods=("GET",))
def get_task(task_id: str | None = None, kind: str | None = None):
	me = frappe.session.user
	k = _kind(kind, me)
	doc = _get(k, task_id, me)
	out = dev.serialize(doc, me)
	out["notes"] = [
		{
			"text": frappe.utils.strip_html(c.content or ""),
			"by": frappe.utils.get_fullname(c.owner),
			"at": timeutil.to_iso(c.creation),
		}
		for c in frappe.get_all(
			"Comment",
			filters={"reference_doctype": k.doctype, "reference_name": doc.name, "comment_type": "Comment"},
			fields=["content", "owner", "creation"],
			order_by="creation desc",
			limit=50,
		)
	]
	return out


@mobile_api(methods=("GET",))
def developers(kind: str | None = None):
	"""People who can be given this kind of work: developers for development tasks,
	every active employee user for team tasks."""
	me = frappe.session.user
	k = _kind(kind, me)
	if k.doctype == dev.DOCTYPE:
		users = set(
			frappe.get_all("Has Role", filters={"role": perms.ROLE_DEVELOPER, "parenttype": "User"}, pluck="parent")
		)
	else:
		users = set(frappe.get_all("User", filters={"enabled": 1, "user_type": "System User"}, pluck="name"))
		if frappe.db.table_exists("Employee"):
			users |= {u for u in frappe.get_all("Employee", filters={"status": "Active"}, pluck="user_id") if u}
		users -= {"Administrator", "Guest"}
	if k.assign_cap not in perms.capabilities(me):
		users &= {me}
	rows = frappe.get_all(
		"User",
		filters={"name": ["in", list(users) or [""]], "enabled": 1},
		fields=["name", "full_name"],
		order_by="full_name asc",
	)
	out = {"items": [{"id": r.name, "name": r.full_name or r.name} for r in rows]}
	if k.doctype == dev.TEAM_DOCTYPE:
		out["teams"] = dev.teams()
	return out


def _apply(k, doc, me, title, description, developer, deadline, priority, reminders, project, customer, team):
	if title is not None:
		doc.title = require_str(title, "Title", 200)
	if description is not None:
		doc.description = optional_str(description, 20000)
	if developer is not None:
		developer = require_str(developer, "Person", 140)
		if k.assign_cap not in perms.capabilities(me) and developer != me:
			raise frappe.PermissionError("Only managers can give work to someone else.")
		if k.doctype == dev.DOCTYPE and perms.ROLE_DEVELOPER not in frappe.get_roles(developer):
			raise frappe.ValidationError("Choose a developer (a user with the Hipersign Developer role).")
		if not frappe.db.get_value("User", developer, "enabled"):
			raise frappe.ValidationError("This person's account is disabled.")
		doc.set(k.person, developer)
	if deadline is not None:
		doc.deadline = timeutil.parse_client_datetime(deadline, me)
	if priority is not None:
		doc.priority = choice(priority, dev.PRIORITIES, "priority")
	if reminders is not None:
		doc.reminders = optional_str(",".join(str(r) for r in reminders) if isinstance(reminders, list) else reminders, 100)
	if project is not None and k.doctype == dev.DOCTYPE:
		doc.project_name = optional_str(project, 140)
	if team is not None and k.doctype == dev.TEAM_DOCTYPE:
		doc.team = optional_str(team, 140)
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
	kind: str | None = None,
	team: str | None = None,
):
	me = frappe.session.user
	k = _kind(kind, me)
	if not deadline:
		raise frappe.ValidationError("Set a deadline.")
	doc = frappe.new_doc(k.doctype)
	doc.assigned_by = me
	doc.status = "Open"
	_apply(k, doc, me, title, description, developer or me, deadline, priority or "Medium", reminders, project, customer, team)
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
	kind: str | None = None,
	team: str | None = None,
):
	"""The person doing it can change the status and reminders and add notes; whoever assigned it
	(or a manager) can change everything."""
	me = frappe.session.user
	k = _kind(kind, me)
	doc = _get(k, task_id, me)
	editing = any(v is not None for v in (title, description, developer, deadline, priority, project, customer, team))
	if editing and not dev.can_edit(doc, me):
		raise frappe.PermissionError("Only the person who assigned this task can change it.")
	_apply(k, doc, me, title, description, developer, deadline, priority, reminders, project, customer, team)
	if status is not None:
		doc.status = choice(status, dev.STATUSES, "status")
	doc.save(ignore_permissions=True)
	if note:
		doc.add_comment("Comment", optional_str(note, 4000))
	return dev.serialize(doc, me)
