import base64
import logging
import os
from datetime import datetime, timedelta
from functools import wraps

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

load_dotenv()

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SHIFTADMIN_BASE_URL = "https://www.shiftadmin.com/vutsw"

# Window (in hours) within which an upcoming shift start is shown as "arriving soon".
ARRIVING_SOON_WINDOW_HOURS = 1
TARGET_FACILITY_ID = 10

# Most recent ShiftAdmin request error details, used to provide diagnostics
# in API responses.
LAST_SHIFTADMIN_ERROR = None

# Roster app authentication
AUTH_USERNAME = "cuhed"
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "")


def _check_auth(username, password):
    """Verify HTTP Basic Auth credentials."""
    return username == AUTH_USERNAME and password == AUTH_PASSWORD


def _require_auth(f):
    """Decorator to protect routes with HTTP Basic Auth."""
    @wraps(f)
    def decorated_function(*args, **kwargs):
        auth = request.authorization
        if not auth or not _check_auth(auth.username, auth.password):
            return (
                jsonify({"error": "Unauthorized"}),
                401,
                {"WWW-Authenticate": 'Basic realm="Roster Access"'},
            )
        return f(*args, **kwargs)

    return decorated_function


def _credentials():
    return os.environ.get("SHIFTADMIN_USER", ""), os.environ.get(
        "SHIFTADMIN_PASSWORD", ""
    )


def _set_last_shiftadmin_error(endpoint, err_type, message, status_code=None):
    global LAST_SHIFTADMIN_ERROR
    LAST_SHIFTADMIN_ERROR = {
        "endpoint": endpoint,
        "type": err_type,
        "message": message,
        "status_code": status_code,
    }


def _post(endpoint, extra_params=None):
    global LAST_SHIFTADMIN_ERROR
    LAST_SHIFTADMIN_ERROR = None

    user, password = _credentials()
    payload = {}
    if extra_params:
        payload.update(extra_params)

    try:
        resp = requests.post(
            f"{SHIFTADMIN_BASE_URL}/{endpoint}", auth=(user, password), json=payload, timeout=15
        )
        resp.raise_for_status()
        return resp.json()
    except requests.exceptions.Timeout as exc:
        _set_last_shiftadmin_error(
            endpoint,
            "timeout",
            "Request to ShiftAdmin timed out.",
        )
        logger.error("ShiftAdmin timeout (%s): %s", endpoint, exc)
        return None
    except requests.exceptions.ConnectionError as exc:
        _set_last_shiftadmin_error(
            endpoint,
            "network",
            "Could not connect to ShiftAdmin.",
        )
        logger.error("ShiftAdmin connection error (%s): %s", endpoint, exc)
        return None
    except requests.exceptions.HTTPError as exc:
        status_code = exc.response.status_code if exc.response is not None else None
        response_text = (exc.response.text[:200] if exc.response is not None else "")
        if status_code in (401, 403):
            _set_last_shiftadmin_error(
                endpoint,
                "auth",
                "ShiftAdmin rejected credentials (401/403).",
                status_code=status_code,
            )
        else:
            _set_last_shiftadmin_error(
                endpoint,
                "http",
                f"ShiftAdmin returned HTTP {status_code}.",
                status_code=status_code,
            )
        logger.error(
            "ShiftAdmin HTTP error (%s): status=%s body=%s",
            endpoint,
            status_code,
            response_text,
        )
        return None
    except requests.exceptions.RequestException as exc:
        _set_last_shiftadmin_error(
            endpoint,
            "request",
            "Unexpected request error while calling ShiftAdmin.",
        )
        logger.error("ShiftAdmin request error (%s): %s", endpoint, exc)
        return None
    except ValueError as exc:
        _set_last_shiftadmin_error(
            endpoint,
            "response_parse",
            "ShiftAdmin response was not valid JSON.",
        )
        logger.error("ShiftAdmin JSON parse error (%s): %s", endpoint, exc)
        return None
    except Exception as exc:
        _set_last_shiftadmin_error(
            endpoint,
            "unknown",
            "Unexpected error while calling ShiftAdmin.",
        )
        logger.error("ShiftAdmin API error (%s): %s", endpoint, exc)
        return None


def _parse_dt(value):
    """Try several common datetime formats returned by ShiftAdmin."""
    if not value:
        return None
    for fmt in (
        "%m/%d/%Y %H:%M",
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


def _is_supertrack_shift(shift_name):
    """Return True when a shift should be grouped under the Supertrack card."""
    if not shift_name:
        return False
    lower_name = shift_name.lower()
    if "supertrack" in lower_name:
        return True
    normalized = lower_name.replace("-", " ").replace("/", " ")
    return "st" in normalized.split()


def build_roster():
    now = datetime.now()
    start_date = now.date().isoformat()
    end_date = start_date

    # --- 1. Fetch provider types from org_users ---
    physician_ids = set()
    users_data = _post("org_users")
    users = []
    if isinstance(users_data, dict):
        users = users_data.get("users", users_data.get("org_users", []))
    elif isinstance(users_data, list):
        users = users_data

    if users:
        for u in users:
            if not isinstance(u, dict):
                continue
            utype = u.get("user_type", u.get("type", "")).lower()
            if utype in ("physician", "attending", "doctor", "md", "do"):
                uid = str(u.get("user_id", u.get("id", "")))
                if uid:
                    physician_ids.add(uid)

    # --- 2. Fetch scheduled shifts for today ---
    shifts_data = _post(
        "org_scheduled_shifts",
        {"type": "json", "start_date": start_date, "end_date": end_date},
    )

    if shifts_data is None:
        error_info = LAST_SHIFTADMIN_ERROR or {}
        return {
            "areas": [],
            "last_updated": now.isoformat(),
            "error": "Unable to reach ShiftAdmin API.",
            "error_type": error_info.get("type", "unknown"),
            "error_detail": error_info.get(
                "message", "No additional ShiftAdmin error details available."
            ),
        }

    raw_shifts = []
    if isinstance(shifts_data, dict):
        raw_shifts = shifts_data.get(
            "scheduled_shifts",
            shifts_data.get("shifts", shifts_data.get("org_scheduled_shifts", [])),
        )
    elif isinstance(shifts_data, list):
        raw_shifts = shifts_data

    areas = {}

    for shift in raw_shifts:
        if not isinstance(shift, dict):
            continue
        if str(shift.get("facility_id", "")) != str(TARGET_FACILITY_ID):
            continue
        if not _is_physician(shift, physician_ids):
            continue

        shift_name = shift.get(
            "shift_name", shift.get("name", shift.get("shift", "Unknown"))
        )
        is_supertrack = _is_supertrack_shift(shift_name)
        area_key = "__supertrack__" if is_supertrack else shift_name
        area_name = "Supertrack" if is_supertrack else shift_name

        start_dt = _parse_dt(
            shift.get("start_datetime")
            or shift.get("start_time")
            or shift.get("shift_start")
            or shift.get("published_shift_start", "")
        )
        end_dt = _parse_dt(
            shift.get("end_datetime")
            or shift.get("end_time")
            or shift.get("shift_end")
            or shift.get("published_shift_end", "")
        )

        if start_dt is None or end_dt is None:
            logger.warning("Skipping shift with unparseable times: %s", shift)
            continue

        name = _provider_name(shift)

        # Only place this physician in an area if the shift is active or
        # starting within the next hour; otherwise skip entirely.
        if start_dt <= now <= end_dt:
            if area_key not in areas:
                areas[area_key] = {
                    "name": area_name,
                    "is_supertrack": is_supertrack,
                    "current_physicians": [],
                    "arriving_soon": [],
                }
            areas[area_key]["current_physicians"].append({
                "name": name,
                "shift_start": start_dt.strftime("%H:%M"),
                "shift_end": end_dt.strftime("%H:%M"),
            })
        elif now < start_dt <= now + timedelta(hours=ARRIVING_SOON_WINDOW_HOURS):
            if area_key not in areas:
                areas[area_key] = {
                    "name": area_name,
                    "is_supertrack": is_supertrack,
                    "current_physicians": [],
                    "arriving_soon": [],
                }
            areas[area_key]["arriving_soon"].append({
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
@_require_auth
def index():
    return render_template("index.html")


@app.route("/api/roster")
@_require_auth
def api_roster():
    return jsonify(build_roster())


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    app.run(debug=debug)
