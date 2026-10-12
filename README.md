# Hipersign Mobile — Frappe backend

Backend for the **Hipersign** mobile app (Flutter, Android + iOS). It adds secure,
role-aware mobile APIs on top of ERPNext v16 and Frappe CRM: calendar, follow-ups,
meetings, customers, trial bookings and push notifications.

ERPNext stays the single source of truth. Follow-ups and meetings are ordinary
**Frappe CRM `CRM Task`** records (extended with `hm_*` fields), so whatever a
salesperson does in the app is immediately visible on the Lead or Deal in Frappe
CRM, and anything changed in ERPNext shows up in the app on the next sync.

Mobile app repo: `hipersign_mobile_app`.

## What it adds

| Item | Purpose |
|---|---|
| Roles `Hipersign Sales`, `Hipersign Manager`, `Hipersign Trainer`, `Hipersign Cold Caller` | The admin assigns these on the User record. The app shows the matching workspace; the server enforces it. |
| Custom fields on **CRM Task** (`hm_activity_type`, `hm_end_datetime`, `hm_reschedule_count`, `hm_completed_at/by`, `hm_outcome`, …) | Follow-up / meeting / trial activities. The CRM Task `status` is untouched, so the CRM UI keeps working. |
| **Sales Activity History** | Immutable audit trail: created, status changes, reschedules (old → new time), reassignments, notes, call/WhatsApp taps. Written for app *and* Desk/CRM edits. |
| **Trainer Slot** | Trainer availability. Managed on Desk now; the trainer workspace comes later. |
| **Trial Booking** | Customer trial/training bookings, linked to exactly one calendar activity. |
| **Mobile Device**, **Mobile Notification**, **Mobile User Preference** | Push registrations, the notification queue (dedupe, retry, delivery status) and per-user reminder settings. |
| **Hipersign Mobile Settings** | Default country code (`91`), customer visibility, push on/off, default reminder time, etc. |

### Status colours in the app

| App status | Rule | Colour |
|---|---|---|
| Scheduled (active) | open, dated today or later | light blue |
| Pending | open and overdue, or no date yet | yellow-orange |
| Rescheduled | open and moved at least once | violet |
| Completed | CRM Task `Done` | green |

## Install

> Back up the site first. Test on a staging copy before production.

**Frappe Press** (`server1`): add the GitHub repo as an app source, include it in the
bench group, deploy, then install it on the site from the dashboard.

**Manual bench** (inside the bench container):

```bash
cd ~/frappe-bench
bench get-app https://github.com/imrandoguy-dev/hipersign_mobile
bench --site nexus365.hipersignerp.cloud install-app hipersign_mobile
bench --site nexus365.hipersignerp.cloud migrate
bench restart
```

Installing is additive. It creates the doctypes and roles, adds the `hm_*` fields and
two indexes to CRM Task, gives the Hipersign roles read/write on CRM Task, and fills
`hm_activity_type` on existing tasks (tasks with a Visit Type become *Meeting*; all
others become *Follow-up*). It does not modify or move existing CRM data.

Requirements: ERPNext, Frappe CRM (`crm`), and a running scheduler and workers
(reminders are sent by a job that runs every minute).

## Configure

1. **Hipersign Mobile Settings** (Desk): check the default country code (`91`) and the
   customer visibility (*All Records* is the agreed default).
2. **Users**: give each salesperson the **Hipersign Sales** role, and managers
   **Hipersign Manager**. Users with no Hipersign role see "no mobile workspace" in the app.
3. **Trainers**: give the **Hipersign Trainer** role and create **Trainer Slot** records
   for their free time. Salespeople can only book slots that are *Available*.
4. **Push notifications (FCM)**:
   - Firebase console → Project settings → Service accounts → *Generate new private key*.
   - Copy the JSON to the server, outside the web root, and point site config at it:
     ```bash
     bench --site nexus365.hipersignerp.cloud set-config hipersign_mobile_fcm_credentials /home/frappe/secrets/hipersign-fcm.json
     ```
   - Tick **Enable Push Notifications** in Hipersign Mobile Settings.
   - For iOS, upload an APNs auth key in Firebase → Cloud Messaging.
   - The credentials never go into the app.

### Privacy note: the Sales User role

The app only ever shows a salesperson their own activities. A user who has *only*
Hipersign roles is also limited to their own CRM Tasks in Desk, the CRM UI and the REST
API. Users who additionally have ERPNext's **Sales User** role keep Frappe CRM's normal
"see all tasks" access in Desk. To hide other people's tasks from them too, tick
**Restrict Task Visibility** in settings. Managers are never restricted.

## Mobile API

Base: `https://<site>/api/method/hipersign_mobile.api.<module>.<function>`

Sign-in uses Frappe's own `POST /api/method/login`, so lockouts, IP restrictions
and 2FA still apply. The returned `sid` cookie is stored in the phone's
Keychain/Keystore. No API keys are embedded in the app. Every response looks like this:

```json
{"message": {"success": true, "data": {}, "request_id": "...", "api_version": 1}}
{"message": {"success": false, "error": {"code": "CONFLICT", "message": "..."}, "request_id": "..."}}
```

Error codes: `UNAUTHENTICATED` 401, `FORBIDDEN` 403, `NOT_FOUND` 404, `CONFLICT` 409,
`VALIDATION_ERROR` 422, `RATE_LIMITED` 429, `SERVER_ERROR` 500. A failed call rolls
back fully, so nothing is half-saved.

| Method | Endpoint | Notes |
|---|---|---|
| GET | `auth.get_me` | user, roles, capabilities, workspaces, timezone |
| GET | `auth.get_config` | public; used to validate the server address |
| POST | `auth.logout_mobile` | `device_id` |
| GET | `calendar.get_my_day` | `start_date`, `end_date` (local dates). Returns `items`, `overdue`, `unscheduled` |
| GET | `calendar.get_month_markers` | `month=2026-10` |
| GET | `activities.get_followups` / `get_meetings` | `status` = Pending / Completed / Rescheduled / Cancelled, `cursor`, `page_size` |
| GET | `activities.get_detail` | `activity_id` (includes history) |
| POST | `activities.create_activity` | `title`, `activity_type`, `reference_doctype` + `reference_name` (Customer / CRM Lead / CRM Deal), `start`, `end`, `priority`, `idempotency_key` |
| POST | `activities.update_status` | `activity_id`, `status` = Completed / Cancelled / In Progress / Pending, `outcome`, `notes` |
| POST | `activities.reschedule_activity` | `activity_id`, `new_start_datetime`, `new_end_datetime`, `reason` |
| POST | `activities.add_note` | also mirrored to the Lead/Deal as an FCRM Note |
| POST | `activities.log_contact` | `channel` = Call / WhatsApp (never completes anything) |
| GET | `customers.search` | `query` (name / phone / email), `kind` = all / customer / lead |
| GET | `customers.get_detail` | `doctype`, `name` → phones (E.164, `tel:` and `wa.me` links), activities, next activity |
| GET | `trials.get_available_slots` | `start_date`, `end_date`, `trainer`, `booking_type` |
| GET | `trials.list_bookings`, `trials.get_booking_detail`, `trials.get_trainers` | |
| POST | `trials.book_trial` | `trainer_slot`, `customer` or `lead`, `booking_type`, `notes`, `idempotency_key` → 409 if just taken |
| POST | `trials.reschedule_booking` / `cancel_booking` / `complete_booking` | |
| POST | `devices.register_device` / `deactivate_device` | |
| GET/POST | `devices.get_notification_preferences` / `update_notification_preferences` | |
| GET | `devices.get_notification_history` | |

Datetimes are ISO-8601 with an offset (`2026-10-13T10:30:00+05:30`). Input without
an offset is read in the user's ERPNext timezone.

## Notifications

- **Assignment**: someone else creates or reassigns a task to you.
- **Rescheduled**: someone else moves your task.
- **Reminder**: N minutes before the activity (per-user setting, default 15). When the
  activity moves, the reminder is re-planned; when it is completed or cancelled, the
  reminder is cancelled.
- **Trial update**: the trainer is told about bookings, moves and cancellations.

Push payloads are generic ("New follow-up assigned", with the time). They contain no
customer names, numbers or notes; the app loads the details after the tap. Each push
has a unique deduplication key. Temporary FCM failures retry with backoff (1, 2, 4…
minutes, at most 5 attempts). An invalid token deactivates that device.

## Tests

```bash
bench --site <test-site> set-config allow_tests true
bench --site <test-site> run-tests --app hipersign_mobile        # 37 tests
python scripts/e2e_http_check.py --base https://<site> --host <site> \
  --sales1 <user1> --sales2 <user2> --trainer <trainer> --password '<pw>' \
  --admin-password '<admin pw>' --customer "<customer name>"  # 25 live HTTP checks
```

### Acceptance checklist → evidence

| Criterion | Covered by |
|---|---|
| Salesperson logs in with ERPNext authentication | e2e: login / wrong password 401 |
| Correct workspace from roles | `TestAccess.test_salesperson_gets_sales_workspace`, `…without_mobile_role…` |
| Can't fetch another employee's activities by editing requests | `TestAccess.test_cannot_*`; e2e: 404 via mobile API, 403 via `/api/resource`, list API filtered |
| Correct date and timezone | `TestCalendar.test_activity_on_correct_local_date_and_timezone` |
| Pending / completed / rescheduled states | `TestCalendar.test_display_states` |
| Complete / reschedule update ERPNext and keep history | `test_complete_updates_erpnext_and_is_idempotent`, `test_reschedule_preserves_history` |
| Call / WhatsApp open the correct number | `TestCustomers.test_phone_normalisation`, `test_call_and_whatsapp_links_on_activity` (+ app tests) |
| Customer search only returns authorised records | `test_search_respects_visibility_setting`, `test_activity_details_hide_other_employees_work` |
| Book a valid slot | `TestTrials.test_book_valid_slot_creates_one_calendar_activity` |
| No conflicting bookings, including simultaneous ones | `test_conflicting_booking_rejected`, `test_overlapping_slots_cannot_both_be_booked`; e2e: 8 parallel requests → exactly 1 succeeds, 7 get 409 |
| Booking changes reach calendars and notifications | `test_reschedule_and_cancel_booking`, trainer notification assertion |
| Assignment triggers push | `TestNotifications.test_assignment_creates_push_with_generic_payload` |
| Reminder at configured time | `test_reminder_scheduled_and_replaced_on_reschedule`, `test_due_reminder_is_delivered_by_scheduler` |
| Tapping opens the activity | `route` payload asserted in `test_due_reminder_is_delivered_by_scheduler` |
| Denied permission / invalid token / failed delivery don't crash | `test_invalid_token_*`, `test_transient_failure_*`, `test_no_device_*` |
| Mobile-created activity saved in ERPNext | `TestSync.test_mobile_created_activity_is_saved_in_erpnext`; e2e |
| ERPNext-side change appears in the app | `test_desk_side_change_appears_in_app`; e2e |
| Network failures don't lose changes or create duplicates | `test_retry_with_same_key_does_not_duplicate`, `test_failed_write_leaves_nothing_behind` |

Real-device checks still to run on phones: push delivery with the app in the
background or killed, battery-saver behaviour on Xiaomi/Oppo/Vivo, and iOS permission flows.

## Uninstall

`bench --site <site> uninstall-app hipersign_mobile` removes the doctypes and the `hm_*`
custom fields. CRM Tasks themselves are kept.
