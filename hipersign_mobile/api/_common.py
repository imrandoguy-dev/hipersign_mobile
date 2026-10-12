"""Shared plumbing for every mobile endpoint: envelope, error mapping, pagination."""

from __future__ import annotations

import functools
import re
import uuid

import frappe
from frappe.utils import cint

from hipersign_mobile.services import settings as hm_settings
from hipersign_mobile.services.permissions import require_active_user

API_VERSION = 1


from hipersign_mobile.services.errors import ConflictError  # noqa: E402  (re-exported)

# Order matters: subclasses before their parents.
_ERROR_MAP = (
	(frappe.AuthenticationError, 401, "UNAUTHENTICATED"),
	(frappe.SessionExpired, 401, "UNAUTHENTICATED"),
	(frappe.PermissionError, 403, "FORBIDDEN"),
	(frappe.DoesNotExistError, 404, "NOT_FOUND"),
	(ConflictError, 409, "CONFLICT"),
	(frappe.UniqueValidationError, 409, "CONFLICT"),
	(frappe.DuplicateEntryError, 409, "CONFLICT"),
	(frappe.TimestampMismatchError, 409, "CONFLICT"),
	(frappe.QueryDeadlockError, 409, "CONFLICT"),
	(frappe.RateLimitExceededError, 429, "RATE_LIMITED"),
	(frappe.ValidationError, 422, "VALIDATION_ERROR"),
)


def _classify(exc: Exception) -> tuple[int, str]:
	for cls, status, code in _ERROR_MAP:
		if isinstance(exc, cls):
			return status, code
	return 500, "SERVER_ERROR"


def _clean(text: str) -> str:
	text = re.sub(r"<[^>]+>", "", str(text or "")).strip()
	return text[:500]


def mobile_api(methods=("GET",), allow_guest: bool = False):
	"""Wrap an endpoint with auth check, a uniform envelope and safe error output.

	Success: {"success": true, "data": ..., "request_id": "...", "api_version": 1}
	Failure: HTTP 4xx/5xx and {"success": false, "error": {"code", "message"}, "request_id"}
	Errors roll back the transaction, so a failed write never half-commits.
	"""

	def decorator(fn):
		@functools.wraps(fn)
		def wrapper(*args, **kwargs):
			request_id = (frappe.get_request_header("X-Request-ID") or "")[:64] if frappe.request else ""
			request_id = request_id or uuid.uuid4().hex
			frappe.flags.hm_request_id = request_id
			frappe.flags.hm_source = "Mobile"
			try:
				if not allow_guest:
					require_active_user()
				data = fn(*args, **kwargs)
			except Exception as exc:
				frappe.db.rollback()
				status, code = _classify(exc)
				if status == 500:
					frappe.log_error(title=f"Hipersign Mobile API error: {fn.__name__}")
					message = "Something went wrong on the server. Please try again."
				else:
					message = _clean(str(exc)) or code.replace("_", " ").title()
				frappe.clear_messages()
				frappe.local.response["http_status_code"] = status
				return {
					"success": False,
					"error": {"code": code, "message": message},
					"request_id": request_id,
				}
			finally:
				frappe.flags.hm_source = None
			frappe.clear_messages()
			return {"success": True, "data": data, "request_id": request_id, "api_version": API_VERSION}

		return frappe.whitelist(allow_guest=allow_guest, methods=list(methods))(wrapper)

	return decorator


def limit_per_user(action: str, limit: int, seconds: int = 60):
	"""Simple per-user rate limit (Frappe's decorator is IP/field based)."""
	key = frappe.cache.make_key(f"hipersign_mobile:rl:{action}:{frappe.session.user}")
	count = frappe.cache.incrby(key, 1)
	if count == 1:
		frappe.cache.expire(key, seconds)
	if count > limit:
		raise frappe.RateLimitExceededError("Too many requests. Please wait a moment and try again.")


def page_args(cursor=None, page_size=None) -> tuple[int, int]:
	"""Opaque-ish cursor = offset. Page size is capped by settings."""
	limit = min(max(cint(page_size) or 20, 1), hm_settings.get().max_page_size)
	start = max(cint(cursor), 0)
	return start, limit


def page_result(items: list, start: int, limit: int, fetched_extra: bool) -> dict:
	return {
		"items": items,
		"next_cursor": str(start + limit) if fetched_extra else None,
	}


def require_str(value, field: str, max_len: int = 140) -> str:
	text = str(value or "").strip()
	if not text:
		raise frappe.ValidationError(f"{field} is required.")
	if len(text) > max_len:
		raise frappe.ValidationError(f"{field} is too long (max {max_len} characters).")
	return text


def optional_str(value, max_len: int = 2000) -> str | None:
	if value is None:
		return None
	text = str(value).strip()
	if len(text) > max_len:
		raise frappe.ValidationError(f"Text is too long (max {max_len} characters).")
	return text or None


def choice(value, allowed, field: str, default=None):
	if value in (None, ""):
		if default is not None:
			return default
		raise frappe.ValidationError(f"{field} is required.")
	if value not in allowed:
		raise frappe.ValidationError(f"Invalid {field}: {value}. Allowed: {', '.join(allowed)}.")
	return value
