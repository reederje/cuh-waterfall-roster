import base64
import json
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
SUPERTRACK_SELECTION_MODES = ("phase_multiplier", "strict_round_robin")
DEFAULT_SUPERTRACK_SELECTION_MODE = "phase_multiplier"
# Pod areas (besides Supertrack) that participate in the "next up" rotation
# as a single unit, rather than per-physician.
ROTATION_POD_AREAS = ("Gray", "Purple")

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
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS physician_assignment_state (
                shift_key TEXT PRIMARY KEY,
                patients_assigned INTEGER NOT NULL DEFAULT 0
                    CHECK (patients_assigned >= 0),
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS supertrack_settings (
                setting_key TEXT PRIMARY KEY,
                setting_value TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS rotation_state (
                date_key TEXT PRIMARY KEY,
                counter INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS skip_state (
                date_key TEXT PRIMARY KEY,
                skipped_keys TEXT NOT NULL,
                updated_at TEXT NOT NULL
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


def _shift_key(provider_id, start_dt, end_dt):
    return f"{provider_id}|{start_dt.isoformat()}|{end_dt.isoformat()}"


def _get_assignment_counts(shift_keys):
    if not shift_keys:
        return {}

    placeholders = ", ".join("?" for _ in shift_keys)
    try:
        with _state_db_conn() as conn:
            rows = conn.execute(
                f"""
                SELECT shift_key, patients_assigned
                FROM physician_assignment_state
                WHERE shift_key IN ({placeholders})
                """,
                shift_keys,
            ).fetchall()
        return {shift_key: patients_assigned for shift_key, patients_assigned in rows}
    except Exception as exc:
        logger.warning("State DB read failed for assignment counts: %s", exc)
        return {}


def _increment_assignment_count(shift_key):
    with _STATE_DB_LOCK:
        with _state_db_conn() as conn:
            conn.execute(
                """
                INSERT INTO physician_assignment_state
                    (shift_key, patients_assigned, updated_at)
                VALUES (?, 1, ?)
                ON CONFLICT(shift_key) DO UPDATE SET
                    patients_assigned = patients_assigned + 1,
                    updated_at = excluded.updated_at
                """,
                (shift_key, datetime.now().isoformat()),
            )
            row = conn.execute(
                """
                SELECT patients_assigned
                FROM physician_assignment_state
                WHERE shift_key = ?
                """,
                (shift_key,),
            ).fetchone()
            conn.commit()
    return row[0]


def _set_assignment_count(shift_key, patients_assigned):
    if not isinstance(patients_assigned, int) or patients_assigned < 0:
        raise ValueError("Patient count must be a non-negative whole number.")

    with _STATE_DB_LOCK:
        with _state_db_conn() as conn:
            conn.execute(
                """
                INSERT INTO physician_assignment_state
                    (shift_key, patients_assigned, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(shift_key) DO UPDATE SET
                    patients_assigned = excluded.patients_assigned,
                    updated_at = excluded.updated_at
                """,
                (shift_key, patients_assigned, datetime.now().isoformat()),
            )
            conn.commit()


def _patients_per_hour(patients_assigned, shift_start, now):
    elapsed_hours = (now - shift_start).total_seconds() / 3600
    if elapsed_hours <= 0:
        return 0.0
    return round(patients_assigned / max(elapsed_hours, 1.0), 1)


def _supertrack_phase_multiplier(shift_start, now):
    elapsed_hours = (now - shift_start).total_seconds() / 3600
    if elapsed_hours < 1:
        return 0.5
    if elapsed_hours < 2:
        return 1.0
    if elapsed_hours < 4:
        return 1.25
    if elapsed_hours < 5:
        return 1.75
    return 2.5


def _get_selection_mode():
    try:
        with _state_db_conn() as conn:
            row = conn.execute(
                "SELECT setting_value FROM supertrack_settings WHERE setting_key = 'selection_mode'"
            ).fetchone()
        if row and row[0] in SUPERTRACK_SELECTION_MODES:
            return row[0]
    except Exception as exc:
        logger.warning("State DB read failed for selection mode: %s", exc)
    return DEFAULT_SUPERTRACK_SELECTION_MODE


def _set_selection_mode(mode):
    if mode not in SUPERTRACK_SELECTION_MODES:
        raise ValueError("Invalid Supertrack selection mode.")
    with _STATE_DB_LOCK:
        with _state_db_conn() as conn:
            conn.execute(
                """
                INSERT INTO supertrack_settings (setting_key, setting_value, updated_at)
                VALUES ('selection_mode', ?, ?)
                ON CONFLICT(setting_key) DO UPDATE SET
                    setting_value = excluded.setting_value,
                    updated_at = excluded.updated_at
                """,
                (mode, datetime.now().isoformat()),
            )
            conn.commit()


def _strict_round_robin_next(keys, current_key):
    if not keys:
        return None
    if current_key not in keys:
        return keys[0]
    return keys[(keys.index(current_key) + 1) % len(keys)]


def _strict_round_robin_current(keys, current_key):
    if not keys:
        return None
    return current_key if current_key in keys else keys[0]


def _weighted_supertrack_next(physicians, current_key, now, selection_mode=None):
    if not physicians:
        return None

    keys = [_physician_key(physician) for physician in physicians]
    if selection_mode == "strict_round_robin":
        return _strict_round_robin_current(keys, current_key)

    scores = {
        _physician_key(physician): (
            physician["patients_per_hour"]
            * _supertrack_phase_multiplier(physician["_shift_start"], now)
        )
        for physician in physicians
    }
    lowest_score = min(scores.values())
    tied_keys = {
        key for key, score in scores.items() if score == lowest_score
    }
    if current_key in keys:
        start_index = keys.index(current_key)
        ordered_keys = keys[start_index:] + keys[:start_index]
    else:
        ordered_keys = keys

    return next(key for key in ordered_keys if key in tied_keys)


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


def _get_rotation_counter(date_key):
    try:
        with _state_db_conn() as conn:
            row = conn.execute(
                "SELECT counter FROM rotation_state WHERE date_key = ?",
                (date_key,),
            ).fetchone()
        return row[0] if row else 0
    except Exception as exc:
        logger.warning("State DB read failed for rotation counter: %s", exc)
        return 0


def _advance_rotation_counter(date_key):
    with _STATE_DB_LOCK:
        with _state_db_conn() as conn:
            conn.execute(
                """
                INSERT INTO rotation_state (date_key, counter, updated_at)
                VALUES (?, 1, ?)
                ON CONFLICT(date_key) DO UPDATE SET
                    counter = counter + 1,
                    updated_at = excluded.updated_at
                """,
                (date_key, datetime.now().isoformat()),
            )
            conn.commit()


def _build_rotation_slots(areas):
    """Build the ordered set of "next up" rotation entries: one slot per
    current Supertrack physician, plus one slot per non-empty pod area
    (Gray, Purple) regardless of how many physicians are in that pod."""
    slots = []

    supertrack_area = next((area for area in areas if area.get("is_supertrack")), None)
    if supertrack_area:
        slots.extend(["supertrack"] * len(supertrack_area.get("current_physicians", [])))

    pod_areas = sorted(
        (
            area for area in areas
            if area.get("name") in ROTATION_POD_AREAS and area.get("current_physicians")
        ),
        key=lambda area: area["name"],
    )
    slots.extend(area["name"] for area in pod_areas)

    return slots


def _get_skipped_keys(date_key):
    try:
        with _state_db_conn() as conn:
            row = conn.execute(
                "SELECT skipped_keys FROM skip_state WHERE date_key = ?",
                (date_key,),
            ).fetchone()
        if row:
            return set(json.loads(row[0]))
    except Exception as exc:
        logger.warning("State DB read failed for skip state: %s", exc)
    return set()


def _add_skipped_key(date_key, key):
    with _STATE_DB_LOCK:
        skipped = _get_skipped_keys(date_key)
        skipped.add(key)
        with _state_db_conn() as conn:
            conn.execute(
                """
                INSERT INTO skip_state (date_key, skipped_keys, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(date_key) DO UPDATE SET
                    skipped_keys = excluded.skipped_keys,
                    updated_at = excluded.updated_at
                """,
                (date_key, json.dumps(sorted(skipped)), datetime.now().isoformat()),
            )
            conn.commit()


def _clear_skipped_keys(date_key):
    with _STATE_DB_LOCK:
        with _state_db_conn() as conn:
            conn.execute("DELETE FROM skip_state WHERE date_key = ?", (date_key,))
            conn.commit()


def _hydrate_supertrack_state(areas, date_key, now, selection_mode):
    """Update each area's own indicator state and determine which entity
    (a Supertrack physician, or an entire pod area) is next up overall.
    Entries recorded in skip_state are excluded from being chosen."""
    supertrack_state = {}
    skipped_keys = _get_skipped_keys(date_key)

    for area in areas:
        if not area.get("is_supertrack"):
            continue

        area_name = area.get("name", "")
        all_physicians = area.get("current_physicians", [])
        keys = [_physician_key(p) for p in all_physicians]

        if not keys:
            _clear_supertrack_indicator_key(date_key, area_name)
            continue

        eligible_physicians = [
            physician for physician in all_physicians
            if _physician_key(physician) not in skipped_keys
        ] or all_physicians

        current_key = _get_supertrack_indicator_key(date_key, area_name)
        next_key = _weighted_supertrack_next(
            eligible_physicians, current_key, now, selection_mode
        )
        if next_key != current_key:
            _set_supertrack_indicator_key(date_key, area_name, next_key)

        supertrack_state[area_name] = next_key

    slots = _build_rotation_slots(areas)
    if not slots:
        return supertrack_state, None

    counter = _get_rotation_counter(date_key)
    num_slots = len(slots)
    chosen = None
    for offset in range(num_slots):
        candidate = slots[(counter + offset) % num_slots]
        if candidate == "supertrack" or candidate not in skipped_keys:
            chosen = candidate
            break
    if chosen is None:
        chosen = slots[counter % num_slots]

    if chosen == "supertrack":
        supertrack_area = next((area for area in areas if area.get("is_supertrack")), None)
        area_name = supertrack_area.get("name", "Supertrack") if supertrack_area else "Supertrack"
        next_up = {
            "type": "physician",
            "area_name": area_name,
            "phys_key": supertrack_state.get(area_name),
        }
    else:
        next_up = {"type": "area", "area_name": chosen}

    return supertrack_state, next_up


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
            provider_id = shift.get("user_id") or shift.get("provider_id") or name
            areas[area_key]["current_physicians"].append({
                "name": name,
                "shift_start": start_dt.strftime("%H:%M"),
                "shift_end": end_dt.strftime("%H:%M"),
                "shift_key": _shift_key(provider_id, start_dt, end_dt),
                "_shift_start": start_dt,
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

    current_physicians = [
        physician
        for area in sorted_areas
        for physician in area["current_physicians"]
    ]
    assignment_counts = _get_assignment_counts(
        [physician["shift_key"] for physician in current_physicians]
    )
    for physician in current_physicians:
        patients_assigned = assignment_counts.get(physician["shift_key"], 0)
        physician["patients_assigned"] = patients_assigned
        physician["patients_per_hour"] = _patients_per_hour(
            patients_assigned, physician["_shift_start"], now
        )

    selection_mode = _get_selection_mode()
    supertrack_state, next_up = _hydrate_supertrack_state(
        sorted_areas, state_date_key, now, selection_mode
    )
    for physician in current_physicians:
        del physician["_shift_start"]
    return {
        "areas": sorted_areas,
        "last_updated": now.isoformat(),
        "supertrack_state": supertrack_state,
        "supertrack_selection_mode": selection_mode,
        "next_up": next_up,
    }


@app.route("/")
@_require_auth
def index():
    return render_template("index.html")


@app.route("/api/roster")
@_require_auth
def api_roster():
    return jsonify(build_roster())


def _find_active_physician(roster, shift_key):
    for area in roster.get("areas", []):
        for physician in area.get("current_physicians", []):
            if physician.get("shift_key") == shift_key:
                return area, physician
    return None, None


def _emit_roster_update():
    roster = build_roster()
    if "error" in roster:
        socketio.emit("assignment_error", {
            "message": "Unable to refresh roster after updating assignment state."
        })
        return
    socketio.emit("roster_updated", roster)


@socketio.on("set_selection_mode")
def handle_set_selection_mode(payload):
    mode = (payload or {}).get("mode")
    try:
        _set_selection_mode(mode)
    except ValueError as exc:
        emit("assignment_error", {"message": str(exc)})
        return
    _emit_roster_update()


@socketio.on("record_assignment")
def handle_record_assignment(payload):
    shift_key = (payload or {}).get("shift_key")
    if not isinstance(shift_key, str):
        emit("assignment_error", {"message": "Invalid assignment payload."})
        return

    roster = build_roster()
    if "error" in roster:
        emit("assignment_error", {
            "message": "Unable to record an assignment while the roster API is unavailable."
        })
        return

    area, physician = _find_active_physician(roster, shift_key)
    if physician is None:
        emit("assignment_error", {
            "message": "That physician is no longer active on the roster."
        })
        return

    _increment_assignment_count(shift_key)
    if area and (area.get("is_supertrack") or area.get("name") in ROTATION_POD_AREAS):
        _advance_rotation_counter(datetime.now().date().isoformat())
    # A real assignment ends the current round, so skipped entities are eligible again.
    _clear_skipped_keys(datetime.now().date().isoformat())
    if area and area.get("is_supertrack"):
        keys = [
            _physician_key(item)
            for item in area.get("current_physicians", [])
        ]
        _set_supertrack_indicator_key(
            datetime.now().date().isoformat(),
            area["name"],
            _strict_round_robin_next(keys, _physician_key(physician))
            if _get_selection_mode() == "strict_round_robin"
            else _get_supertrack_indicator_key(
                datetime.now().date().isoformat(), area["name"]
            ),
        )
    _emit_roster_update()


@socketio.on("skip_next_up")
def handle_skip_next_up(payload=None):
    roster = build_roster()
    if "error" in roster:
        emit("assignment_error", {
            "message": "Unable to skip while the roster API is unavailable."
        })
        return

    next_up = roster.get("next_up")
    if not next_up:
        emit("assignment_error", {"message": "There is no next up selection to skip."})
        return

    skip_key = next_up.get("phys_key") if next_up.get("type") == "physician" else next_up.get("area_name")
    if not skip_key:
        emit("assignment_error", {"message": "Unable to determine who to skip."})
        return

    _add_skipped_key(datetime.now().date().isoformat(), skip_key)
    _emit_roster_update()


@socketio.on("set_assignment_count")
def handle_set_assignment_count(payload):
    shift_key = (payload or {}).get("shift_key")
    patients_assigned = (payload or {}).get("patients_assigned")
    if not isinstance(shift_key, str) or isinstance(patients_assigned, bool):
        emit("assignment_error", {"message": "Invalid assignment correction payload."})
        return

    roster = build_roster()
    if "error" in roster:
        emit("assignment_error", {
            "message": "Unable to update an assignment while the roster API is unavailable."
        })
        return

    _, physician = _find_active_physician(roster, shift_key)
    if physician is None:
        emit("assignment_error", {
            "message": "That physician is no longer active on the roster."
        })
        return

    try:
        _set_assignment_count(shift_key, patients_assigned)
    except ValueError as exc:
        emit("assignment_error", {"message": str(exc)})
        return

    _emit_roster_update()


if __name__ == "__main__":
    debug = os.environ.get("FLASK_DEBUG", "0") == "1"
    socketio.run(app, debug=debug)
