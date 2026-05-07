"""Tests for app.py core logic using a mocked ShiftAdmin API."""
import json
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

import app as flask_app


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as c:
        yield c


def _make_shift(shift_name, start_offset_minutes, end_offset_minutes,
                user_id="1", user_name="Dr. Smith", user_type="Physician"):
    now = datetime.now()
    start = now + timedelta(minutes=start_offset_minutes)
    end = now + timedelta(minutes=end_offset_minutes)
    return {
        "shift_name": shift_name,
        "start_datetime": start.strftime("%Y-%m-%dT%H:%M:%S"),
        "end_datetime": end.strftime("%Y-%m-%dT%H:%M:%S"),
        "user_id": user_id,
        "user_name": user_name,
        "user_type": user_type,
    }


# ── _parse_dt ──────────────────────────────────────────────────────────────

def test_parse_dt_iso_with_T():
    dt = flask_app._parse_dt("2024-06-15T08:30:00")
    assert dt == datetime(2024, 6, 15, 8, 30, 0)


def test_parse_dt_space_separator():
    dt = flask_app._parse_dt("2024-06-15 08:30:00")
    assert dt == datetime(2024, 6, 15, 8, 30, 0)


def test_parse_dt_returns_none_on_empty():
    assert flask_app._parse_dt("") is None
    assert flask_app._parse_dt(None) is None


def test_parse_dt_returns_none_on_garbage():
    assert flask_app._parse_dt("not-a-date") is None


# ── _provider_name ─────────────────────────────────────────────────────────

def test_provider_name_from_user_name():
    assert flask_app._provider_name({"user_name": "Jones, Alice"}) == "Jones, Alice"


def test_provider_name_from_provider_name():
    assert flask_app._provider_name({"provider_name": "Dr. Bob"}) == "Dr. Bob"


def test_provider_name_from_first_last():
    assert flask_app._provider_name({"first_name": "Alice", "last_name": "Jones"}) == "Alice Jones"


def test_provider_name_fallback():
    assert flask_app._provider_name({}) == "Unknown"


# ── _is_physician ──────────────────────────────────────────────────────────

def test_is_physician_with_physician_ids_match():
    assert flask_app._is_physician({"user_id": "42"}, {"42"}) is True


def test_is_physician_with_physician_ids_no_match():
    assert flask_app._is_physician({"user_id": "99"}, {"42"}) is False


def test_is_physician_fallback_type_field():
    assert flask_app._is_physician({"user_type": "Physician"}, set()) is True
    assert flask_app._is_physician({"user_type": "Resident"}, set()) is False
    assert flask_app._is_physician({"user_type": "APP"}, set()) is False


def test_is_physician_empty_type_defaults_true():
    """If no type info at all, include the provider (defensive)."""
    assert flask_app._is_physician({}, set()) is True


# ── build_roster ───────────────────────────────────────────────────────────

def _mock_post_factory(users_resp, shifts_resp):
    def _mock_post(endpoint, extra_params=None):
        if endpoint == "org_users":
            return users_resp
        if endpoint == "org_scheduled_shifts":
            return shifts_resp
        return None
    return _mock_post


def test_build_roster_current_physician():
    """A shift active right now should put the physician in current_physicians."""
    shifts = {"scheduled_shifts": [
        _make_shift("ED Main", start_offset_minutes=-60, end_offset_minutes=60)
    ]}
    with patch.object(flask_app, "_post", side_effect=_mock_post_factory(None, shifts)):
        result = flask_app.build_roster()

    assert len(result["areas"]) == 1
    area = result["areas"][0]
    assert area["name"] == "ED Main"
    assert len(area["current_physicians"]) == 1
    assert area["current_physicians"][0]["name"] == "Dr. Smith"
    assert area["arriving_soon"] == []


def test_build_roster_arriving_soon():
    """A shift starting within 60 min should appear in arriving_soon."""
    shifts = {"scheduled_shifts": [
        _make_shift("ED Main", start_offset_minutes=30, end_offset_minutes=90)
    ]}
    with patch.object(flask_app, "_post", side_effect=_mock_post_factory(None, shifts)):
        result = flask_app.build_roster()

    area = result["areas"][0]
    assert area["current_physicians"] == []
    assert len(area["arriving_soon"]) == 1
    arriving = area["arriving_soon"][0]
    assert arriving["name"] == "Dr. Smith"
    assert 25 <= arriving["minutes_until"] <= 35


def test_build_roster_future_shift_beyond_hour_not_shown():
    """A shift starting in >1 h should not appear in arriving_soon."""
    shifts = {"scheduled_shifts": [
        _make_shift("ED Main", start_offset_minutes=90, end_offset_minutes=150)
    ]}
    with patch.object(flask_app, "_post", side_effect=_mock_post_factory(None, shifts)):
        result = flask_app.build_roster()

    assert result["areas"] == []


def test_build_roster_past_shift_not_shown():
    """An already-ended shift should not appear anywhere."""
    shifts = {"scheduled_shifts": [
        _make_shift("ED Main", start_offset_minutes=-120, end_offset_minutes=-10)
    ]}
    with patch.object(flask_app, "_post", side_effect=_mock_post_factory(None, shifts)):
        result = flask_app.build_roster()

    assert result["areas"] == []


def test_build_roster_supertrack_flag():
    """Shifts with 'ST' in the name are flagged is_supertrack=True."""
    shifts = {"scheduled_shifts": [
        _make_shift("ST Pods", start_offset_minutes=-30, end_offset_minutes=30)
    ]}
    with patch.object(flask_app, "_post", side_effect=_mock_post_factory(None, shifts)):
        result = flask_app.build_roster()

    assert result["areas"][0]["is_supertrack"] is True


def test_build_roster_non_supertrack_flag():
    shifts = {"scheduled_shifts": [
        _make_shift("ED Main", start_offset_minutes=-30, end_offset_minutes=30)
    ]}
    with patch.object(flask_app, "_post", side_effect=_mock_post_factory(None, shifts)):
        result = flask_app.build_roster()

    assert result["areas"][0]["is_supertrack"] is False


def test_build_roster_supertrack_sorted_first():
    """SuperTrack areas should appear before regular areas."""
    shifts = {"scheduled_shifts": [
        _make_shift("ED Main", start_offset_minutes=-30, end_offset_minutes=30,
                    user_id="1", user_name="Dr. A"),
        _make_shift("ST Pods", start_offset_minutes=-30, end_offset_minutes=30,
                    user_id="2", user_name="Dr. B"),
    ]}
    with patch.object(flask_app, "_post", side_effect=_mock_post_factory(None, shifts)):
        result = flask_app.build_roster()

    names = [a["name"] for a in result["areas"]]
    assert names.index("ST Pods") < names.index("ED Main")


def test_build_roster_filters_non_physicians():
    """Residents and APPs should be excluded from the roster."""
    shifts = {"scheduled_shifts": [
        _make_shift("ED Main", start_offset_minutes=-30, end_offset_minutes=30,
                    user_id="1", user_name="Dr. Physician", user_type="Physician"),
        _make_shift("ED Main", start_offset_minutes=-30, end_offset_minutes=30,
                    user_id="2", user_name="Resident Bob", user_type="Resident"),
        _make_shift("ED Main", start_offset_minutes=-30, end_offset_minutes=30,
                    user_id="3", user_name="APP Carol", user_type="APP"),
    ]}
    with patch.object(flask_app, "_post", side_effect=_mock_post_factory(None, shifts)):
        result = flask_app.build_roster()

    area = result["areas"][0]
    names = [p["name"] for p in area["current_physicians"]]
    assert "Dr. Physician" in names
    assert "Resident Bob" not in names
    assert "APP Carol" not in names


def test_build_roster_uses_org_users_for_physician_ids():
    """When org_users returns physician IDs, those should be used for filtering."""
    users = {"users": [
        {"user_id": "10", "user_type": "Physician"},
    ]}
    shifts = {"scheduled_shifts": [
        _make_shift("ED Main", start_offset_minutes=-30, end_offset_minutes=30,
                    user_id="10", user_name="Dr. Known"),
        _make_shift("ED Main", start_offset_minutes=-30, end_offset_minutes=30,
                    user_id="99", user_name="Unknown Person"),
    ]}
    with patch.object(flask_app, "_post", side_effect=_mock_post_factory(users, shifts)):
        result = flask_app.build_roster()

    area = result["areas"][0]
    names = [p["name"] for p in area["current_physicians"]]
    assert "Dr. Known" in names
    assert "Unknown Person" not in names


def test_build_roster_api_failure_returns_error():
    with patch.object(flask_app, "_post", return_value=None):
        result = flask_app.build_roster()

    assert "error" in result
    assert result["areas"] == []


def test_build_roster_skips_shift_with_bad_times():
    shifts = {"scheduled_shifts": [
        {
            "shift_name": "ED Main",
            "start_datetime": "not-a-date",
            "end_datetime": "also-not-a-date",
            "user_id": "1",
            "user_name": "Dr. Smith",
            "user_type": "Physician",
        }
    ]}
    with patch.object(flask_app, "_post", side_effect=_mock_post_factory(None, shifts)):
        result = flask_app.build_roster()

    # Bad shifts are skipped; no areas created
    assert result["areas"] == []


# ── HTTP routes ────────────────────────────────────────────────────────────

def test_index_route_returns_200(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert b"Roster" in resp.data


def test_api_roster_returns_json(client):
    shifts = {"scheduled_shifts": [
        _make_shift("ED Main", start_offset_minutes=-30, end_offset_minutes=30)
    ]}
    with patch.object(flask_app, "_post", side_effect=_mock_post_factory(None, shifts)):
        resp = client.get("/api/roster")

    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert "areas" in data
    assert "last_updated" in data


def test_api_roster_area_structure(client):
    shifts = {"scheduled_shifts": [
        _make_shift("ED Main", start_offset_minutes=-30, end_offset_minutes=30)
    ]}
    with patch.object(flask_app, "_post", side_effect=_mock_post_factory(None, shifts)):
        resp = client.get("/api/roster")

    area = json.loads(resp.data)["areas"][0]
    assert "name" in area
    assert "is_supertrack" in area
    assert "current_physicians" in area
    assert "arriving_soon" in area
