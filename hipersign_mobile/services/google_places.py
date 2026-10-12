"""Find which business a phone number belongs to, using Google Places API (New) Text Search.

Only Pro-tier fields are requested (name, address, Maps link) to keep the cost per lookup
down, and every number is cached for a week so re-dialing never costs a second lookup.
The API key stays on the server (Hipersign Mobile Settings) and never reaches the phone.
"""

from __future__ import annotations

import hashlib
import re
from urllib.parse import parse_qs, unquote_plus, urlparse

import frappe
import requests

from hipersign_mobile.services import settings as hm_settings

ENDPOINT = "https://places.googleapis.com/v1/places:searchText"
FIELDS = "places.id,places.displayName,places.formattedAddress,places.googleMapsUri"
CACHE_SECONDS = 7 * 24 * 3600

# Country calling code -> CLDR region (Google wants both for phone searches).
REGIONS = {
	"91": "IN",
	"971": "AE",
	"966": "SA",
	"974": "QA",
	"968": "OM",
	"973": "BH",
	"965": "KW",
	"94": "LK",
	"880": "BD",
	"977": "NP",
	"92": "PK",
	"65": "SG",
	"60": "MY",
	"44": "GB",
	"1": "US",
}


class GoogleLookupError(Exception):
	pass


def enabled() -> bool:
	return bool(hm_settings.get().enable_google_lookup)


def _api_key() -> str:
	key = frappe.get_cached_doc("Hipersign Mobile Settings").get_password(
		"google_maps_api_key", raise_exception=False
	)
	if not key:
		raise GoogleLookupError("Google lookup is on, but no Google Maps API key is saved in Hipersign Mobile Settings.")
	return key


def _split(e164: str) -> tuple[str, str]:
	"""+919495771084 -> ("91", "9495771084")"""
	digits = e164.lstrip("+")
	for length in (3, 2, 1):
		if digits[:length] in REGIONS:
			return digits[:length], digits[length:]
	return "", digits


def lookup(e164: str) -> dict | None:
	"""{"place_id", "name", "address", "maps_url"} for the business with this number, or None."""
	if not enabled() or not e164:
		return None
	cache_key = f"hipersign_mobile:gplace:{e164}"
	cached = frappe.cache.get_value(cache_key)
	if cached is not None:
		return cached or None  # {} = "Google has nothing for this number"

	cc, national = _split(e164)
	body = {"textQuery": f"+{cc} {national}" if cc else e164}
	if cc in REGIONS:
		body["regionCode"] = REGIONS[cc]
	try:
		r = requests.post(
			ENDPOINT,
			json=body,
			headers={"X-Goog-Api-Key": _api_key(), "X-Goog-FieldMask": FIELDS},
			timeout=10,
		)
	except requests.RequestException:
		raise GoogleLookupError("Google could not be reached. Try again.")
	if r.status_code != 200:
		try:
			message = r.json().get("error", {}).get("message") or ""
		except ValueError:
			message = ""
		frappe.log_error(title="Hipersign Mobile: Google lookup failed", message=f"HTTP {r.status_code}: {message}")
		raise GoogleLookupError(f"Google lookup failed (HTTP {r.status_code}). {message[:200]}".strip())

	places = (r.json() or {}).get("places") or []
	result = {}
	if places:
		p = places[0]
		name = ((p.get("displayName") or {}).get("text") or "").strip()
		if name:
			result = {
				"place_id": p.get("id"),
				"name": name[:140],
				"address": (p.get("formattedAddress") or "")[:300] or None,
				"maps_url": p.get("googleMapsUri"),
			}
	frappe.cache.set_value(cache_key, result, expires_in_sec=CACHE_SECONDS)
	return result or None


# ---------------------------------------------------------------- shared from Google (Search / Maps)

# Shared places need the phone number too: that field is Google's "Enterprise" level.
PLACE_FIELDS = FIELDS + ",places.internationalPhoneNumber,places.nationalPhoneNumber"
URL_RE = re.compile(r"https?://\S+")
_GOOGLE_HOSTS = ("google.", "goo.gl", "g.co", "share.google")


def _name_from_url(url: str) -> str | None:
	"""Business name inside a Google URL: /maps/place/<name>/..., ?q=<name>, or a consent ?continue= wrapper."""
	try:
		u = urlparse(url)
	except ValueError:
		return None
	qs = parse_qs(u.query)
	if "continue" in qs:
		return _name_from_url(qs["continue"][0])
	m = re.search(r"/maps/place/([^/@]+)", u.path)
	if m:
		return unquote_plus(m.group(1)).strip() or None
	# google.com/share.google?q=<code>: the q here is the short-link code, not a name.
	if u.path.rstrip("/").endswith("share.google") or "share.google" in (u.netloc or ""):
		return None
	for key in ("q", "query"):
		if qs.get(key):
			value = qs[key][0].strip()
			# "?q=10.7,76.6" is a coordinate, not a name
			if not value or re.fullmatch(r"[-\d.,\s]+", value):
				continue
			# A random-looking token (letters and digits, no spaces) is a link code, not a name.
			if " " not in value and len(value) >= 10 and re.search(r"\d", value) and re.search(r"[a-zA-Z]", value):
				continue
			return value
	return None


def _follow(url: str, hops: int = 5) -> str | None:
	"""Follow short links (maps.app.goo.gl, g.co/kgs, share.google) until a name appears."""
	for _ in range(hops):
		name = _name_from_url(url)
		if name:
			return name
		if not any(h in (urlparse(url).netloc or "") for h in _GOOGLE_HOSTS):
			return None
		try:
			r = requests.get(
				url,
				allow_redirects=False,
				timeout=8,
				headers={"User-Agent": "Mozilla/5.0 (Linux; Android 14) Hipersign"},
			)
		except requests.RequestException:
			return None
		location = r.headers.get("Location")
		if not location:
			return None
		url = requests.compat.urljoin(url, location)
	return _name_from_url(url)


def query_from_shared(text: str) -> str | None:
	"""What to search on Google for a shared text: the name/address lines, else the name in the link."""
	text = (text or "").strip()[:2000]
	lines = [ln.strip() for ln in URL_RE.sub("", text).splitlines()]
	words = " ".join(ln for ln in lines if ln)
	if words:
		return words[:300]
	for url in URL_RE.findall(text):
		name = _follow(url)
		if name:
			return name[:300]
	return None


def place_with_phone(query: str) -> dict | None:
	"""{"place_id", "name", "address", "maps_url", "phone"} for a business search, or None."""
	if not enabled() or not query:
		return None
	cache_key = "hipersign_mobile:gshare:" + hashlib.sha1(query.lower().encode()).hexdigest()
	cached = frappe.cache.get_value(cache_key)
	if cached is not None:
		return cached or None
	cc = hm_settings.get().default_country_code
	body = {"textQuery": query}
	if cc in REGIONS:
		body["regionCode"] = REGIONS[cc]
	try:
		r = requests.post(
			ENDPOINT,
			json=body,
			headers={"X-Goog-Api-Key": _api_key(), "X-Goog-FieldMask": PLACE_FIELDS},
			timeout=10,
		)
	except requests.RequestException:
		raise GoogleLookupError("Google could not be reached. Try again.")
	if r.status_code != 200:
		try:
			message = r.json().get("error", {}).get("message") or ""
		except ValueError:
			message = ""
		frappe.log_error(title="Hipersign Mobile: Google place search failed", message=f"HTTP {r.status_code}: {message}")
		raise GoogleLookupError(f"Google lookup failed (HTTP {r.status_code}). {message[:200]}".strip())
	places = (r.json() or {}).get("places") or []
	result = {}
	if places:
		p = places[0]
		name = ((p.get("displayName") or {}).get("text") or "").strip()
		if name:
			result = {
				"place_id": p.get("id"),
				"name": name[:140],
				"address": (p.get("formattedAddress") or "")[:300] or None,
				"maps_url": p.get("googleMapsUri"),
				"phone": p.get("internationalPhoneNumber") or p.get("nationalPhoneNumber"),
			}
	frappe.cache.set_value(cache_key, result, expires_in_sec=CACHE_SECONDS)
	return result or None
