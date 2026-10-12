"""End-to-end check against a running site, over real HTTP, the way the phone talks to it.

Usage:
    python scripts/e2e_http_check.py --base http://127.0.0.1:8000 --host test.local \
        --sales1 hm.sales1@example.com --sales2 hm.sales2@example.com --password '...' \
        --admin-password '...' --trainer hm.trainer@example.com --customer "HM Test Customer"

Creates its own trainer slots/activities (as Administrator) and verifies:
login, role workspace, isolation (incl. Frappe's /api/resource), create / reschedule
round-trip, POST-only enforcement, simultaneous trial bookings, and logout.
"""

from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import sys
import uuid

import requests

RESULTS: list[tuple[bool, str]] = []


def check(ok: bool, label: str, detail=""):
	RESULTS.append((bool(ok), label))
	print(("PASS " if ok else "FAIL ") + label + (f"  -> {detail}" if not ok and detail else ""))


class Client:
	def __init__(self, base, host):
		self.base, self.s = base.rstrip("/"), requests.Session()
		self.s.headers.update({"Host": host, "Accept": "application/json"})

	def login(self, usr, pwd):
		return self.s.post(f"{self.base}/api/method/login", json={"usr": usr, "pwd": pwd})

	def get(self, method, **params):
		return self.s.get(f"{self.base}/api/method/hipersign_mobile.api.{method}", params=params)

	def post(self, method, **body):
		return self.s.post(
			f"{self.base}/api/method/hipersign_mobile.api.{method}",
			json=body,
			headers={"X-Request-ID": uuid.uuid4().hex},
		)

	def resource(self, path):
		return self.s.get(f"{self.base}/api/resource/{path}")


def msg(r):
	try:
		return r.json().get("message")
	except ValueError:
		return None


def main():
	ap = argparse.ArgumentParser()
	ap.add_argument("--base", default="http://127.0.0.1:8000")
	ap.add_argument("--host", default="test.local")
	ap.add_argument("--sales1", required=True)
	ap.add_argument("--sales2", required=True)
	ap.add_argument("--trainer", required=True)
	ap.add_argument("--password", required=True)
	ap.add_argument("--admin-password", required=True)
	ap.add_argument("--customer", required=True)
	a = ap.parse_args()

	tz = dt.timezone(dt.timedelta(hours=5, minutes=30))
	run = uuid.uuid4().hex[:6]
	# A random future day per run so repeated runs never collide with earlier test data.
	day = (dt.datetime.now(tz) + dt.timedelta(days=30 + int(run, 16) % 600)).replace(
		hour=10, minute=0, second=0, microsecond=0
	)

	admin = Client(a.base, a.host)
	check(admin.login("Administrator", a.admin_password).ok, "admin login")

	s1, s2, anon = Client(a.base, a.host), Client(a.base, a.host), Client(a.base, a.host)
	bad = Client(a.base, a.host).login(a.sales1, "wrong-password")
	check(bad.status_code == 401, "wrong password is rejected (401)", bad.status_code)
	r = s1.login(a.sales1, a.password)
	check(
		r.ok and s1.s.cookies.get("sid"),
		"salesperson logs in with ERPNext credentials (sid cookie)",
		r.text[:200],
	)
	check(s2.login(a.sales2, a.password).ok, "second salesperson logs in")

	r = anon.get("auth.get_me")
	check(r.status_code in (401, 403), "anonymous get_me is refused", r.status_code)
	r = anon.get("auth.get_config")
	check(r.ok and msg(r)["success"], "public config is reachable before login")

	me = msg(s1.get("auth.get_me"))["data"]
	check("sales" in me["workspaces"], "sales workspace resolved from ERPNext roles", me)

	# --- create (POST JSON, no CSRF token needed for the API session)
	start = day.isoformat()
	r = s1.post(
		"activities.create_activity",
		title=f"E2E follow-up {run}",
		activity_type="Follow-up",
		reference_doctype="Customer",
		reference_name=a.customer,
		start=start,
		idempotency_key=f"e2e-{run}",
	)
	m = msg(r)
	check(r.ok and m["success"], "mobile-created activity saved", r.text[:300])
	act_id = m["data"]["activity"]["id"]
	r2 = s1.post(
		"activities.create_activity",
		title=f"E2E follow-up {run}",
		reference_doctype="Customer",
		reference_name=a.customer,
		start=start,
		idempotency_key=f"e2e-{run}",
	)
	check(
		msg(r2)["data"]["activity"]["id"] == act_id and not msg(r2)["data"]["created"],
		"retry with same key returns the same record",
	)

	# --- visible in ERPNext (Administrator via standard REST)
	r = admin.resource(f"CRM Task/{act_id}")
	check(
		r.ok and r.json()["data"]["assigned_to"] == a.sales1,
		"activity visible in ERPNext as a CRM Task",
		r.text[:200],
	)

	# --- isolation
	r = s2.get("activities.get_detail", activity_id=act_id)
	check(r.status_code == 404, "other salesperson cannot open it via mobile API (404)", r.status_code)
	r = s2.resource(f"CRM Task/{act_id}")
	check(
		r.status_code == 403, "other salesperson cannot read it via Frappe /api/resource (403)", r.status_code
	)
	r = s2.s.get(
		f"{a.base}/api/resource/CRM Task",
		params={"fields": '["name","assigned_to"]', "limit_page_length": 500},
	)
	others = [t for t in (r.json().get("data") or []) if t["assigned_to"] not in (a.sales2, None)]
	check(r.ok and not others, "Frappe list API only returns the salesperson's own tasks", others[:3])
	r = s2.get("calendar.get_my_day", user=a.sales1)
	check(r.status_code == 403, "cannot request another employee's calendar (403)", r.status_code)

	# --- POST-only enforcement
	r = s1.get("activities.update_status", activity_id=act_id, status="Completed")
	check(r.status_code in (403, 405), "state-changing endpoints refuse GET", r.status_code)

	# --- reschedule round trip, ERPNext-side change reflected back
	new_start = (day + dt.timedelta(hours=5)).isoformat()
	r = s1.post(
		"activities.reschedule_activity", activity_id=act_id, new_start_datetime=new_start, reason="E2E"
	)
	check(
		r.ok and msg(r)["data"]["activity"]["status"] == "Rescheduled",
		"reschedule saved and status is Rescheduled",
		r.text[:300],
	)
	r = admin.s.put(f"{a.base}/api/resource/CRM Task/{act_id}", json={"title": f"Renamed on desk {run}"})
	check(r.ok, "desk/REST edit by admin")
	d = msg(s1.get("activities.get_detail", activity_id=act_id))["data"]
	check(d["title"] == f"Renamed on desk {run}", "ERPNext-side change appears in the app")
	check(any(h["event_type"] == "Rescheduled" for h in d["history"]), "history kept for the reschedule")

	# --- simultaneous trial bookings: 8 requests from two salespeople for one trainer slot
	st = day + dt.timedelta(days=1)
	payload = {
		"trainer": a.trainer,
		"start_datetime": st.strftime("%Y-%m-%d %H:%M:%S"),
		"end_datetime": (st + dt.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"),
		"status": "Available",
		"slot_type": "Both",
	}
	r = admin.s.post(f"{a.base}/api/resource/Trainer Slot", json=payload)
	check(r.ok, "admin creates a trainer slot", r.text[:200])
	slot_ids = [r.json()["data"]["name"]] * 2

	def attempt(i):
		client = s1 if i % 2 == 0 else s2
		return client.post(
			"trials.book_trial",
			trainer_slot=slot_ids[i % 2],
			customer=a.customer,
			booking_type="ERP Trial",
			idempotency_key=f"e2e-trial-{run}-{i}",
		)

	with cf.ThreadPoolExecutor(max_workers=8) as ex:
		responses = list(ex.map(attempt, range(8)))
	ok = [r for r in responses if r.status_code == 200 and msg(r)["success"]]
	conflicts = [r for r in responses if r.status_code == 409]
	check(
		len(ok) == 1,
		f"8 simultaneous bookings for one trainer time -> exactly 1 succeeds (got {len(ok)})",
		[r.status_code for r in responses],
	)
	check(len(conflicts) == 7, "the other 7 get 409 CONFLICT", [r.status_code for r in responses])
	if ok:
		b = msg(ok[0])["data"]["booking"]
		owner = s1 if b["booked_by"] == a.sales1 else s2
		cal = msg(owner.get("calendar.get_my_day", start_date=b["start"][:10], end_date=b["start"][:10]))[
			"data"
		]
		check(
			b["activity_id"] in [x["id"] for x in cal["items"]],
			"booking appears in the salesperson's calendar",
		)

	# --- logout ends the session
	r = s1.post("auth.logout_mobile", device_id=f"e2e-{run}")
	check(r.ok, "logout")
	r = s1.get("auth.get_me")
	check(r.status_code in (401, 403), "session is gone after logout", r.status_code)

	failed = [label for ok_, label in RESULTS if not ok_]
	print(f"\n{len(RESULTS) - len(failed)}/{len(RESULTS)} checks passed")
	sys.exit(1 if failed else 0)


if __name__ == "__main__":
	main()
