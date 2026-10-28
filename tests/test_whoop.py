import io
import json
import time
import urllib.error
from datetime import UTC, datetime

import pytest

from garmin_hub import db, whoop

NOW = datetime(2026, 10, 20, 12, 0, tzinfo=UTC)


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "test.db")


@pytest.fixture
def creds(monkeypatch):
    monkeypatch.setenv("WHOOP_CLIENT_ID", "id")
    monkeypatch.setenv("WHOOP_CLIENT_SECRET", "secret")


def _tokens(tmp_path, expires_in=3600):
    path = tmp_path / "tokens.json"
    whoop.save_tokens({"access_token": "a1", "refresh_token": "r1", "expires_at": time.time() + expires_in}, path)
    return path


def _http_error(code):
    return urllib.error.HTTPError("https://x", code, "err", {}, io.BytesIO(b"{}"))


def _sleep(sleep_id, cycle_id, start, end, nap=False, hours=7.5):
    in_bed = int(hours * 3600000)
    return {
        "id": sleep_id, "cycle_id": cycle_id, "start": start, "end": end, "timezone_offset": "-04:00",
        "nap": nap, "score_state": "SCORED",
        "score": {"stage_summary": {"total_in_bed_time_milli": in_bed + 1800000, "total_awake_time_milli": 1800000,
                                    "total_slow_wave_sleep_time_milli": 5400000, "total_rem_sleep_time_milli": 6000000},
                  "sleep_performance_percentage": 88, "respiratory_rate": 14.2},
    }


def _recovery(sleep_id, cycle_id, score=60, calibrating=False):
    return {"sleep_id": sleep_id, "cycle_id": cycle_id, "score_state": "SCORED",
            "score": {"recovery_score": score, "hrv_rmssd_milli": 51.27, "resting_heart_rate": 57,
                      "user_calibrating": calibrating}}


def test_code_from_redirect_checks_state():
    url = "http://localhost:8765/callback?code=abc&state=12345678"
    assert whoop.code_from_redirect(url, "12345678") == "abc"
    with pytest.raises(whoop.WhoopAuthError):
        whoop.code_from_redirect(url, "87654321")


def test_declined_login_raises():
    with pytest.raises(whoop.WhoopAuthError, match="declined"):
        whoop.code_from_redirect("http://localhost:8765/callback?error=access_denied&state=12345678", "12345678")


def test_authorize_url_asks_for_offline_and_read_scopes():
    url = whoop.authorize_url("id", "12345678")
    assert "offline" in url and "read%3Arecovery" in url and "state=12345678" in url


def test_missing_credentials_explain_where_to_get_them(monkeypatch):
    monkeypatch.delenv("WHOOP_CLIENT_ID", raising=False)
    with pytest.raises(whoop.WhoopAuthError, match="developer-dashboard"):
        whoop._credentials()


def test_tokens_file_is_owner_only(tmp_path):
    path = _tokens(tmp_path)
    assert path.stat().st_mode & 0o777 == 0o600


def test_collection_follows_next_token(tmp_path, monkeypatch):
    pages = iter([{"records": [{"id": 1}], "next_token": "t2"}, {"records": [{"id": 2}], "next_token": ""}])
    seen = []

    def fake_http(request):
        seen.append(request.full_url)
        return next(pages)

    monkeypatch.setattr(whoop, "_http_json", fake_http)
    records = list(whoop.Client(_tokens(tmp_path)).collection("cycle", "2026-10-01T00:00:00.000Z", delay=0))
    assert [r["id"] for r in records] == [1, 2]
    assert "nextToken=t2" in seen[1] and "limit=25" in seen[0]


def test_expired_token_is_refreshed_and_rotated_token_saved(tmp_path, monkeypatch, creds):
    path = _tokens(tmp_path, expires_in=-10)
    calls = []

    def fake_http(request):
        calls.append(request.full_url)
        if request.full_url == whoop.TOKEN_URL:
            body = dict(x.split("=") for x in request.data.decode().split("&"))
            assert body["grant_type"] == "refresh_token" and body["refresh_token"] == "r1"
            return {"access_token": "a2", "refresh_token": "r2", "expires_in": 3600}
        assert request.headers["Authorization"] == "Bearer a2"
        return {"records": []}

    monkeypatch.setattr(whoop, "_http_json", fake_http)
    whoop.Client(path).get("/v2/cycle")
    assert calls[0] == whoop.TOKEN_URL
    assert json.loads(path.read_text())["refresh_token"] == "r2"


def test_401_refreshes_once_then_gives_up(tmp_path, monkeypatch, creds):
    def fake_http(request):
        if request.full_url == whoop.TOKEN_URL:
            return {"access_token": "a2", "refresh_token": "r2", "expires_in": 3600}
        raise _http_error(401)

    monkeypatch.setattr(whoop, "_http_json", fake_http)
    with pytest.raises(whoop.WhoopAuthError):
        whoop.Client(_tokens(tmp_path)).get("/v2/cycle")


def test_429_raises_rate_limited(tmp_path, monkeypatch):
    def fake_http(request):
        raise _http_error(429)

    monkeypatch.setattr(whoop, "_http_json", fake_http)
    with pytest.raises(whoop.WhoopRateLimited):
        whoop.Client(_tokens(tmp_path)).get("/v2/cycle")


class FakeClient:
    def __init__(self, data):
        self.data = data

    def collection(self, kind, start, delay=0):
        return iter(self.data.get(kind, []))


def test_sync_upserts_and_whoop_daily_uses_local_wake_day(conn):
    # Friday night: asleep 4:34am local (08:34Z), up 11:48am local. Local wake day is Oct 17.
    data = {
        "sleep": [_sleep("s1", 100, "2026-10-17T08:34:00.000Z", "2026-10-17T15:48:00.000Z")],
        "recovery": [_recovery("s1", 100, score=34)],
        "cycle": [{"id": 100, "start": "2026-10-17T08:34:00.000Z", "timezone_offset": "-04:00",
                   "score_state": "SCORED", "score": {"strain": 14.36}}],
    }
    whoop.sync(FakeClient(data), conn, 7, NOW, delay=0)
    whoop.sync(FakeClient(data), conn, 7, NOW, delay=0)
    rows = conn.execute("SELECT day, recovery, hrv, sleep_h, deep_min, bedtime, day_strain, quality_flags FROM whoop_daily").fetchall()
    assert rows == [("2026-10-17", 34, 51.3, 7.5, 90.0, "04:34:00", 14.4, None)]


def test_late_night_sleep_counts_for_the_morning_after(conn):
    # Asleep 11pm local on Oct 16 (03:00Z Oct 17), up 7am local Oct 17.
    whoop.sync(FakeClient({"sleep": [_sleep("s1", 1, "2026-10-17T03:00:00.000Z", "2026-10-17T11:00:00.000Z")]}),
               conn, 7, NOW, delay=0)
    assert conn.execute("SELECT day, bedtime FROM whoop_daily").fetchall() == [("2026-10-17", "23:00:00")]


def test_naps_are_left_out_and_flags_mark_unusable_nights(conn):
    data = {
        "sleep": [
            _sleep("s1", 1, "2026-10-17T03:00:00.000Z", "2026-10-17T11:00:00.000Z"),
            _sleep("nap", 1, "2026-10-17T18:00:00.000Z", "2026-10-17T19:00:00.000Z", nap=True, hours=0.8),
            _sleep("s2", 2, "2026-10-18T05:00:00.000Z", "2026-10-18T08:00:00.000Z", hours=3),
        ],
        "recovery": [_recovery("s1", 1, calibrating=True)],
    }
    whoop.sync(FakeClient(data), conn, 7, NOW, delay=0)
    flags = dict(conn.execute("SELECT day, quality_flags FROM whoop_daily").fetchall())
    assert flags == {"2026-10-17": "calibrating", "2026-10-18": "no_recovery sleep_short"}


def test_bands_joins_garmin_and_whoop_on_the_same_day(conn):
    db.save_daily(conn, "2026-10-17", "summary", {"totalSteps": 9000, "restingHeartRate": 60})
    db.save_daily(conn, "2026-10-17", "hrv", {"hrvSummary": {"lastNightAvg": 33}})
    whoop.sync(FakeClient({"sleep": [_sleep("s1", 1, "2026-10-17T08:34:00.000Z", "2026-10-17T15:48:00.000Z")],
                           "recovery": [_recovery("s1", 1)]}), conn, 7, NOW, delay=0)
    assert conn.execute("SELECT day, garmin_hrv, whoop_hrv, garmin_rhr, whoop_rhr FROM bands").fetchall() == [
        ("2026-10-17", 33, 51.3, 60, 57)
    ]
