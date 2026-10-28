# garmin-hub

Pull your Garmin and WHOOP data into one local SQLite file, flag what the wrist got wrong, and let Claude query it over MCP. Use either band, or both side by side.

- **Garmin** uses the unofficial [`garminconnect`](https://github.com/cyberjunky/python-garminconnect) library with your own login. It can break when Garmin changes its login.
- **WHOOP** uses WHOOP's [official developer API](https://developer.whoop.com) (v2) through your own developer app.

It's for your own data, and it isn't affiliated with Garmin or WHOOP.

## Setup

Needs [uv](https://docs.astral.sh/uv/getting-started/installation/) and Python 3.13+.

```
git clone https://github.com/eliotb26/garmin-hub && cd garmin-hub
uv run garmin-hub login          # in a real terminal: email, password, MFA code
uv run garmin-hub sync --days 90 # a few minutes for 90 days
uv run garmin-hub status
```

Your password is only typed at the prompt. Tokens are saved to `~/.garminconnect` (override with `GARMINTOKENS`). Data stays in `data/garmin.db` (override with `GARMIN_HUB_DB`).

## WHOOP setup

1. Go to [developer-dashboard.whoop.com](https://developer-dashboard.whoop.com), sign in with your WHOOP account, and create an app.
2. Add the redirect URL `http://localhost:8765/callback`, and tick the scopes `read:recovery`, `read:cycles`, `read:sleep`, `read:workout`, `read:profile` (and `offline` if it's listed).
3. Copy the app's client ID and secret, then:

```
export WHOOP_CLIENT_ID=...  WHOOP_CLIENT_SECRET=...
uv run garmin-hub whoop-login          # opens WHOOP in your browser to approve
uv run garmin-hub whoop-sync --days 90
```

Tokens are saved to `~/.whoop-hub/tokens.json` (owner-only; override with `WHOOP_TOKENS`) and refresh themselves, so you only log in once. Keep the client ID and secret in your shell profile or a password manager, not in this repo. Using a different redirect URL? Set `WHOOP_REDIRECT_URI`; if it isn't on localhost, you paste the URL you land on after approving.

## Connect Claude

Claude Code:

```
claude mcp add --scope user garmin -- uv run --directory "$PWD" garmin-hub mcp
```

Using WHOOP? Add `-e WHOOP_CLIENT_ID=... -e WHOOP_CLIENT_SECRET=...` after `--scope user` so the `whoop_sync` tool can refresh your login. Reading data that's already synced doesn't need them.

Claude Desktop: add this to `claude_desktop_config.json` (Settings → Developer → Edit Config), using the full paths from `which uv` and `pwd`:

```json
{
  "mcpServers": {
    "garmin": {
      "command": "/full/path/to/uv",
      "args": ["run", "--directory", "/full/path/to/garmin-hub", "garmin-hub", "mcp"],
      "env": { "WHOOP_CLIENT_ID": "...", "WHOOP_CLIENT_SECRET": "..." }
    }
  }
}
```

Tools: `daily`, `workouts`, `whoop_daily`, `bands` (both bands, same nights), `query` (read-only SQL, 10-second limit), `sync` and `whoop_sync` (up to 14 days).

Things to ask once it's connected:
- "What actually determines my fitness age, and what would lower it fastest?"
- "What does a night out cost me, and how many nights does it take to recover?"
- "Do my Garmin and WHOOP agree on my bad nights? Which one tracks my sleep better?"

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
- `whoop_daily` view: one row per wake-up day (naps left out): recovery, HRV, resting HR, SpO2, skin temperature, sleep stages, day strain, and `quality_flags`:
  - `calibrating`: WHOOP is still learning your baseline (first few weeks)
  - `no_recovery`: the night wasn't scored
  - `sleep_short`: under 4 hours
- `whoop_workouts` view: workouts with strain and a `quality_flag` (`empty`, `hr_artifact`, `partial_recording` under 80% recorded).
- `bands` view: Garmin and WHOOP for the same night. Their HRV and sleep stages come from different algorithms, so compare whether they move together, not the raw numbers.
- `whoop_raw` keeps WHOOP's raw JSON.

## Caveats

- Stress, Body Battery and readiness are all computed from the same heart-rate and HRV signal, so don't treat them as independent.
- Under about 8 weeks of data, patterns are hints, not trends.
