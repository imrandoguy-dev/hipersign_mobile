"""In-app updates for the Android pilot build.

The CI build publishes a GitHub release (tag `pilot` by default) with two assets:
`Hipersign.apk` and `version.json` ({"version_code": 123, "version_name": "0.1.123", ...}).
The repo is private, so phones never talk to GitHub directly: this endpoint reads the
release with a server-side token and hands the app a short-lived signed download URL.

Configure in ERPNext: Hipersign Mobile Settings > App Updates (tick "Enable In-App
Updates" and paste a fine-grained GitHub token with read-only "Contents" on the app repo).
site_config keys hipersign_mobile_github_token / _release_repo / _release_tag also work.
"""

from __future__ import annotations

import frappe
import requests
from frappe.utils import cint

from hipersign_mobile.api._common import mobile_api

CACHE_KEY = "hipersign_mobile:latest_release"
CACHE_SECONDS = 600
GITHUB_API = "https://api.github.com"


class UpdateConfigError(Exception):
	pass


def _conf():
	"""Hipersign Mobile Settings > App Updates (preferred), else site_config."""
	settings = frappe.get_cached_doc("Hipersign Mobile Settings")
	token = None
	if settings.get("enable_app_updates"):
		token = settings.get_password("github_token", raise_exception=False)
	token = token or frappe.conf.get("hipersign_mobile_github_token")
	if not token:
		raise UpdateConfigError("In-app updates are not enabled on this server.")
	repo = (
		settings.get("release_repo")
		or frappe.conf.get("hipersign_mobile_release_repo")
		or "imrandoguy-dev/hipersign_mobile_app"
	).strip()
	tag = (settings.get("release_tag") or frappe.conf.get("hipersign_mobile_release_tag") or "pilot").strip()
	return token, repo, tag


def _headers(token, accept="application/vnd.github+json"):
	return {"Authorization": f"Bearer {token}", "Accept": accept, "X-GitHub-Api-Version": "2022-11-28"}


def _latest_release(force: bool = False) -> dict:
	"""{"version_code", "version_name", "notes", "apk_asset_url", "size"} — cached 10 min."""
	if not force:
		cached = frappe.cache.get_value(CACHE_KEY)
		if cached:
			return cached
	token, repo, tag = _conf()
	r = requests.get(f"{GITHUB_API}/repos/{repo}/releases/tags/{tag}", headers=_headers(token), timeout=15)
	if r.status_code != 200:
		raise UpdateConfigError(f"Could not read the release from GitHub (HTTP {r.status_code}).")
	release = r.json()
	assets = {a["name"]: a for a in release.get("assets", [])}
	apk, meta = assets.get("Hipersign.apk"), assets.get("version.json")
	if not apk or not meta:
		raise UpdateConfigError("The latest release has no Hipersign.apk / version.json.")
	m = requests.get(meta["url"], headers=_headers(token, "application/octet-stream"), timeout=15)
	if m.status_code != 200:
		raise UpdateConfigError("Could not read version.json from the release.")
	info = m.json()
	out = {
		"version_code": cint(info.get("version_code")),
		"version_name": str(info.get("version_name") or ""),
		"notes": str(info.get("notes") or release.get("body") or "")[:2000],
		"mandatory": bool(info.get("mandatory")),
		"min_version_code": cint(info.get("min_version_code")),
		"apk_asset_url": apk["url"],
		"size": cint(apk.get("size")),
	}
	frappe.cache.set_value(CACHE_KEY, out, expires_in_sec=CACHE_SECONDS)
	return out


@mobile_api(methods=("GET",))
def check(version_code: str | int | None = None, platform: str | None = None):
	"""Is there a newer Android build than `version_code`? Never errors for the user."""
	if (platform or "Android") != "Android":
		return {"available": False, "reason": "Updates on iOS come through TestFlight / the App Store."}
	try:
		latest = _latest_release()
	except (UpdateConfigError, requests.RequestException) as exc:
		return {
			"available": False,
			"reason": str(exc) if isinstance(exc, UpdateConfigError) else "GitHub unreachable.",
		}
	current = cint(version_code)
	available = latest["version_code"] > current
	return {
		"available": available,
		"version_code": latest["version_code"],
		"version_name": latest["version_name"],
		"notes": latest["notes"],
		"size": latest["size"],
		"mandatory": available and (latest["mandatory"] or current < latest["min_version_code"]),
	}


@mobile_api(methods=("GET",))
def get_download_url():
	"""A short-lived (a few minutes) direct download URL for the latest APK."""
	try:
		token, _repo, _tag = _conf()
		latest = _latest_release()
		r = requests.get(
			latest["apk_asset_url"],
			headers=_headers(token, "application/octet-stream"),
			allow_redirects=False,
			timeout=15,
		)
	except UpdateConfigError as exc:
		raise frappe.ValidationError(str(exc))
	except requests.RequestException:
		raise frappe.ValidationError("Could not reach GitHub. Try again in a minute.")
	location = r.headers.get("Location")
	if r.status_code not in (301, 302, 303, 307) or not location:
		frappe.cache.delete_value(CACHE_KEY)  # asset may have been replaced by a newer build
		raise frappe.ValidationError("The download link is not available right now. Try again.")
	return {"url": location, "version_code": latest["version_code"], "size": latest["size"]}
