"""Phone number normalisation for tel: and WhatsApp links.

Rules (deliberately conservative; we never guess a foreign country):
- "+..." or "00..." already carries a country code.
- A number with a trunk "0" (e.g. 09087652245 in India, 0501234567 in the UAE)
  is local: drop the 0 and add the site's default country code.
- A bare 10-digit number (Indian mobile, e.g. 9087652245) gets the default code.
- A bare 9-digit number gets the default code only when the site default is 971
  (UAE mobiles written without the trunk 0, e.g. 501234567).
- Longer bare numbers (11-15 digits) are taken as already international
  (e.g. 919087652245, 971501234567).
- Anything else is rejected rather than guessed.
"""

from __future__ import annotations

import re

E164_MIN, E164_MAX = 8, 15


def normalize_phone(raw: str | None, default_country_code: str = "91") -> str | None:
	"""Return an E.164 number ("+919087652245") or None when the input is not usable."""
	if not raw:
		return None
	text = str(raw).strip()
	if not text:
		return None

	text = re.split(r"(?i)\s*(?:x|ext\.?)\s*\d+$", text)[0]  # drop extensions
	has_plus = text.startswith("+")
	digits = re.sub(r"\D", "", text)
	cc = re.sub(r"\D", "", default_country_code or "91") or "91"
	if not digits:
		return None

	if has_plus:
		candidate = digits
	elif digits.startswith("00"):
		candidate = digits[2:]
	elif digits.startswith("0"):
		national = digits[1:]
		candidate = cc + national if 7 <= len(national) <= 10 else None
	elif len(digits) == 10:
		candidate = cc + digits
	elif len(digits) == 9 and cc == "971":
		candidate = cc + digits
	elif len(digits) > 10:
		candidate = digits
	else:
		candidate = None

	if not candidate or not (E164_MIN <= len(candidate) <= E164_MAX) or candidate.startswith("0"):
		return None
	return "+" + candidate


def digits_only(raw: str | None) -> str:
	return re.sub(r"\D", "", str(raw or ""))


def whatsapp_url(e164: str | None) -> str | None:
	return "https://wa.me/" + e164.lstrip("+") if e164 else None


def tel_url(e164: str | None) -> str | None:
	return "tel:" + e164 if e164 else None


def contact_payload(
	raw: str | None, default_country_code: str = "91", label: str | None = None
) -> dict | None:
	"""Shape used by the mobile app for every phone it can call / WhatsApp."""
	if not raw or not str(raw).strip():
		return None
	e164 = normalize_phone(raw, default_country_code)
	return {
		"label": label,
		"raw": str(raw).strip(),
		"e164": e164,
		"valid": bool(e164),
		"tel_url": tel_url(e164),
		"whatsapp_url": whatsapp_url(e164),
	}
