"""Tests for app.py core logic using a mocked ShiftAdmin API."""
import base64
import json
import os
from datetime import datetime, timedelta
from unittest.mock import patch

import pytest

import app as flask_app


# ── Fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture
def client():
    if "AUTH_PASSWORD" not in os.environ:
        os.environ["AUTH_PASSWORD"] = "test_password"
    flask_app.AUTH_PASSWORD = os.environ.get("AUTH_PASSWORD", "")
    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as c:
        yield c


def _make_shift(shift_name, start_offset_minutes, end_offset_minutes,
                user_id="1", user_name="Dr. Smith", user_type="Physician",
                first_name="Dr.", last_name="Smith", group_id=1):
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
        "first_name": first_name,
        "last_name": last_name,
        "facility_id": 10,
        "group_id": group_id,
    }


# ── _parse_dt ──────────────────────────────────────────────────────────────

def test_parse_dt_iso_with_T():
    dt = flask_app._parse_dt("2024-06-15T08:30:00")
    assert dt == datetime(2024, 6, 15, 8, 30, 0)


def test_parse_dt_space_separator():
    dt = flask_app._parse_dt("2024-06-15 08:30:00")
    assert dt == datetime(2024, 6, 15, 8, 30, 0)


def test_parse_dt_shiftadmin_slash_format():
    dt = flask_app._parse_dt("5/7/2026 23:00")
    assert dt == datetime(2026, 5, 7, 23, 0, 0)


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
    assert names.index("Supertrack") < names.index("ED Main")


def test_build_roster_merges_supertrack_shifts_into_single_area():
    shifts = {"scheduled_shifts": [
        _make_shift("ST Pods", start_offset_minutes=-30, end_offset_minutes=30,
                    user_id="1", user_name="Dr. A"),
        _make_shift("Supertrack Rapid", start_offset_minutes=-20, end_offset_minutes=40,
                    user_id="2", user_name="Dr. B"),
    ]}
    with patch.object(flask_app, "_post", side_effect=_mock_post_factory(None, shifts)):
        result = flask_app.build_roster()

    supertrack_areas = [a for a in result["areas"] if a["name"] == "Supertrack"]
    assert len(supertrack_areas) == 1
    names = [p["name"] for p in supertrack_areas[0]["current_physicians"]]
    assert "Dr. A" in names
    assert "Dr. B" in names


def test_build_roster_supertrack_visible_within_first_six_hours():
    shifts = {"scheduled_shifts": [
        _make_shift("ST Pods", start_offset_minutes=-359, end_offset_minutes=60,
                    user_id="1", user_name="Dr. A"),
    ]}
    with patch.object(flask_app, "_post", side_effect=_mock_post_factory(None, shifts)):
        result = flask_app.build_roster()

    supertrack_areas = [a for a in result["areas"] if a["name"] == "Supertrack"]
    assert len(supertrack_areas) == 1
    names = [p["name"] for p in supertrack_areas[0]["current_physicians"]]
    assert "Dr. A" in names


def test_build_roster_supertrack_hidden_after_first_six_hours():
    shifts = {"scheduled_shifts": [
        _make_shift("ST Pods", start_offset_minutes=-361, end_offset_minutes=60,
                    user_id="1", user_name="Dr. A"),
    ]}
    with patch.object(flask_app, "_post", side_effect=_mock_post_factory(None, shifts)):
        result = flask_app.build_roster()

    assert result["areas"] == []


def test_build_roster_non_supertrack_not_limited_to_six_hours():
    shifts = {"scheduled_shifts": [
        _make_shift("ED Main", start_offset_minutes=-361, end_offset_minutes=60,
                    user_id="1", user_name="Dr. A"),
    ]}
    with patch.object(flask_app, "_post", side_effect=_mock_post_factory(None, shifts)):
        result = flask_app.build_roster()

    assert len(result["areas"]) == 1
    assert result["areas"][0]["name"] == "ED Main"
    assert len(result["areas"][0]["current_physicians"]) == 1


def test_build_roster_merges_color_shifts_into_single_area():
    shifts = {"scheduled_shifts": [
        _make_shift("CUH Blue 1 3p-11p", start_offset_minutes=-30, end_offset_minutes=30,
                    user_id="1", user_name="Dr. A"),
        _make_shift("Blue 2 11p-7a", start_offset_minutes=-20, end_offset_minutes=40,
                    user_id="2", user_name="Dr. B"),
    ]}
    with patch.object(flask_app, "_post", side_effect=_mock_post_factory(None, shifts)):
        result = flask_app.build_roster()

    blue_areas = [a for a in result["areas"] if a["name"] == "Blue"]
    assert len(blue_areas) == 1
    names = [p["name"] for p in blue_areas[0]["current_physicians"]]
    assert "Dr. A" in names
    assert "Dr. B" in names


def test_build_roster_keeps_different_color_areas_separate():
    shifts = {"scheduled_shifts": [
        _make_shift("CUH Blue 1 3p-11p", start_offset_minutes=-30, end_offset_minutes=30,
                    user_id="1", user_name="Dr. A"),
        _make_shift("Gray Fast Track", start_offset_minutes=-20, end_offset_minutes=40,
                    user_id="2", user_name="Dr. B"),
    ]}
    with patch.object(flask_app, "_post", side_effect=_mock_post_factory(None, shifts)):
        result = flask_app.build_roster()

    names = [a["name"] for a in result["areas"]]
    assert "Blue" in names
    assert "Gray" in names


def test_build_roster_merges_nprefixed_color_into_same_area():
    shifts = {"scheduled_shifts": [
        _make_shift("nPurple 7a-3p", start_offset_minutes=-30, end_offset_minutes=30,
                    user_id="1", user_name="Dr. A"),
        _make_shift("Purple 3p-11p", start_offset_minutes=-20, end_offset_minutes=40,
                    user_id="2", user_name="Dr. B"),
    ]}
    with patch.object(flask_app, "_post", side_effect=_mock_post_factory(None, shifts)):
        result = flask_app.build_roster()

    purple_areas = [a for a in result["areas"] if a["name"] == "Purple"]
    assert len(purple_areas) == 1
    names = [p["name"] for p in purple_areas[0]["current_physicians"]]
    assert "Dr. A" in names
    assert "Dr. B" in names


def test_build_roster_parses_shiftadmin_shift_start_and_end_fields():
    now = datetime.now()
    start = now - timedelta(minutes=30)
    end = now + timedelta(minutes=30)
    shifts = {
        "scheduled_shifts": [
            {
                "shift_name": "ED Main",
                "shift_start": f"{start.month}/{start.day}/{start.year} {start.strftime('%H:%M')}",
                "shift_end": f"{end.month}/{end.day}/{end.year} {end.strftime('%H:%M')}",
                "user_id": "1",
                "first_name": "Joby",
                "last_name": "Thoppil",
                "user_type": "Physician",
                "facility_id": 10,
                "group_id": 1,
            }
        ]
    }

    with patch.object(flask_app, "_post", side_effect=_mock_post_factory(None, shifts)):
        result = flask_app.build_roster()

    assert len(result["areas"]) == 1
    area = result["areas"][0]
    assert area["name"] == "ED Main"
    assert len(area["current_physicians"]) == 1
    assert area["current_physicians"][0]["name"] == "Joby Thoppil"


def test_build_roster_api_failure_returns_error():
    with patch.object(flask_app, "_post", return_value=None):
        result = flask_app.build_roster()

    assert "error" in result
    assert result["areas"] == []
    assert "error_type" in result
    assert "error_detail" in result


def test_post_sets_auth_error_type_for_401_or_403():
    class _Resp:
        status_code = 401
        text = "Unauthorized"

        def raise_for_status(self):
            raise flask_app.requests.exceptions.HTTPError(response=self)

    with patch.object(flask_app.requests, "post", return_value=_Resp()):
        result = flask_app._post("org_users")

    assert result is None
    assert flask_app.LAST_SHIFTADMIN_ERROR["type"] == "auth"
    assert flask_app.LAST_SHIFTADMIN_ERROR["status_code"] == 401


def test_post_sets_network_error_type_for_connection_error():
    with patch.object(
        flask_app.requests,
        "post",
        side_effect=flask_app.requests.exceptions.ConnectionError("boom"),
    ):
        result = flask_app._post("org_users")

    assert result is None
    assert flask_app.LAST_SHIFTADMIN_ERROR["type"] == "network"


def test_build_roster_skips_shift_with_bad_times():
    shifts = {"scheduled_shifts": [
        {
            "shift_name": "ED Main",
            "start_datetime": "not-a-date",
            "end_datetime": "also-not-a-date",
            "user_id": "1",
            "user_name": "Dr. Smith",
            "user_type": "Physician",
            "facility_id": 10,
        }
    ]}
    with patch.object(flask_app, "_post", side_effect=_mock_post_factory(None, shifts)):
        result = flask_app.build_roster()

    # Bad shifts are skipped; no areas created
    assert result["areas"] == []


# ── HTTP routes ────────────────────────────────────────────────────────────

def _get_auth_headers():
    """Build HTTP Basic Auth headers for testing."""
    password = os.environ.get("AUTH_PASSWORD", "test_password")
    credentials = base64.b64encode(b"cuhed:" + password.encode()).decode()
    return {"Authorization": f"Basic {credentials}"}


def test_index_route_rejects_missing_auth(client):
    resp = client.get("/")
    assert resp.status_code == 401
    assert "WWW-Authenticate" in resp.headers


def test_index_route_returns_200(client):
    resp = client.get("/", headers=_get_auth_headers())
    assert resp.status_code == 200
    assert b"Roster" in resp.data


def test_api_roster_rejects_missing_auth(client):
    resp = client.get("/api/roster")
    assert resp.status_code == 401
    assert "WWW-Authenticate" in resp.headers


def test_api_roster_returns_json(client):
    shifts = {"scheduled_shifts": [
        _make_shift("ED Main", start_offset_minutes=-30, end_offset_minutes=30)
    ]}
    with patch.object(flask_app, "_post", side_effect=_mock_post_factory(None, shifts)):
        resp = client.get("/api/roster", headers=_get_auth_headers())

    assert resp.status_code == 200
    data = json.loads(resp.data)
    assert "areas" in data
    assert "last_updated" in data


def test_api_roster_area_structure(client):
    shifts = {"scheduled_shifts": [
        _make_shift("ED Main", start_offset_minutes=-30, end_offset_minutes=30)
    ]}
    with patch.object(flask_app, "_post", side_effect=_mock_post_factory(None, shifts)):
        resp = client.get("/api/roster", headers=_get_auth_headers())

    area = json.loads(resp.data)["areas"][0]
    assert "name" in area
    assert "is_supertrack" in area
    assert "current_physicians" in area
    assert "arriving_soon" in area
