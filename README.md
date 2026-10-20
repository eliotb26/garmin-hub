# garmin-hub

Pull your Garmin Connect data into a local SQLite file, flag what the wrist got wrong, and let Claude query it over MCP.

Uses the unofficial [`garminconnect`](https://github.com/cyberjunky/python-garminconnect) library with your own login. It's for your own data; it can break when Garmin changes its login, and it isn't affiliated with Garmin.

## Setup

Needs [uv](https://docs.astral.sh/uv/getting-started/installation/) and Python 3.13+.

```
git clone https://github.com/eliotb26/garmin-hub && cd garmin-hub
uv run garmin-hub login          # in a real terminal: email, password, MFA code
uv run garmin-hub sync --days 90 # a few minutes for 90 days
uv run garmin-hub status
```

Your password is only typed at the prompt. Tokens are saved to `~/.garminconnect` (override with `GARMINTOKENS`). Data stays in `data/garmin.db` (override with `GARMIN_HUB_DB`).

## Connect Claude

Claude Code:

```
claude mcp add --scope user garmin -- uv run --directory "$PWD" garmin-hub mcp
```

Claude Desktop: add this to `claude_desktop_config.json` (Settings → Developer → Edit Config), using the full paths from `which uv` and `pwd`:

```json
{
  "mcpServers": {
    "garmin": {
      "command": "/full/path/to/uv",
      "args": ["run", "--directory", "/full/path/to/garmin-hub", "garmin-hub", "mcp"]
    }
  }
}
```

Tools: `daily`, `workouts`, `query` (read-only SQL, 10-second limit), `sync` (up to 14 days).

Anything these tools return is sent to the model you're chatting with, so you're sharing that health data with your Claude account.

## What's in the database

- `daily` view: one row per day (the day you woke up). Sleep, HRV, resting HR, stress, Body Battery, readiness, steps, workouts, and `quality_flags` (NULL when clean).
- `activities_clean` view: workouts with a `quality_flag` (NULL when clean):
  - `empty`: under 3 minutes
  - `hr_artifact`: max HR of 200 or more, usually the wrist sensor misreading during lifting
  - `forgotten_timer`: over 90 minutes with average HR under 90
- `daily` flags:
  - `low_wear`: under 18 hours worn
  - `sleep_suspect`: sleep that starts between 3 and 8pm, or runs over 11 hours
  - `sleep_short`: under 4 hours
- `daily_raw` and `activities` keep Garmin's raw JSON, so nothing is lost.

## Caveats

- Stress, Body Battery and readiness are all computed from the same heart-rate and HRV signal, so don't treat them as independent.
- Under about 8 weeks of data, patterns are hints, not trends.
