"""MCP server: lets Claude read your Garmin and WHOOP data from the local SQLite database."""

import sqlite3
import time
from contextlib import closing
from datetime import date

from mcp.server.mcpserver import MCPServer

from . import db

INSTRUCTIONS = """Garmin and WHOOP health data for one person, stored locally.
- `daily` view (Garmin): one row per day (the day you woke up). sleep_*, hrv and readiness describe the night before.
- `activities_clean` view (Garmin): workouts with a quality_flag (empty, hr_artifact, forgotten_timer).
- `whoop_daily` view: one row per wake-up day: recovery, hrv, resting_hr, sleep, day_strain, quality_flags.
- `whoop_workouts` view: WHOOP workouts with strain and a quality_flag.
- `bands` view: Garmin and WHOOP for the same night. Their HRV and sleep stages use different algorithms,
  so compare direction and agreement, not raw values.
- Before any average, trend or correlation: keep only rows where quality_flags / quality_flag IS NULL,
  say how many days you used, and check whether weekends (Sat/Sun rows = Fri/Sat nights) explain the pattern.
- Stress, Body Battery and readiness are computed from the same heart-rate/HRV signal; don't treat them as independent.
- Under ~8 weeks of data, call patterns hints, not trends."""

server = MCPServer("garmin-hub", instructions=INSTRUCTIONS)
MAX_ROWS = 500
QUERY_TIMEOUT_SECONDS = 10
MAX_SYNC_DAYS = 14


def _read_only() -> sqlite3.Connection:
    db.connect(db.DEFAULT_DB_PATH).close()  # make sure tables and views exist
    conn = sqlite3.connect(f"file:{db.DEFAULT_DB_PATH}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(sql: str, params: tuple = ()) -> list[dict]:
    deadline = time.monotonic() + QUERY_TIMEOUT_SECONDS
    with closing(_read_only()) as conn:
        conn.set_progress_handler(lambda: 1 if time.monotonic() > deadline else 0, 100_000)
        rows = [dict(r) for r in conn.execute(sql, params).fetchmany(MAX_ROWS + 1)]
    if len(rows) > MAX_ROWS:
        return rows[:MAX_ROWS] + [{"_truncated": f"Only the first {MAX_ROWS} rows. Aggregate in SQL instead."}]
    return rows


@server.tool()
def daily(start: str, end: str | None = None) -> list[dict]:
    """Daily health rows (sleep, HRV, resting HR, stress, Body Battery, readiness, steps, workouts, quality_flags) from start to end, YYYY-MM-DD."""
    return _rows("SELECT * FROM daily WHERE day BETWEEN ? AND ?", (start, end or date.today().isoformat()))


@server.tool()
def workouts(start: str, end: str | None = None) -> list[dict]:
    """Workouts with duration, avg/max HR, training load and a quality_flag, from start to end, YYYY-MM-DD."""
    return _rows(
        "SELECT activity_id, start_local, activity_type, name, minutes, avg_hr, max_hr, training_load,"
        " aerobic_te, anaerobic_te, quality_flag FROM activities_clean"
        " WHERE substr(start_local, 1, 10) BETWEEN ? AND ? ORDER BY start_local",
        (start, end or date.today().isoformat()),
    )


@server.tool()
def whoop_daily(start: str, end: str | None = None) -> list[dict]:
    """WHOOP daily rows (recovery, HRV, resting HR, SpO2, skin temp, sleep stages, day strain, quality_flags) from start to end, YYYY-MM-DD."""
    return _rows("SELECT * FROM whoop_daily WHERE day BETWEEN ? AND ?", (start, end or date.today().isoformat()))


@server.tool()
def bands(start: str, end: str | None = None) -> list[dict]:
    """Garmin and WHOOP side by side for the same nights (HRV, resting HR, sleep, deep/REM, readiness vs recovery), YYYY-MM-DD."""
    return _rows("SELECT * FROM bands WHERE day BETWEEN ? AND ?", (start, end or date.today().isoformat()))


@server.tool()
def query(sql: str) -> list[dict]:
    """Run a read-only SQL query. Tables: daily_raw(day, kind, payload JSON), activities(payload JSON), whoop_raw(kind, record_id, start_local, end_local, payload JSON). Views: daily, activities_clean, whoop_daily, whoop_workouts, bands. Use json_extract for raw fields."""
    if not sql.lstrip().lower().startswith(("select", "with")):
        raise ValueError("Only SELECT queries are allowed.")
    return _rows(sql)


@server.tool()
def sync(days: int = 3) -> str:
    """Pull the latest days (max 14) from Garmin Connect, using the saved login."""
    from garminconnect import (
        GarminConnectAuthenticationError,
        GarminConnectTooManyRequestsError,
    )

    from . import auth
    from . import sync as sync_mod

    days = max(1, min(days, MAX_SYNC_DAYS))
    try:
        client = auth.login_with_tokens()
        with closing(db.connect()) as conn:
            counts = sync_mod.sync_daily(client, conn, days, client.today())
            n = sync_mod.sync_activities(client, conn, days, client.today())
    except GarminConnectAuthenticationError:
        return "Garmin rejected the saved login. Ask the user to run `uv run garmin-hub login` in a terminal."
    except GarminConnectTooManyRequestsError:
        return "Garmin rate-limited the sync. What was fetched is saved; try again later."
    except sync_mod.SyncAborted as exc:
        return f"Sync stopped early: {exc}. What was fetched is saved."
    return f"Synced {days} days: {counts}, {n} activities."


@server.tool()
def whoop_sync(days: int = 3) -> str:
    """Pull the latest days (max 14) from WHOOP, using the saved login."""
    from . import whoop

    days = max(1, min(days, MAX_SYNC_DAYS))
    try:
        with closing(db.connect()) as conn:
            counts = whoop.sync(whoop.Client(), conn, days)
    except whoop.WhoopAuthError:
        return "WHOOP isn't connected or the login expired. Ask the user to run `uv run garmin-hub whoop-login` in a terminal."
    except whoop.WhoopRateLimited:
        return "WHOOP rate-limited the sync. What was fetched is saved; try again in a minute."
    return f"Synced {days} days of WHOOP: {counts}."


def main() -> None:
    server.run()
