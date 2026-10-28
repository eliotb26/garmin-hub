"""SQLite storage. Raw API payloads are kept as-is so nothing is lost; views are built on top."""

import json
import os
import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path

DEFAULT_DB_PATH = Path(os.getenv("GARMIN_HUB_DB", Path(__file__).resolve().parents[2] / "data" / "garmin.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_raw (
    day TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (day, kind)
);
CREATE TABLE IF NOT EXISTS activities (
    activity_id INTEGER PRIMARY KEY,
    start_local TEXT,
    activity_type TEXT,
    payload TEXT NOT NULL,
    fetched_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS whoop_raw (
    kind TEXT NOT NULL,
    record_id TEXT NOT NULL,
    start_local TEXT,
    end_local TEXT,
    payload TEXT NOT NULL,
    fetched_at TEXT NOT NULL,
    PRIMARY KEY (kind, record_id)
);
"""


def connect(path: Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)
    conn.executescript((Path(__file__).parent / "views.sql").read_text())
    return conn


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def save_daily(conn: sqlite3.Connection, day: str, kind: str, payload: object) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO daily_raw (day, kind, payload, fetched_at) VALUES (?, ?, ?, ?)",
        (day, kind, json.dumps(payload), _now()),
    )


def settled_kinds(conn: sqlite3.Connection, day: str, settle_days: int) -> set[str]:
    """Kinds for `day` fetched at least `settle_days` after it, so Garmin had time to finish them."""
    rows = conn.execute(
        "SELECT kind FROM daily_raw WHERE day = ? AND date(fetched_at) >= date(?, ?)",
        (day, day, f"+{settle_days} days"),
    ).fetchall()
    return {kind for (kind,) in rows}


def fetched_kinds(conn: sqlite3.Connection, day: str) -> set[str]:
    rows = conn.execute("SELECT kind FROM daily_raw WHERE day = ?", (day,)).fetchall()
    return {kind for (kind,) in rows}


def save_activity(conn: sqlite3.Connection, activity: dict) -> None:
    conn.execute(
        "INSERT OR REPLACE INTO activities (activity_id, start_local, activity_type, payload, fetched_at)"
        " VALUES (?, ?, ?, ?, ?)",
        (
            activity["activityId"],
            activity.get("startTimeLocal"),
            (activity.get("activityType") or {}).get("typeKey"),
            json.dumps(activity),
            _now(),
        ),
    )


def _local(timestamp: str | None, offset: str | None) -> str | None:
    """WHOOP times are UTC plus a timezone_offset like "-04:00"; return local "YYYY-MM-DD HH:MM:SS"."""
    if not timestamp:
        return None
    moment = datetime.fromisoformat(timestamp)
    if offset:
        sign = -1 if offset.startswith("-") else 1
        hours, minutes = offset.lstrip("+-").split(":")
        moment = moment + sign * timedelta(hours=int(hours), minutes=int(minutes))
    return moment.strftime("%Y-%m-%d %H:%M:%S")


def save_whoop(conn: sqlite3.Connection, kind: str, record: dict) -> None:
    # Recovery has no id of its own; it belongs to exactly one sleep.
    record_id = str(record.get("id") or record["sleep_id"])
    offset = record.get("timezone_offset")
    conn.execute(
        "INSERT OR REPLACE INTO whoop_raw (kind, record_id, start_local, end_local, payload, fetched_at)"
        " VALUES (?, ?, ?, ?, ?, ?)",
        (kind, record_id, _local(record.get("start"), offset), _local(record.get("end"), offset),
         json.dumps(record), _now()),
    )
