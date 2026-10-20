from datetime import date

import pytest
from garminconnect import GarminConnectNotFoundError, GarminConnectTooManyRequestsError

from garmin_hub import db, sync

TODAY = date(2026, 10, 20)


class FakeClient:
    def __init__(self, fail_kind=None, error=None):
        self.calls = []
        self.fail_kind, self.error = fail_kind, error

    def __getattr__(self, name):
        def call(day, *_):
            self.calls.append((name, day))
            if self.fail_kind == name:
                raise self.error
            return {"method": name, "day": day}
        return call


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "test.db")


def test_days_back_yields_oldest_first_ending_today():
    assert list(sync.days_back(TODAY, 3)) == ["2026-10-18", "2026-10-19", "2026-10-20"]


def test_sync_stores_every_kind_for_every_day(conn):
    counts = sync.sync_daily(FakeClient(), conn, 3, TODAY, delay=0)
    assert counts["fetched"] == 3 * len(sync.DAILY_ENDPOINTS)
    assert db.fetched_kinds(conn, "2026-10-18") == set(sync.DAILY_ENDPOINTS)


def _backdate_fetches(conn, when):
    conn.execute("UPDATE daily_raw SET fetched_at = ?", (when,))


def test_resync_skips_settled_days_but_refreshes_recent(conn):
    sync.sync_daily(FakeClient(), conn, 5, TODAY, delay=0)
    _backdate_fetches(conn, TODAY.isoformat())
    client = FakeClient()
    sync.sync_daily(client, conn, 5, TODAY, delay=0)
    assert {day for _, day in client.calls} == {"2026-10-19", "2026-10-20"}


def test_day_fetched_while_still_partial_is_refetched_later(conn):
    # Synced on Oct 18, when Oct 18 was still in progress; syncing again on Oct 20 must refresh it.
    sync.sync_daily(FakeClient(), conn, 1, date(2026, 10, 18), delay=0)
    _backdate_fetches(conn, "2026-10-18T22:00:00+00:00")
    client = FakeClient()
    sync.sync_daily(client, conn, 3, TODAY, delay=0)
    assert "2026-10-18" in {day for _, day in client.calls}


def test_auth_error_stops_sync(conn):
    from garminconnect import GarminConnectAuthenticationError
    client = FakeClient("get_stats", GarminConnectAuthenticationError("expired"))
    with pytest.raises(GarminConnectAuthenticationError):
        sync.sync_daily(client, conn, 3, TODAY, delay=0)


def test_repeated_errors_abort_sync(conn):
    class Broken:
        def __getattr__(self, name):
            def call(*_):
                raise ConnectionError("offline")
            return call
    with pytest.raises(sync.SyncAborted):
        sync.sync_daily(Broken(), conn, 3, TODAY, delay=0)


def test_not_found_is_stored_as_empty_so_it_is_not_retried(conn):
    client = FakeClient("get_hrv_data", GarminConnectNotFoundError("none"))
    counts = sync.sync_daily(client, conn, 1, TODAY, delay=0)
    assert counts["empty"] == 1
    assert "hrv" in db.fetched_kinds(conn, "2026-10-20")


def test_other_errors_are_counted_and_not_stored(conn):
    client = FakeClient("get_hrv_data", ValueError("boom"))
    counts = sync.sync_daily(client, conn, 1, TODAY, delay=0)
    assert counts["errors"] == 1
    assert "hrv" not in db.fetched_kinds(conn, "2026-10-20")


def test_rate_limit_stops_sync_and_keeps_progress(conn):
    client = FakeClient("get_hrv_data", GarminConnectTooManyRequestsError("slow down"))
    with pytest.raises(GarminConnectTooManyRequestsError):
        sync.sync_daily(client, conn, 1, TODAY, delay=0)
    assert {"summary", "sleep"} <= db.fetched_kinds(conn, "2026-10-20")


def test_activities_are_upserted(conn):
    class Client:
        def get_activities_by_date(self, start, end):
            return [{"activityId": 1, "startTimeLocal": "2026-10-19 07:00:00", "activityType": {"typeKey": "running"}}]
    assert sync.sync_activities(Client(), conn, 7, TODAY) == 1
    assert sync.sync_activities(Client(), conn, 7, TODAY) == 1
    assert conn.execute("SELECT activity_type FROM activities").fetchall() == [("running",)]


def test_mcp_query_rejects_writes():
    from garmin_hub import server
    with pytest.raises(ValueError):
        server.query("DELETE FROM activities")


def test_mcp_query_reports_truncation(monkeypatch, tmp_path):
    from garmin_hub import server
    monkeypatch.setattr(server.db, "DEFAULT_DB_PATH", tmp_path / "t.db")
    monkeypatch.setattr(server, "MAX_ROWS", 3)
    rows = server.query("WITH RECURSIVE n(x) AS (SELECT 1 UNION ALL SELECT x + 1 FROM n WHERE x < 10) SELECT x FROM n")
    assert len(rows) == 4 and "_truncated" in rows[-1]
