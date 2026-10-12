"""Find which business a phone number belongs to, using Google Places API (New) Text Search.

Only Pro-tier fields are requested (name, address, Maps link) to keep the cost per lookup
down, and every number is cached for a week so re-dialing never costs a second lookup.
The API key stays on the server (Hipersign Mobile Settings) and never reaches the phone.
"""

from __future__ import annotations

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
