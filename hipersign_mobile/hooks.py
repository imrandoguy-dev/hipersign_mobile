app_name = "hipersign_mobile"
app_title = "Hipersign Mobile"
app_publisher = "Hipersign Technologies"
app_description = "Backend for the Hipersign mobile app"
app_email = "dev@hipersign.com"
app_license = "mit"

# Frappe CRM provides CRM Task / CRM Lead / CRM Deal; ERPNext provides Customer.
required_apps = ["erpnext", "crm"]

after_install = "hipersign_mobile.install.after_install"
after_migrate = "hipersign_mobile.install.after_migrate"
before_uninstall = "hipersign_mobile.install.before_uninstall"

# Every CRM Task change (from the app, the CRM UI or Desk) goes through these hooks,
# so audit history, reschedule counting and notifications stay consistent everywhere.
doc_events = {
	"CRM Task": {
		"before_insert": "hipersign_mobile.events.crm_task.before_insert",
		"validate": "hipersign_mobile.events.crm_task.validate",
		"after_insert": "hipersign_mobile.events.crm_task.after_insert",
		"on_update": "hipersign_mobile.events.crm_task.on_update",
		"on_trash": "hipersign_mobile.events.crm_task.on_trash",
	},
	"User": {
		"on_update": "hipersign_mobile.events.user.on_update",
	},
}

# Mobile-only users see only their own CRM Tasks everywhere (Desk, CRM UI, REST).
permission_query_conditions = {"CRM Task": "hipersign_mobile.permissions.crm_task_query"}
has_permission = {"CRM Task": "hipersign_mobile.permissions.crm_task_has_permission"}

# Audit history and notification log must survive (and never block) deleting a task.
ignore_links_on_delete = ["Sales Activity History", "Mobile Notification"]

scheduler_events = {
	"cron": {
		# Deliver due reminders and retry transient push failures.
		"* * * * *": ["hipersign_mobile.notifications.queue.process_due_notifications"],
		# Mark trainer slots that ended without a booking as Expired.
		"*/15 * * * *": ["hipersign_mobile.services.booking_service.expire_past_slots"],
	},
	"daily": ["hipersign_mobile.notifications.queue.purge_old_notifications"],
}
