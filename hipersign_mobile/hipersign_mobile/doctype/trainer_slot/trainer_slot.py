# Copyright (c) 2026, Hipersign Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document
from frappe.utils import get_datetime, time_diff_in_seconds


class TrainerSlot(Document):
	def validate(self):
		if get_datetime(self.end_datetime) <= get_datetime(self.start_datetime):
			frappe.throw("End must be after start.")
		self.duration_minutes = int(time_diff_in_seconds(self.end_datetime, self.start_datetime) // 60)

		roles = set(frappe.get_roles())
		privileged = roles & {"System Manager", "Hipersign Manager"}
		if not privileged and self.trainer != frappe.session.user:
			frappe.throw("Trainers can only manage their own availability.", frappe.PermissionError)

		if self.status in ("Available", "Reserved"):
			clash = frappe.get_all(
				"Trainer Slot",
				filters=[
					["trainer", "=", self.trainer],
					["status", "in", ("Available", "Reserved")],
					["start_datetime", "<", self.end_datetime],
					["end_datetime", ">", self.start_datetime],
					["name", "!=", self.name or ""],
				],
				pluck="name",
				limit=1,
			)
			if clash:
				frappe.throw(f"This overlaps another slot of the same trainer ({clash[0]}).")

		if not self.is_new():
			before = self.get_doc_before_save()
			if before and before.status == "Reserved" and self._active_booking():
				changed = any(
					str(before.get(f)) != str(self.get(f))
					for f in ("trainer", "start_datetime", "end_datetime")
				)
				if changed or self.status != "Reserved":
					frappe.throw("This slot has an active booking. Reschedule or cancel the booking first.")

	def on_trash(self):
		if self._active_booking():
			frappe.throw("This slot has an active booking. Cancel the booking first.")

	def _active_booking(self):
		return frappe.db.exists(
			"Trial Booking",
			{"trainer_slot": self.name, "status": ["in", ("Pending Confirmation", "Booked", "Rescheduled")]},
		)
