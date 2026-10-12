"""Trial bookings against trainer availability.

Concurrency: every booking change for a trainer first takes a row lock on that
trainer's User row (SELECT ... FOR UPDATE). Two salespeople booking the same
trainer at the same moment are therefore serialized; the second one re-checks
overlaps after the first commits and gets CONFLICT. A slot-level unique key
would not be enough, because different slots of the same trainer can overlap.
"""

from __future__ import annotations

import frappe
from frappe.utils import get_datetime

from hipersign_mobile.services import permissions as perms
from hipersign_mobile.services import timeutil
from hipersign_mobile.services.errors import ConflictError
from hipersign_mobile.services.party import party_summary

ACTIVE_BOOKING_STATUSES = ("Pending Confirmation", "Booked", "Rescheduled")
BOOKING_TYPES = ("ERP Trial", "Training", "Other")
SLOT_TYPE_FOR = {
	"ERP Trial": ("Trial", "Both"),
	"Training": ("Training", "Both"),
	"Other": ("Trial", "Training", "Both"),
}


def lock_trainer(trainer: str):
	frappe.db.sql("select name from `tabUser` where name=%s for update", trainer)


def overlapping_bookings(trainer: str, start, end, exclude: str | None = None) -> list[str]:
	filters = [
		["trainer", "=", trainer],
		["status", "in", ACTIVE_BOOKING_STATUSES],
		["start_datetime", "<", end],
		["end_datetime", ">", start],
	]
	if exclude:
		filters.append(["name", "!=", exclude])
	return frappe.get_all("Trial Booking", filters=filters, pluck="name")


def _load_slot_for_booking(slot_name: str, booking_type: str, exclude_booking: str | None = None):
	if not slot_name or not frappe.db.exists("Trainer Slot", slot_name):
		raise frappe.DoesNotExistError("Trainer slot not found.")
	trainer = frappe.db.get_value("Trainer Slot", slot_name, "trainer")
	lock_trainer(trainer)
	slot = frappe.db.get_value(
		"Trainer Slot",
		slot_name,
		[
			"name",
			"trainer",
			"status",
			"slot_type",
			"start_datetime",
			"end_datetime",
			"location",
			"meeting_link",
		],
		as_dict=True,
		for_update=True,
	)
	if not frappe.db.get_value("User", slot.trainer, "enabled"):
		raise ConflictError("This trainer is no longer available.")
	if slot.status != "Available":
		raise ConflictError("This slot has just been taken. Please pick another time.")
	if get_datetime(slot.start_datetime) <= timeutil.now_system():
		raise ConflictError("This slot is in the past.")
	if slot.slot_type not in SLOT_TYPE_FOR[booking_type]:
		raise frappe.ValidationError(f"This slot is for {slot.slot_type} only.")
	if overlapping_bookings(slot.trainer, slot.start_datetime, slot.end_datetime, exclude=exclude_booking):
		raise ConflictError("The trainer already has a booking at this time.")
	return slot


def _party(customer, lead, user):
	if not customer and not lead:
		raise frappe.ValidationError("Select a customer or a lead for the trial.")
	if customer:
		perms.require_party("Customer", customer, user)
		return "Customer", customer
	perms.require_party("CRM Lead", lead, user)
	return "CRM Lead", lead


def book(
	user: str,
	*,
	customer=None,
	lead=None,
	trainer_slot=None,
	booking_type="ERP Trial",
	notes=None,
	idempotency_key=None,
):
	perms.require("trial_booking", user)
	if booking_type not in BOOKING_TYPES:
		raise frappe.ValidationError("Invalid booking type.")
	key = (idempotency_key or "").strip()[:140] or None
	if key:
		existing = frappe.db.get_value(
			"Trial Booking", {"idempotency_key": key}, ["name", "booked_by"], as_dict=True
		)
		if existing:
			if existing.booked_by != user:
				raise frappe.ValidationError("This request key was already used.")
			return frappe.get_doc("Trial Booking", existing.name), False

	ref_doctype, ref_name = _party(customer, lead, user)
	slot = _load_slot_for_booking(trainer_slot, booking_type)
	summary = party_summary(ref_doctype, ref_name) or {}

	booking = frappe.get_doc(
		{
			"doctype": "Trial Booking",
			"customer": customer if ref_doctype == "Customer" else None,
			"lead": lead if ref_doctype == "CRM Lead" else (lead or None),
			"party_name": summary.get("name") or ref_name,
			"trainer_slot": slot.name,
			"trainer": slot.trainer,
			"booking_type": booking_type,
			"status": "Booked",
			"start_datetime": slot.start_datetime,
			"end_datetime": slot.end_datetime,
			"booked_by": user,
			"notes": notes,
			"idempotency_key": key,
		}
	)
	booking.flags.hm_service = True
	booking.insert(ignore_permissions=True)
	frappe.db.set_value("Trainer Slot", slot.name, "status", "Reserved")

	# One calendar activity for the booking (never two records for one appointment).
	trainer_name = frappe.utils.get_fullname(slot.trainer)
	task = frappe.get_doc(
		{
			"doctype": "CRM Task",
			"title": f"{booking_type}: {booking.party_name} with {trainer_name}"[:140],
			"hm_activity_type": "Trial",
			"status": "Todo",
			"priority": "Medium",
			"assigned_to": user,
			"due_date": slot.start_datetime,
			"start_date": get_datetime(slot.start_datetime).date(),
			"hm_end_datetime": slot.end_datetime,
			"hm_meeting_link": slot.meeting_link,
			"reference_doctype": ref_doctype,
			"reference_docname": ref_name,
			"hm_trial_booking": booking.name,
			"description": notes,
		}
	)
	if slot.location and task.meta.has_field("custom_location"):
		task.custom_location = slot.location
	task.insert(ignore_permissions=True)
	booking.db_set("crm_task", task.name, update_modified=False)

	from hipersign_mobile.notifications.events import on_trial_event

	on_trial_event(booking, "booked", [slot.trainer])
	return booking, True


def _get_booking_for(user: str, booking_id: str, for_update: bool = False):
	if not booking_id or not frappe.db.exists("Trial Booking", booking_id):
		raise frappe.DoesNotExistError("Booking not found.")
	b = frappe.get_doc("Trial Booking", booking_id)
	caps = perms.capabilities(user)
	if not ("team_view" in caps or b.booked_by == user or b.trainer == user):
		raise frappe.DoesNotExistError("Booking not found.")
	if for_update:
		lock_trainer(b.trainer)
		b.reload()
	return b


def _sync_task(booking, status: str | None = None, reason: str | None = None):
	if not booking.crm_task or not frappe.db.exists("CRM Task", booking.crm_task):
		return
	task = frappe.get_doc("CRM Task", booking.crm_task)
	task.due_date = booking.start_datetime
	task.start_date = get_datetime(booking.start_datetime).date()
	task.hm_end_datetime = booking.end_datetime
	if status:
		task.status = status
	task.flags.hm_reason = reason
	task.save(ignore_permissions=True)


def reschedule(user: str, booking_id: str, new_slot: str, reason: str | None = None):
	booking = _get_booking_for(user, booking_id, for_update=True)
	if booking.status not in ACTIVE_BOOKING_STATUSES:
		raise frappe.ValidationError("Only active bookings can be rescheduled.")
	if booking.trainer_slot == new_slot:
		return booking, False
	new_trainer = frappe.db.get_value("Trainer Slot", new_slot, "trainer")
	if new_trainer and new_trainer != booking.trainer:
		lock_trainer(new_trainer)
	slot = _load_slot_for_booking(new_slot, booking.booking_type, exclude_booking=booking.name)

	old_slot, old_trainer = booking.trainer_slot, booking.trainer
	booking.update(
		{
			"trainer_slot": slot.name,
			"trainer": slot.trainer,
			"start_datetime": slot.start_datetime,
			"end_datetime": slot.end_datetime,
			"status": "Rescheduled",
		}
	)
	booking.flags.hm_service = True
	booking.save(ignore_permissions=True)
	frappe.db.set_value("Trainer Slot", old_slot, "status", "Available")
	frappe.db.set_value("Trainer Slot", slot.name, "status", "Reserved")
	_sync_task(booking, reason=reason)

	from hipersign_mobile.notifications.events import on_trial_event

	on_trial_event(booking, "rescheduled", [old_trainer, slot.trainer])
	return booking, True


def cancel(user: str, booking_id: str, reason: str | None = None):
	booking = _get_booking_for(user, booking_id, for_update=True)
	if booking.status == "Cancelled":
		return booking, False
	if booking.status == "Completed":
		raise frappe.ValidationError("A completed booking cannot be cancelled.")
	booking.status = "Cancelled"
	booking.cancellation_reason = reason
	booking.flags.hm_service = True
	booking.save(ignore_permissions=True)
	if frappe.db.get_value("Trainer Slot", booking.trainer_slot, "status") == "Reserved":
		frappe.db.set_value("Trainer Slot", booking.trainer_slot, "status", "Available")
	_sync_task(booking, status="Canceled", reason=reason)

	from hipersign_mobile.notifications.events import on_trial_event

	on_trial_event(booking, "cancelled", [booking.trainer, booking.booked_by])
	return booking, True


def complete(user: str, booking_id: str, notes: str | None = None):
	booking = _get_booking_for(user, booking_id, for_update=True)
	if booking.status == "Completed":
		return booking, False
	if booking.status == "Cancelled":
		raise frappe.ValidationError("A cancelled booking cannot be completed.")
	booking.status = "Completed"
	booking.completion_notes = notes
	booking.flags.hm_service = True
	booking.save(ignore_permissions=True)
	_sync_task(booking, status="Done", reason=notes)

	from hipersign_mobile.notifications.events import on_trial_event

	on_trial_event(booking, "completed", [booking.trainer, booking.booked_by])
	return booking, True


def available_slots(
	start, end, trainer: str | None = None, booking_type: str | None = None, limit: int = 200
):
	now = timeutil.now_system()
	filters = [
		["status", "=", "Available"],
		["start_datetime", ">=", max(start, now)],
		["start_datetime", "<", end],
	]
	if trainer:
		filters.append(["trainer", "=", trainer])
	if booking_type:
		filters.append(["slot_type", "in", SLOT_TYPE_FOR.get(booking_type, ("Both",))])
	slots = frappe.get_all(
		"Trainer Slot",
		filters=filters,
		fields=[
			"name",
			"trainer",
			"trainer_name",
			"slot_type",
			"start_datetime",
			"end_datetime",
			"location",
			"meeting_link",
		],
		order_by="start_datetime asc",
		limit=limit,
	)
	# Advisory only: hide slots that overlap an existing booking; book() re-checks under lock.
	out = []
	for s in slots:
		if overlapping_bookings(s.trainer, s.start_datetime, s.end_datetime):
			continue
		if not frappe.db.get_value("User", s.trainer, "enabled"):
			continue
		out.append(
			{
				"id": s.name,
				"trainer": s.trainer,
				"trainer_name": s.trainer_name or frappe.utils.get_fullname(s.trainer),
				"slot_type": s.slot_type,
				"start": timeutil.to_iso(s.start_datetime),
				"end": timeutil.to_iso(s.end_datetime),
				"location": s.location,
				"meeting_link": s.meeting_link,
			}
		)
	return out


def serialize(b) -> dict:
	return {
		"id": b.name,
		"status": b.status,
		"booking_type": b.booking_type,
		"party_name": b.party_name,
		"customer": b.customer,
		"lead": b.lead,
		"party": party_summary("Customer", b.customer) if b.customer else party_summary("CRM Lead", b.lead),
		"trainer": b.trainer,
		"trainer_name": frappe.utils.get_fullname(b.trainer) if b.trainer else None,
		"trainer_slot": b.trainer_slot,
		"start": timeutil.to_iso(b.start_datetime),
		"end": timeutil.to_iso(b.end_datetime),
		"booked_by": b.booked_by,
		"activity_id": str(b.crm_task) if b.crm_task else None,
		"notes": b.notes,
		"cancellation_reason": b.cancellation_reason,
		"completion_notes": b.completion_notes,
		"modified": timeutil.to_iso(b.modified),
	}


def expire_past_slots():
	frappe.db.sql(
		"""update `tabTrainer Slot` set status='Expired'
		where status='Available' and end_datetime < %s""",
		timeutil.now_system(),
	)
