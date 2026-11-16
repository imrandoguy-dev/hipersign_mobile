import frappe


def execute():
	"""Link earlier calls / recordings to the records that now have their numbers, and unlink
	calls filed under a record that doesn't have the number."""
	from hipersign_mobile.services.call_links import repair

	fixed = repair(days=120)
	frappe.logger().info(f"Hipersign: repaired call links {fixed}")
