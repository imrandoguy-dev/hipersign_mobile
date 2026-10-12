"""Timezone handling.

Frappe stores Datetime values as naive timestamps in the *system* timezone.
The API always emits ISO-8601 with an explicit offset and accepts ISO-8601 with
or without an offset (no offset = the user's timezone), so the phone never has
to guess what a bare timestamp means.
"""

from __future__ import annotations

import datetime as dt
from zoneinfo import ZoneInfo

import frappe
from frappe.utils import get_datetime, get_system_timezone


def system_tz() -> ZoneInfo:
	return ZoneInfo(get_system_timezone())


def user_tz(user: str | None = None) -> ZoneInfo:
	user = user or frappe.session.user
	tz = frappe.db.get_value("User", user, "time_zone") if user and user != "Guest" else None
	try:
		return ZoneInfo(tz) if tz else system_tz()
	except Exception:
		return system_tz()


def now_system() -> dt.datetime:
	"""Naive 'now' in system timezone (same convention as frappe.utils.now_datetime)."""
	return dt.datetime.now(system_tz()).replace(tzinfo=None, microsecond=0)


def to_iso(value) -> str | None:
	"""Naive system-tz datetime (or string) -> ISO string with offset."""
	if not value:
		return None
	value = get_datetime(value)
	if value.tzinfo is None:
		value = value.replace(tzinfo=system_tz())
	return value.isoformat()


def parse_client_datetime(value, user: str | None = None) -> dt.datetime:
	"""ISO string from the phone -> naive system-tz datetime for storage."""
	if isinstance(value, dt.datetime):
		parsed = value
	else:
		text = str(value or "").strip()
		if not text:
			raise frappe.ValidationError("A date and time is required.")
		try:
			parsed = dt.datetime.fromisoformat(text.replace("Z", "+00:00"))
		except ValueError:
			raise frappe.ValidationError(f"Invalid date/time: {text}")
	if parsed.tzinfo is None:
		parsed = parsed.replace(tzinfo=user_tz(user))
	return parsed.astimezone(system_tz()).replace(tzinfo=None, microsecond=0)


def parse_client_date(value) -> dt.date:
	text = str(value or "").strip()
	try:
		return dt.date.fromisoformat(text[:10])
	except ValueError:
		raise frappe.ValidationError(f"Invalid date: {text}")


def user_day_bounds(start_date: dt.date, end_date: dt.date, user: str | None = None):
	"""Inclusive local-day range for the user -> [start, end) in naive system time."""
	tz = user_tz(user)
	start_local = dt.datetime.combine(start_date, dt.time.min, tzinfo=tz)
	end_local = dt.datetime.combine(end_date + dt.timedelta(days=1), dt.time.min, tzinfo=tz)
	conv = lambda d: d.astimezone(system_tz()).replace(tzinfo=None)  # noqa: E731
	return conv(start_local), conv(end_local)


def user_today_start(user: str | None = None) -> dt.datetime:
	today = dt.datetime.now(user_tz(user)).date()
	return user_day_bounds(today, today, user)[0]
