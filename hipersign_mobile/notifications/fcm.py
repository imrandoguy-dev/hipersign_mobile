"""Firebase Cloud Messaging (HTTP v1) sender. Also delivers to iOS through APNs
when the Firebase project has an APNs key configured.

Credentials: put the Firebase service-account JSON on the server and point
site_config at it (never ship it in the app):

    bench --site <site> set-config hipersign_mobile_fcm_credentials /home/frappe/secrets/fcm.json
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass

import frappe
import requests

SCOPE = "https://www.googleapis.com/auth/firebase.messaging"
TOKEN_CACHE_KEY = "hipersign_mobile:fcm_access_token"
ANDROID_CHANNEL_ID = "hipersign_alerts"


@dataclass
class SendResult:
	ok: bool
	message_id: str | None = None
	error_code: str | None = None
	error_summary: str | None = None
	invalid_token: bool = False  # permanent: deactivate this device
	transient: bool = False  # retry later


class FCMConfigError(Exception):
	pass


def _credentials() -> dict:
	conf = frappe.conf.get("hipersign_mobile_fcm_credentials") or _uploaded_key()
	if not conf:
		raise FCMConfigError(
			"Upload the Firebase service account key in Hipersign Mobile Settings > Push Notifications."
		)
	if isinstance(conf, dict):
		creds = conf
	else:
		try:
			with open(conf) as fh:
				creds = json.load(fh)
		except Exception as exc:
			raise FCMConfigError(f"Cannot read FCM credentials file: {type(exc).__name__}")
	for key in ("project_id", "client_email", "private_key"):
		if not creds.get(key):
			raise FCMConfigError(f"FCM credentials are missing '{key}'.")
	return creds


def _uploaded_key() -> dict | None:
	"""The key file uploaded in Hipersign Mobile Settings (Firebase Service Account Key)."""
	url = frappe.db.get_single_value("Hipersign Mobile Settings", "fcm_key_file")
	if not url:
		return None
	name = frappe.db.get_value("File", {"file_url": url}, "name")
	if not name:
		raise FCMConfigError("The uploaded Firebase key file was not found - upload it again.")
	try:
		content = frappe.get_doc("File", name).get_content()
		if isinstance(content, bytes):
			content = content.decode("utf-8")
		return json.loads(content)
	except Exception as exc:
		raise FCMConfigError(
			f"The uploaded Firebase key is not a valid JSON key file ({type(exc).__name__})."
		)


def is_configured() -> bool:
	try:
		_credentials()
		return True
	except FCMConfigError:
		return False


def _access_token(force_refresh: bool = False) -> str:
	if not force_refresh:
		cached = frappe.cache.get_value(TOKEN_CACHE_KEY)
		if cached:
			return cached
	import jwt

	creds = _credentials()
	token_uri = creds.get("token_uri") or "https://oauth2.googleapis.com/token"
	now = int(time.time())
	assertion = jwt.encode(
		{"iss": creds["client_email"], "scope": SCOPE, "aud": token_uri, "iat": now, "exp": now + 3600},
		creds["private_key"],
		algorithm="RS256",
	)
	resp = requests.post(
		token_uri,
		data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer", "assertion": assertion},
		timeout=15,
	)
	if resp.status_code != 200:
		raise FCMConfigError(f"Google OAuth token request failed with HTTP {resp.status_code}.")
	payload = resp.json()
	token = payload["access_token"]
	ttl = max(int(payload.get("expires_in", 3600)) - 300, 60)
	frappe.cache.set_value(TOKEN_CACHE_KEY, token, expires_in_sec=ttl)
	return token


def build_message(token: str, title: str, body: str, data: dict | None) -> dict:
	return {
		"message": {
			"token": token,
			"notification": {"title": title, "body": body},
			# FCM data values must be strings.
			"data": {k: str(v) for k, v in (data or {}).items() if v is not None},
			"android": {
				"priority": "HIGH",
				"notification": {"channel_id": ANDROID_CHANNEL_ID, "default_sound": True},
			},
			"apns": {
				"headers": {"apns-priority": "10", "apns-push-type": "alert"},
				"payload": {"aps": {"sound": "default"}},
			},
		}
	}


def send(token: str, title: str, body: str, data: dict | None = None) -> SendResult:
	try:
		creds = _credentials()
		access = _access_token()
	except FCMConfigError as exc:
		return SendResult(ok=False, error_code="NOT_CONFIGURED", error_summary=str(exc))
	except requests.RequestException as exc:
		return SendResult(
			ok=False, error_code="OAUTH_NETWORK", error_summary=type(exc).__name__, transient=True
		)

	url = f"https://fcm.googleapis.com/v1/projects/{creds['project_id']}/messages:send"
	message = build_message(token, title, body, data)
	for attempt in (1, 2):
		try:
			resp = requests.post(
				url,
				json=message,
				headers={"Authorization": f"Bearer {access}", "Content-Type": "application/json"},
				timeout=15,
			)
		except requests.RequestException as exc:
			return SendResult(
				ok=False, error_code="NETWORK", error_summary=type(exc).__name__, transient=True
			)

		if resp.status_code == 200:
			return SendResult(ok=True, message_id=resp.json().get("name"))
		if resp.status_code == 401 and attempt == 1:
			access = _access_token(force_refresh=True)
			continue
		return classify_error(resp.status_code, _safe_json(resp))
	return SendResult(ok=False, error_code="UNAUTHENTICATED", error_summary="FCM rejected the access token.")


def _safe_json(resp) -> dict:
	try:
		return resp.json()
	except ValueError:
		return {}


def classify_error(status: int, payload: dict) -> SendResult:
	err = (payload or {}).get("error") or {}
	fcm_code = None
	for detail in err.get("details") or []:
		if detail.get("errorCode"):
			fcm_code = detail["errorCode"]
	code = fcm_code or err.get("status") or f"HTTP_{status}"
	# Never store the raw provider message: it can echo the device token.
	summary = f"FCM HTTP {status}: {code}"

	if code in ("UNREGISTERED", "SENDER_ID_MISMATCH") or status == 404:
		return SendResult(ok=False, error_code=code, error_summary=summary, invalid_token=True)
	if code == "INVALID_ARGUMENT" and "token" in str(err.get("message", "")).lower():
		return SendResult(ok=False, error_code="INVALID_TOKEN", error_summary=summary, invalid_token=True)
	if status in (429, 500, 502, 503, 504) or code in ("UNAVAILABLE", "INTERNAL", "QUOTA_EXCEEDED"):
		return SendResult(ok=False, error_code=code, error_summary=summary, transient=True)
	return SendResult(ok=False, error_code=code, error_summary=summary)
