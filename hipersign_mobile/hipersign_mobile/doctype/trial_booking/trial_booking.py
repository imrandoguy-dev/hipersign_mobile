# Copyright (c) 2026, Hipersign Technologies and contributors
# For license information, please see license.txt

import frappe
from frappe.model.document import Document

ACTIVE = ("Pending Confirmation", "Booked", "Rescheduled")


class TrialBooking(Document):
	def validate(self):
		if not self.customer and not self.lead:
			frappe.throw("Select a customer or a lead.")
		slot = frappe.db.get_value(
			"Trainer Slot", self.trainer_slot, ["trainer", "start_datetime", "end_datetime"], as_dict=True
		)
		if not slot:
			frappe.throw("Trainer slot not found.")
		# Times and trainer always come from the slot, never from user input.
		self.trainer = slot.trainer
		self.start_datetime = slot.start_datetime
		self.end_datetime = slot.end_datetime
		if not self.booked_by:
			self.booked_by = frappe.session.user
		if not self.party_name:
			self.party_name = (
				frappe.db.get_value("Customer", self.customer, "customer_name")
				if self.customer
				else frappe.db.get_value("CRM Lead", self.lead, "lead_name") or self.lead
			)

		if self.status in ACTIVE:
			clash = frappe.get_all(
				"Trial Booking",
				filters=[
					["trainer", "=", self.trainer],
					["status", "in", ACTIVE],
					["start_datetime", "<", self.end_datetime],
					["end_datetime", ">", self.start_datetime],
					["name", "!=", self.name or ""],
				],
				pluck="name",
				limit=1,
			)
			if clash:
				from hipersign_mobile.services.errors import ConflictError

				frappe.throw(f"The trainer already has a booking at this time ({clash[0]}).", ConflictError)

	def on_update(self):
		# Desk edits (not via the mobile service) still keep the slot status right.
		if self.flags.get("hm_service"):
			return
		if self.status in ACTIVE:
			frappe.db.set_value("Trainer Slot", self.trainer_slot, "status", "Reserved")
		elif self.status == "Cancelled":
			if frappe.db.get_value("Trainer Slot", self.trainer_slot, "status") == "Reserved":
				frappe.db.set_value("Trainer Slot", self.trainer_slot, "status", "Available")
