import argparse
import sys

from garminconnect import (
    GarminConnectAuthenticationError,
    GarminConnectTooManyRequestsError,
)

from . import auth, db, sync


def _login(_args) -> None:
    client = auth.login_interactive()
    print(f"Logged in as {client.get_full_name()}. Tokens saved to {auth.TOKENSTORE}.")


def _sync(args) -> None:
    try:
        client = auth.login_with_tokens()
    except GarminConnectAuthenticationError:
        sys.exit("Not logged in (or tokens expired). Run: uv run garmin-hub login")
    today = client.today()
    conn = db.connect()
    print(f"Syncing {args.days} days of daily data into {db.DEFAULT_DB_PATH}")
    try:
        counts = sync.sync_daily(client, conn, args.days, today)
        n_activities = sync.sync_activities(client, conn, args.days, today)
    except GarminConnectTooManyRequestsError:
        sys.exit("Garmin rate-limited us. Progress is saved; run sync again later to resume.")
    except GarminConnectAuthenticationError:
        sys.exit("Garmin rejected the saved login. Run: uv run garmin-hub login")
    except sync.SyncAborted as exc:
        sys.exit(f"Sync stopped: {exc}. Progress is saved.")
    print(f"Daily: {counts}. Activities: {n_activities}.")


def _status(_args) -> None:
    conn = db.connect()
    for kind, n, first, last in conn.execute(
        "SELECT kind, COUNT(*), MIN(day), MAX(day) FROM daily_raw GROUP BY kind ORDER BY kind"
    ):
        print(f"{kind:20} {n:5} days  {first} → {last}")
    (n_act,) = conn.execute("SELECT COUNT(*) FROM activities").fetchone()
    print(f"{'activities':20} {n_act:5}")


def _mcp(_args) -> None:
    from .server import main as serve

    serve()


def main() -> None:
    parser = argparse.ArgumentParser(prog="garmin-hub")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="log in once and cache tokens").set_defaults(run=_login)
    sync_cmd = commands.add_parser("sync", help="pull data into SQLite")
    sync_cmd.add_argument("--days", type=int, default=30)
    sync_cmd.set_defaults(run=_sync)
    commands.add_parser("status", help="show what's stored").set_defaults(run=_status)
    commands.add_parser("mcp", help="run the MCP server for Claude (stdio)").set_defaults(run=_mcp)
    args = parser.parse_args()
    args.run(args)
