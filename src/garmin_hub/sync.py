"""Pull daily health data and activities from Garmin Connect into SQLite."""

import sqlite3
import sys
import time
from collections.abc import Callable, Iterator
from datetime import date, timedelta

from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectNotFoundError,
    GarminConnectTooManyRequestsError,
)

from . import db

# Each kind maps to the library call that fetches one day of it.
DAILY_ENDPOINTS: dict[str, Callable] = {
    "summary": lambda c, d: c.get_stats(d),
    "sleep": lambda c, d: c.get_sleep_data(d),
    "hrv": lambda c, d: c.get_hrv_data(d),
    "stress": lambda c, d: c.get_stress_data(d),
    "body_battery": lambda c, d: c.get_body_battery(d),
    "training_readiness": lambda c, d: c.get_training_readiness(d),
    "training_status": lambda c, d: c.get_training_status(d),
    "rhr": lambda c, d: c.get_rhr_day(d),
    "max_metrics": lambda c, d: c.get_max_metrics(d),
}

# Garmin keeps revising the last couple of days (late syncs, sleep scored in the morning),
# so a day only counts as done once it was fetched this many days after it.
SETTLE_DAYS = 2
REQUEST_DELAY_SECONDS = 0.3
MAX_CONSECUTIVE_ERRORS = 5


class SyncAborted(RuntimeError):
    pass


def days_back(today: date, count: int) -> Iterator[str]:
    """Yield ISO dates from `count - 1` days ago through today, oldest first."""
    for offset in range(count - 1, -1, -1):
        yield (today - timedelta(days=offset)).isoformat()


def sync_daily(client, conn: sqlite3.Connection, days: int, today: date, delay: float = REQUEST_DELAY_SECONDS) -> dict:
    counts = {"fetched": 0, "skipped": 0, "empty": 0, "errors": 0}
    consecutive_errors = 0
    for day in days_back(today, days):
        have = db.settled_kinds(conn, day, SETTLE_DAYS)
        for kind, fetch in DAILY_ENDPOINTS.items():
            if kind in have:
                counts["skipped"] += 1
                continue
            try:
                payload = fetch(client, day)
            except (GarminConnectTooManyRequestsError, GarminConnectAuthenticationError):
                conn.commit()
                raise
            except GarminConnectNotFoundError:
                payload = None
            except Exception as exc:  # one bad endpoint shouldn't stop the whole sync
                print(f"  ! {day} {kind}: {exc}", file=sys.stderr)
                counts["errors"] += 1
                consecutive_errors += 1
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    conn.commit()
                    raise SyncAborted(f"{consecutive_errors} errors in a row, last: {exc}") from exc
                continue
            consecutive_errors = 0
            if payload in (None, {}, []):
                counts["empty"] += 1
            db.save_daily(conn, day, kind, payload)
            counts["fetched"] += 1
            time.sleep(delay)
        conn.commit()
        print(f"  {day} done", file=sys.stderr)
    return counts


def sync_activities(client, conn: sqlite3.Connection, days: int, today: date) -> int:
    start = (today - timedelta(days=days - 1)).isoformat()
    activities = client.get_activities_by_date(start, today.isoformat())
    for activity in activities:
        db.save_activity(conn, activity)
    conn.commit()
    return len(activities)
