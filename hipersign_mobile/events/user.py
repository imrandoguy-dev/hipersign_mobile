import frappe


def on_update(doc, method=None):
	"""A disabled user must stop receiving pushes on every device immediately."""
	if not doc.enabled and doc.has_value_changed("enabled"):
		frappe.db.set_value("Mobile Device", {"user": doc.name, "is_active": 1}, "is_active", 0)
