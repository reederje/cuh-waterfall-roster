import logging
import os
from datetime import datetime, timedelta

import requests
from flask import Flask, jsonify, render_template

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SHIFTADMIN_BASE_URL = "https://www.shiftadmin.com/vutsw"

# Window (in hours) within which an upcoming shift start is shown as "arriving soon".
ARRIVING_SOON_WINDOW_HOURS = 1


def _credentials():
    return os.environ.get("SHIFTADMIN_USER", ""), os.environ.get(
        "SHIFTADMIN_PASSWORD", ""
    )


def _post(endpoint, extra_params=None):
    user, password = _credentials()
    payload = {"user": user, "password": password}
    if extra_params:
        payload.update(extra_params)
    try:
        resp = requests.post(
            f"{SHIFTADMIN_BASE_URL}/{endpoint}", data=payload, timeout=15
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        logger.error("ShiftAdmin API error (%s): %s", endpoint, exc)
        return None


def _parse_dt(value):
    """Try several common datetime formats returned by ShiftAdmin."""
    if not value:
        return None
    for fmt in (
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M",
    ):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    # Fall back to fromisoformat (Python 3.7+)
    try:
        return datetime.fromisoformat(value)
    except ValueError:
        return None


def _provider_name(record):
    """Extract a display name from a shift or user record."""
    for key in ("provider_name", "user_name"):
        if record.get(key):
            return record[key]
    first = record.get("first_name", "")
    last = record.get("last_name", "")
    full = f"{first} {last}".strip()
    return full or "Unknown"


def _is_physician(shift, physician_ids):
    """
    Return True if the provider on this shift should be displayed.
    Priority: cross-reference with physician_ids set from org_users.
    Fallback: check the type/user_type field on the shift itself.
    """
    if physician_ids:
        pid = str(shift.get("user_id", shift.get("provider_id", "")))
        return pid in physician_ids

    ptype = shift.get("user_type", shift.get("provider_type", "")).lower()
    # If the API omits the type field entirely, include everyone.
    if not ptype:
        return True
    return ptype in ("physician", "attending", "doctor", "md", "do")


def build_roster():
    now = datetime.now()
    today = now.strftime("%Y-%m-%d")

    # --- 1. Fetch provider types from org_users ---
    physician_ids = set()
    users_data = _post("org_users")
    if users_data:
        for u in users_data.get("users", users_data.get("org_users", [])):
            utype = u.get("user_type", u.get("type", "")).lower()
            if utype in ("physician", "attending", "doctor", "md", "do"):
                uid = str(u.get("user_id", u.get("id", "")))
                if uid:
                    physician_ids.add(uid)

    # --- 2. Fetch scheduled shifts for today ---
    shifts_data = _post(
        "org_scheduled_shifts",
        {"date_start": today, "date_end": today, "date": today},
    )

    if shifts_data is None:
        return {
            "areas": [],
            "last_updated": now.isoformat(),
            "error": "Unable to reach ShiftAdmin API.",
        }

    raw_shifts = shifts_data.get(
        "scheduled_shifts",
        shifts_data.get("shifts", shifts_data.get("org_scheduled_shifts", [])),
    )

    areas = {}

    for shift in raw_shifts:
        if not _is_physician(shift, physician_ids):
            continue

        shift_name = shift.get(
            "shift_name", shift.get("name", shift.get("shift", "Unknown"))
        )

        start_dt = _parse_dt(
            shift.get("start_datetime", shift.get("start_time", ""))
        )
        end_dt = _parse_dt(
            shift.get("end_datetime", shift.get("end_time", ""))
        )

        if start_dt is None or end_dt is None:
            logger.warning("Skipping shift with unparseable times: %s", shift)
            continue

        name = _provider_name(shift)

        # Only place this physician in an area if the shift is active or
        # starting within the next hour; otherwise skip entirely.
        if start_dt <= now <= end_dt:
            if shift_name not in areas:
                areas[shift_name] = {
                    "name": shift_name,
                    "is_supertrack": "ST" in shift_name,
                    "current_physicians": [],
                    "arriving_soon": [],
                }
            areas[shift_name]["current_physicians"].append({
                "name": name,
                "shift_start": start_dt.strftime("%H:%M"),
                "shift_end": end_dt.strftime("%H:%M"),
            })
        elif now < start_dt <= now + timedelta(hours=ARRIVING_SOON_WINDOW_HOURS):
            if shift_name not in areas:
                areas[shift_name] = {
                    "name": shift_name,
                    "is_supertrack": "ST" in shift_name,
                    "current_physicians": [],
                    "arriving_soon": [],
                }
            areas[shift_name]["arriving_soon"].append({
                "name": name,
                "shift_start": start_dt.strftime("%H:%M"),
                "shift_end": end_dt.strftime("%H:%M"),
                "minutes_until": int((start_dt - now).total_seconds() / 60),
            })

    # Sort areas: SuperTrack first, then alphabetically
    sorted_areas = sorted(
        areas.values(),
        key=lambda a: (not a["is_supertrack"], a["name"]),
    )

    return {"areas": sorted_areas, "last_updated": now.isoformat()}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/roster")
def api_roster():
    return jsonify(build_roster())


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug)
