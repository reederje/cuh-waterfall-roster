import base64
import logging
import os
import sqlite3
import threading
from datetime import datetime, timedelta
from functools import wraps

import requests
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request
from flask_socketio import SocketIO, emit

load_dotenv()

app = Flask(__name__)

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

SHIFTADMIN_BASE_URL = "https://www.shiftadmin.com/vutsw"

# Window (in hours) within which an upcoming shift start is shown as "arriving soon".
ARRIVING_SOON_WINDOW_HOURS = 1
MAX_SHIFT_HOURS = 12
SUPERTRACK_ACTIVE_WINDOW_HOURS = 6
TARGET_FACILITY_ID = 10
COLOR_AREAS = ("Grey", "Blue", "Purple", "Orange")
STATE_DB_PATH = os.environ.get("STATE_DB_PATH", os.path.join("state", "supertrack_state.db"))

# Most recent ShiftAdmin request error details, used to provide diagnostics
# in API responses.
LAST_SHIFTADMIN_ERROR = None

# Roster app authentication
AUTH_USERNAME = "cuhed"
AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "")
_STATE_DB_LOCK = threading.Lock()

socketio = SocketIO(
    app,
    cors_allowed_origins="*",
    async_mode="threading",
)


def _ensure_state_db():
    db_dir = os.path.dirname(STATE_DB_PATH)
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)

    with sqlite3.connect(STATE_DB_PATH, timeout=5) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS supertrack_state (
                date_key TEXT NOT NULL,
                area_name TEXT NOT NULL,
                phys_key TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (date_key, area_name)
            )
            """
        )
        conn.commit()


def _state_db_conn():
    return sqlite3.connect(STATE_DB_PATH, timeout=5)


_ensure_state_db()


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


def _is_supertrack_shift(shift_name):
    """Return True when a shift should be grouped under the Supertrack card."""
    if not shift_name:
        return False
    lower_name = shift_name.lower()
    if "supert" in lower_name:
        return True
    normalized = lower_name.replace("-", " ").replace("/", " ")
    return "st" in normalized.split()


def _color_area_name(shift_name):
    """Return canonical color area name when the shift contains one."""
    if not shift_name:
        return None

    tokens = []
    current = []
    for ch in shift_name.lower():
        if "a" <= ch <= "z":
            current.append(ch)
        elif current:
            tokens.append("".join(current))
            current = []
    if current:
        tokens.append("".join(current))

    if any(token in tokens for token in ("gray", "ngray", "grey", "ngrey")):
        return "Gray"

    for color in COLOR_AREAS:
        color_lower = color.lower()
        if color_lower in tokens or f"n{color_lower}" in tokens:
            return color

    return None


def _physician_key(record):
    return f"{record.get('name', '')}|{record.get('shift_start', '')}|{record.get('shift_end', '')}"


def _get_supertrack_indicator_key(date_key, area_name):
    try:
        with _state_db_conn() as conn:
            row = conn.execute(
                """
                SELECT phys_key
                FROM supertrack_state
                WHERE date_key = ? AND area_name = ?
                """,
                (date_key, area_name),
            ).fetchone()
        return row[0] if row else None
    except Exception as exc:
        logger.warning("State DB read failed for supertrack state: %s", exc)
        return None


def _set_supertrack_indicator_key(date_key, area_name, phys_key):
    try:
        with _STATE_DB_LOCK:
            with _state_db_conn() as conn:
                conn.execute(
                    """
                    INSERT INTO supertrack_state (date_key, area_name, phys_key, updated_at)
                    VALUES (?, ?, ?, ?)
                    ON CONFLICT(date_key, area_name)
                    DO UPDATE SET phys_key = excluded.phys_key, updated_at = excluded.updated_at
                    """,
                    (date_key, area_name, phys_key, datetime.now().isoformat()),
                )
                conn.commit()
    except Exception as exc:
        logger.warning("State DB write failed for supertrack state: %s", exc)


def _clear_supertrack_indicator_key(date_key, area_name):
    try:
        with _STATE_DB_LOCK:
            with _state_db_conn() as conn:
                conn.execute(
                    """
                    DELETE FROM supertrack_state
                    WHERE date_key = ? AND area_name = ?
                    """,
                    (date_key, area_name),
                )
                conn.commit()
    except Exception as exc:
        logger.warning("State DB delete failed for supertrack state: %s", exc)


def _hydrate_supertrack_state(areas, date_key):
    supertrack_state = {}

    for area in areas:
        if not area.get("is_supertrack"):
            continue

        area_name = area.get("name", "")
        keys = [_physician_key(p) for p in area.get("current_physicians", [])]

        if not keys:
            _clear_supertrack_indicator_key(date_key, area_name)
            continue

        current_key = _get_supertrack_indicator_key(date_key, area_name)
        if not current_key or current_key not in keys:
            current_key = keys[0]
            _set_supertrack_indicator_key(date_key, area_name, current_key)

        supertrack_state[area_name] = current_key

    return supertrack_state


def build_roster():
    now = datetime.now()
    query_start_dt = now - timedelta(hours=MAX_SHIFT_HOURS)
    query_end_dt = now + timedelta(hours=ARRIVING_SOON_WINDOW_HOURS)
    start_date = query_start_dt.date().isoformat()
    end_date = query_end_dt.date().isoformat()
    state_date_key = now.date().isoformat()

    # --- Fetch scheduled shifts for today ---
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
        if shift.get("group_id") != 1:
            continue

        shift_name = shift.get(
            "shift_name", shift.get("name", shift.get("shift", "Unknown"))
        )
        is_supertrack = _is_supertrack_shift(shift_name)
        color_area = _color_area_name(shift_name)

        if is_supertrack:
            area_key = "__supertrack__"
            area_name = "Supertrack"
        elif color_area:
            area_key = f"__color__{color_area.lower()}"
            area_name = color_area
        else:
            area_key = shift_name
            area_name = shift_name

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
            if is_supertrack and now >= start_dt + timedelta(hours=SUPERTRACK_ACTIVE_WINDOW_HOURS):
                continue
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

    supertrack_state = _hydrate_supertrack_state(sorted_areas, state_date_key)
    return {
        "areas": sorted_areas,
        "last_updated": now.isoformat(),
        "supertrack_state": supertrack_state,
    }


@app.route("/")
@_require_auth
def index():
    return render_template("index.html")


@app.route("/api/roster")
@_require_auth
def api_roster():
    return jsonify(build_roster())


@socketio.on("advance_supertrack")
def handle_advance_supertrack(payload):
    area_name = (payload or {}).get("area_name")
    phys_key = (payload or {}).get("phys_key")
    if not isinstance(area_name, str) or not isinstance(phys_key, str):
        emit("advance_error", {"message": "Invalid supertrack advance payload."})
        return

    roster = build_roster()
    if "error" in roster:
        emit("advance_error", {"message": "Unable to update indicator while roster API is unavailable."})
        return

    date_key = datetime.now().date().isoformat()
    area = next(
        (
            item
            for item in roster.get("areas", [])
            if item.get("is_supertrack") and item.get("name") == area_name
        ),
        None,
    )
    if area is None:
        emit("advance_error", {"message": "Supertrack area was not found."})
        return

    keys = [_physician_key(p) for p in area.get("current_physicians", [])]
    if not keys:
        _clear_supertrack_indicator_key(date_key, area_name)
        socketio.emit(
            "supertrack_state_updated",
            {"area_name": area_name, "phys_key": None},
        )
        return

    if phys_key not in keys:
        phys_key = _get_supertrack_indicator_key(date_key, area_name)
        if phys_key not in keys:
            phys_key = keys[0]

    next_idx = (keys.index(phys_key) + 1) % len(keys)
    next_key = keys[next_idx]
    _set_supertrack_indicator_key(date_key, area_name, next_key)

    socketio.emit(
        "supertrack_state_updated",
        {"area_name": area_name, "phys_key": next_key},
    )


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    socketio.run(app, debug=debug)
