import frappe


class ConflictError(frappe.ValidationError):
	"""The request is valid but collides with current state (e.g. slot just taken). HTTP 409."""

	http_status_code = 409
