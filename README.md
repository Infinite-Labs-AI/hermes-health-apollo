# Hermes Health Apollo

Local-first health intelligence for Hermes. Apollo connects your wearable data,
calendar, and communication context so Hermes can answer the questions that sit
between "how am I doing?" and "what should I do next?"

It installs as the Hermes `health-data` plugin and keeps the sensitive parts on
your machine: health history, OAuth tokens, calendar context, Gmail metadata,
and generated analysis all live under your Hermes profile instead of inside this
repository.

Maintainer: RTK (X: `@RiverKhan`).
Community: Discord <https://discord.gg/AeUbSYas>

Prerequisite: Hermes Agent must already be installed and configured:
<https://github.com/NousResearch/hermes-agent>. Apollo is the `health-data`
plugin for Hermes, not a standalone app.

## What it does

Apollo turns personal signals into usable context for an agent:

- Syncs Oura sleep, readiness, activity, stress, heart-rate, workout, session,
  resilience, SpO2, and tag data into a local SQLite database.
- Pulls Google Calendar and Gmail metadata through Hermes' Google Workspace
  helper so workload, meetings, and inbox pressure can be compared with health
  signals.
- Records ambient loudness locally through a small native helper — average
  level, peak, and a loudness histogram per minute — so noise exposure can be
  lined up against sleep and stress. It measures how loud the room is, never what
  is said, and no audio is recorded or stored.
- Optionally groups location into anonymous places such as home, work, or the
  gym, so loudness and health can be summarized per place. Off until you opt in,
  and precise coordinates never leave your machine.
- Gives Hermes tools for recent health state, date ranges, stress days,
  correlations, heart-rate windows, workouts, sessions, tags, coverage checks,
  and higher-level analysis plans.
- Ships a health-coach skill so ordinary Hermes questions automatically use the
  right health/context tools instead of forcing you into low-level CLI commands.
- Includes terminal-native visual patterns for meeting stress leaderboards,
  recovery gates, workload-outcome matrices, day-shape barcodes, baseline drift,
  sleep debt, stress waterfalls, workout recovery, and coverage trust ledgers.
- Keeps analysis grounded with coverage checks and privacy guardrails, so thin
  data, missing syncs, and sensitive identities are surfaced instead of hidden.

Example questions:

```text
Why was I stressed yesterday?
What should I prioritize today based on recovery and schedule?
Did my inbox or meetings line up with my stress this week?
Show me which meetings created the biggest heart-rate spikes.
Am I recovered enough for a hard training day tomorrow?
What changed in my baseline over the last month?
```

## Why it is useful

Most health dashboards show biometric charts in isolation. Apollo is built for
the messier questions people actually ask: whether a packed calendar changed
their stress load, whether poor sleep is making today's plan unrealistic, whether
training is helping or draining recovery, and whether a trend is backed by
enough data to trust.

The goal is not medical diagnosis. The goal is a private, local, agent-readable
memory layer that can help you plan your day, protect recovery, notice workload
patterns, and turn wearable exhaust into decisions.

## Data model and privacy posture

Apollo is intentionally local-first. The plugin stores data in
`~/.hermes/health.db`, keeps OAuth material in the active Hermes home, and does
not commit or publish user data. Calendar and Gmail syncs store redacted
metadata and counts; Gmail body content, snippets, OAuth secrets, credential
files, calendar attendee identities, and route/map artifacts are either
intentionally not persisted or blocked from release by scanner and CI tripwires.

Ambient audio and location are handled by a separate local program,
`apollo-sense`, that only ever writes summary numbers to a spool file — loudness
levels for audio, anonymous place ids and coarse centroids for location. Raw
audio samples and precise coordinates are discarded inside that program and never
reach the plugin or the database. Location is opt-in and stays dark until you
enable it.

WHOOP support is documented for future connector work, but this release does
not include a `hermes health connect-whoop` command yet.

## Install and enable

Prerequisite: install and configure Hermes Agent first. This repository supplies
the `health-data` plugin; it does not install Hermes itself.

For development from a local checkout:

```bash
make install-git-hooks
make install-local
hermes plugins enable health-data
```

`make install-git-hooks` installs the repository's tracked `pre-commit` and
`pre-push` hooks into the clone's effective Git hooks directory.

- `pre-commit` scans staged files with `scripts/secret_scan.py` and blocks the
  commit if it finds secrets, personal local artifacts, token files, local DBs,
  map/route exports, or other blocked private material.
- `pre-push` scans tracked repo content again before upload and also blocks
  pushes to private ref namespaces such as Conductor checkpoints while allowing
  normal branch and tag pushes.

The installer will overwrite older repo-managed hook versions, but it refuses
to overwrite unrelated custom local hooks.

`make install-local` installs this workspace into the active Hermes profile as
`health-data` using the profile's Hermes home. It copies only git-visible,
non-ignored files and writes `.health-data-install.json` metadata with the
workspace source path, plugin version, git commit, branch, dirty state, install
time, and destination. `hermes health status` reports that metadata so stale
installed copies are visible. This command is intended for contributors and
local testing.

For normal use once a package is published:

```bash
pip install hermes-health-data
hermes plugins enable health-data
```

Restart Hermes after enabling, then run setup:

```bash
hermes health setup
```

Setup installs the local database, registers the `/health` command family, adds
the plugin `skills/` directory to `skills.external_dirs`, and installs the
scheduled sync launcher at `~/.hermes/scripts/health_sync.py`. It does not log in
to Oura or Google for you. Use `hermes health connect` for Oura and
`hermes health connect-google` for Google after setup reports the next action.
Setup also registers a Hermes no-agent cron job named `health-data-sync` on an
`every 6h` schedule. `setup` and `status` report whether that cron is registered
and whether the Hermes gateway appears to be running. If the gateway is stopped,
the cron job is registered but scheduled syncs will not fire automatically; start
or install the gateway explicitly with `hermes gateway run` or
`hermes gateway install`.

## Oura developer app setup

Official docs:
<https://cloud.ouraring.com/docs/authentication>

Oura does not expose developer-application registration through the API. You
must create the app in the browser:

1. Log in to <https://cloud.ouraring.com/oauth/applications> with the Oura
   account that owns the ring.
2. Create a new application.
3. Fill the registration form:
   - Display Name: `Hermes Health Data` or another personal name.
   - Description: `Personal local Hermes plugin for my own Oura health data.`
   - Contact Email: your email.
   - Website: a valid URL you control, for example a GitHub profile/repo or
     personal site. The portal expects a URL, not free-form text.
   - Privacy Policy / Terms of Service: for a personal-only app, use a valid
     URL that explains the data stays local on your machine. A README, gist, or
     personal page is enough for personal use.
   - Redirect URI: `http://localhost:43828/callback` exactly, then click
     `Add URI`. Oura's portal currently rejects `http://127.0.0.1...` even
     though it is also loopback; use the literal `localhost` hostname.
4. Select the scopes the plugin can sync: `Daily`, `Session`, `SpO2`, `Stress`,
   `Heartrate`, `Tag`, `Workout`, `Personal`, `Heart Health`, and `Ring
   Configuration`. Oura's OpenAPI 1.34 names the SpO2 OAuth scope `spo2Daily`,
   while some auth examples use `spo2`; leaving the OAuth scope parameter blank
   lets Oura request the scopes enabled on the developer app.
5. Accept the Oura API Agreement and save the application.
6. Save the Client ID and Client Secret in your Hermes env file, or pass them to
   the connect command once and let the plugin write `~/.hermes/.env` for you.
   The variable names are:

```text
HERMES_OURA_CLIENT_ID
HERMES_OURA_CLIENT_SECRET
```

Manual env-file example:

```bash
mkdir -p ~/.hermes
printf 'HERMES_OURA_CLIENT_ID="%s"\nHERMES_OURA_CLIENT_SECRET="%s"\n' \
  '<CLIENT_ID>' '<CLIENT_SECRET>' >> ~/.hermes/.env
chmod 600 ~/.hermes/.env
```

One-time CLI storage example:

```bash
hermes health connect --client-id '<CLIENT_ID>' --client-secret '<CLIENT_SECRET>'
```

Then run the short setup/admin flow:

```bash
hermes health setup
hermes health connect
hermes health status
hermes health sync
```

By default, the plugin omits the OAuth `scope` query parameter. Oura documents
that a blank scope requests the scopes enabled on the developer app, so keep the
app limited to the health-data scopes above. Advanced users can test explicit
OAuth scopes with:

```bash
hermes health connect --scopes 'email personal daily heartrate tag workout session spo2Daily'
```

The browser flow handles Oura login and consent. If loopback browser login does
not work, start the manual flow:

```bash
hermes health connect --manual
```

The command returns an `authorize_url` and `state`; open the URL, approve
access, then copy the `code` query parameter from the localhost callback URL and
re-run:

```bash
hermes health connect --code '<CODE_FROM_CALLBACK>' --state '<STATE_FROM_PREVIOUS_OUTPUT>'
```

For debugging browser callback problems without waiting the default two
minutes, pass `--loopback-timeout <seconds>`.

## WHOOP developer app setup

WHOOP support is not wired into this release yet; there is no
`hermes health connect-whoop` command today. This section is for users and
contributors who want to prepare a WHOOP app before the connector lands.

Official docs:
<https://developer.whoop.com/docs/developing/getting-started/>
<https://developer.whoop.com/docs/developing/oauth/>
<https://developer.whoop.com/api/>

1. Log in to the WHOOP Developer Dashboard at
   <https://developer-dashboard.whoop.com/> with the WHOOP account you want to
   authorize.
2. Create a Team if the dashboard prompts you to do so.
3. Create a new App.
4. Select only the scopes the connector will actually need:
   `read:profile`, `read:body_measurement`, `read:cycles`, `read:recovery`,
   `read:sleep`, and `read:workout`. Add `offline` when the connector needs
   refresh tokens for scheduled background sync.
5. Add the redirect URL that the eventual connector documents. WHOOP requires
   the OAuth redirect URL to match one registered in the Developer Dashboard.
   Their docs show HTTPS URLs and custom app-scheme URLs as valid examples; do
   not assume an arbitrary localhost callback will work until the connector
   specifies it.
6. Save the App, then copy the Client ID and Client Secret into your password
   manager or local secret store. Do not commit them to this repository, paste
   them into docs, or put them in `.context`, `.omx`, `docs`, `docker`, or
   `plans`.

When WHOOP support is added, the connector should document its exact redirect
URL, token file, env var names, and setup command in this section before
release.

## Google Workspace / Google OAuth setup

Official docs:
<https://developers.google.com/workspace/calendar/api/quickstart/python>
<https://developers.google.com/workspace/gmail/api/quickstart/python>

Google Workspace auth is exposed through the health CLI, but you first need a
Google Cloud OAuth client JSON. Create it in the browser:

1. Open the Google Cloud console and choose or create a project for personal
   Hermes use.
2. Enable the Google Calendar API and Gmail API for that project. The health
   plugin currently stores calendar and Gmail metadata only; it does not persist
   message bodies.
3. Configure the Google Auth platform / OAuth consent screen. For a personal
   project, a testing app is fine. If the app is `External`, add the Google
   account you will authorize as a test user.
4. Go to `Google Auth platform` -> `Clients`, create an OAuth client, and choose
   `Desktop app` as the application type. Google's Workspace quickstarts use a
   desktop OAuth client for local Python command-line apps.
5. Add or confirm the authorized redirect URI `http://localhost:1/`. The Hermes
   Google Workspace helper uses that loopback redirect for manual code capture.
6. Download the JSON credentials file. Keep it outside this repository, for
   example in `~/Downloads` or a password-manager export folder. Do not commit it
   or paste its contents into `.context`, `.omx`, `docs`, `docker`, or `plans`.

Before connecting, make sure the Hermes Google Workspace productivity skill is
installed and enabled. The health plugin delegates Google OAuth to that helper
and expects this script to exist:

```text
~/.hermes/skills/productivity/google-workspace/scripts/setup.py
```

A normal Google account is enough for personal calendar/Gmail sync. A Workspace
admin is only needed if your organization restricts third-party OAuth apps or
requires admin approval for requested scopes.

Then start the CLI flow:

```bash
hermes health connect-google --install-deps --open-browser
```

If no client secret has been stored yet, the command looks for a downloaded
Google OAuth JSON in `~/Downloads` or `~/Desktop`, stores it under
`~/.hermes/google_client_secret.json`, and opens Google's consent page. You can
also pass an explicit file:

```bash
hermes health connect-google --client-secret /path/to/google_client_secret.json --open-browser
```

Choose the Google account in the browser, approve access, then copy the full
`http://localhost:1/...` redirect URL from the address bar. The browser may show
a connection error because no server is listening on port 1; the code in the
URL is still valid. Finish the connection with:

```bash
hermes health connect-google --auth-code '<FULL_LOCALHOST_REDIRECT_URL>'
hermes health connect-google --check-live
hermes health sync --days 30
```

Use `hermes health connect-google` with no arguments to check the current state.
If the client secret is already stored, it prints a fresh authorization URL.

This command delegates OAuth to the existing Hermes Google Workspace skill. That
shared skill can request broader Workspace scopes than this health plugin
persists. The health plugin's sync path stores redacted calendar metadata,
Gmail message metadata/counts, sync status, and provenance in `~/.hermes/health.db`.

## Ambient audio and location

Apollo can fold two local signals into the same daily tables as Oura and
calendar: how loud your surroundings are, and, if you opt in, which anonymous
place you were in. Both come from a small native helper, `apollo-sense`, kept in
this repository under `apollo-sense/`. It reads the microphone (and, for
location, GPS) and writes only summary numbers to a spool file. Raw audio and
exact coordinates are thrown away inside the helper and never touch `health.db`.

Ambient audio is the working path today. Location clustering is included and
opt-in, but wiring a live GPS device (gpsd/NMEA/geoclue) is not part of this
release yet, so location currently expects a ping feed rather than a phone or
watch.

### Build the helper

`apollo-sense` is a Rust program with a small C kernel. Building it needs a Rust
toolchain, a C compiler, and, on Linux, the ALSA development headers. On Debian
or Ubuntu, install the prerequisites and build:

```bash
sudo apt install build-essential libasound2-dev
cd apollo-sense
cargo build --release
```

Point the plugin at the built binary with `APOLLO_SENSE_BIN`, or put it on your
`PATH` as `apollo-sense`, then confirm the microphone works before enabling
anything:

```bash
export APOLLO_SENSE_BIN="$PWD/target/release/apollo-sense"
apollo-sense check
```

### Ambient audio

The helper measures loudness in one-minute windows and appends them to
`~/.hermes/audio_spool.jsonl`. Capture a single minute with `once`, or leave
`run` going until you stop it:

```bash
apollo-sense once 60
apollo-sense run
```

Register the source and sync it into `health.db` like any other connector:

```bash
hermes health connect-audio
hermes health sync
```

After a sync, `hermes health sync` includes an `audio` block and loudness shows
up next to your other daily signals, for questions such as "was I noisier on my
worst sleep nights?".

The line between quiet and not-quiet depends on your room and microphone, so it
is a setting rather than a fixed number. Run `calibrate-audio` on its own to
measure your floor and confirm a suggestion, pass `--yes` to accept it without
prompting, or `--threshold` to set a value directly in dBFS:

```bash
hermes health calibrate-audio
hermes health calibrate-audio --yes
hermes health calibrate-audio --threshold -20
```

Calibration is retroactive: it re-scores the loudness history you already
captured, so you never have to re-record after changing the threshold.

### Location

Location is off until `precise_location_opt_in` is set in your health profile.
Until then, `hermes health connect-location` and the location part of
`hermes health sync` return `skipped` and touch nothing.

Once enabled, `apollo-sense` groups pings into anonymous places (`Location_A`,
`Location_B`, and so on), keeps only completed visits, and stores coarse
centroids, never your exact path. Register and sync it the same way:

```bash
hermes health connect-location
hermes health sync
```

Visits are joined against ambient audio per day, so you can ask which place was
loudest or where your stress ran highest without any place ever being named or
precisely located.

## Sensitive data warning

The plugin stores health and context data in `~/.hermes/health.db`. That SQLite
database can contain sensitive health PII, calendar metadata, email counts, and
food logs. It is protected only by local OS file permissions. Review backups,
sync tools, and shared-machine access before enabling the plugin.

Plan A adds local sync provenance tables alongside the existing compatibility
tables: `health_sources`, `source_scopes`, `sync_runs`, `sync_batches`,
`sync_cursors`, `sync_errors`, `sync_schedules`, `raw_records`, and
`record_lineage`. Existing `oura_*`, `calendar_daily`, `email_daily`,
`food_logs`, `sync_state`, and `daily_overview` tables remain the query
surface.

Ambient audio and location add `audio_windows`, `audio_daily`, `audio_settings`,
`location_clusters`, `location_visits`, and `location_audio_daily`. These hold
only loudness numbers and anonymous place summaries; raw audio and precise
coordinates are never written.

`raw_records` stores Oura payloads and redacted Google metadata locally so
future migrations can be traced back to provider records. Gmail body content,
Gmail snippets, OAuth secrets, credential files, calendar attendee identities,
conference join details, raw audio samples, and precise location coordinates are
not persisted by default.

## Commands

```bash
hermes health setup
hermes health connect
hermes health connect-google
hermes health connect-google --open-browser
hermes health connect-google --client-secret /path/to/google_client_secret.json --open-browser
hermes health connect-google --auth-code '<FULL_LOCALHOST_REDIRECT_URL>'
hermes health connect-audio
hermes health connect-location
hermes health calibrate-audio
hermes health calibrate-audio --threshold -20
hermes health sync
hermes health sync --days 30
hermes health sync --start-date 2026-05-12 --end-date 2026-06-10
hermes health status
hermes health uninstall
hermes health uninstall --purge
```

Slash command equivalents are exposed under `/health`, including `/health sync`
and `/health status`.

## Asking questions

Normal product use is just Hermes chat. Once the plugin is installed and
enabled, health questions route through the health-coach skill and local
health-data tools automatically:

```bash
hermes
# then ask: how did I sleep last night?

hermes -z "Why was I stressed yesterday?"
hermes -z "What should I prioritize today based on recovery and schedule?"
hermes -z "Did my inbox or meetings line up with my stress this week?"
```

`hermes health ask ...` is a lower-level command for debugging the health
question path:

```bash
hermes health ask "Why was I stressed yesterday?"
hermes health ask "What is current right now?" --sync
hermes health ask "How did I sleep?" --no-sync
```

`hermes health sync` uses a short rolling Oura window so routine cron jobs stay
cheap and includes yesterday plus today for Google email/calendar context. It
syncs Oura daily summaries, sleep sessions, heart-rate samples,
workouts, Oura app sessions, tags, enhanced tags, resilience, cardiovascular
age, VO2 max, sleep-time recommendations, rest-mode periods, ring battery, ring
configuration, and personal info when the token has the corresponding scopes.
After first connecting Oura, run `hermes health sync --days 365` or an explicit
date range to backfill older rows that were not in the default window.

`hermes health setup` installs an every-six-hours no-agent cron job named
`health-data-sync`. Normal health questions use that cron baseline. The
lower-level `hermes health ask ...` command only runs `hermes health sync
--days 3` before asking when local sync state is older than six hours, missing,
explicitly requested by `--sync`, or the question asks for current/fresh/latest
data.

The health sync cron is not a reminder scheduler. For user-visible reminders
such as "remind me to eat every day at 1pm", use Hermes cron/reminder scheduling
explicitly, for example:

```bash
hermes cron create --name eat-reminder --deliver local "0 13 * * *" "Remind me to eat."
```

`hermes health status` includes this guidance so sync setup is not confused with
reminder delivery.

Use `--sync` when you want to force a refresh now. Use `--no-sync` when you want
the fastest possible answer from already-synced local data.

Health questions query the local data through `health_query` using
`recent`, `date_range`, `stress_days`, `correlate`, `heart_rate`, `workouts`,
`sessions`, `tags`, and `coverage`. Broad analytical questions should use the
Plan B analysis tools when available: `health_analysis_plan`, `health_coverage`,
`health_analyze`, and `health_analysis_explain`.

## Development and verification

### Dev, release, and Hermes homes

This repository is now the canonical Apollo checkout. Hermes installs the
plugin into one active Hermes home at a time, normally
`~/.hermes/plugins/health-data`.

For the normal one-repo setup, just use your default Hermes home:

```bash
cd /path/to/hermes-health-apollo
make install-local
make verify-local-install
hermes health status
```

If you ever want a second isolated profile, set `HERMES_HOME` to another path
such as `~/.hermes-dev`. That profile gets its own `health.db`, OAuth files,
installed plugin copy, and sync launcher. Keep all Hermes homes outside the
repository and never commit their contents.

After local changes, refresh the active Hermes plugin copy and verify drift:

```bash
make install-local
make verify-local-install
hermes health status
```

`make verify-local-install` compares git-visible workspace files against the
installed plugin copy, reads only install metadata, and includes cron/gateway
status guidance. It does not read or export health databases, OAuth tokens, or
other personal data.

Before pushing public changes, run the same secret tripwire used by CI:

```bash
python scripts/secret_scan.py
```

The default scan checks git-tracked files and blocks private workspace paths
even if they were force-added. Private planning/runtime areas such as `.context`,
`.omx`, `docs`, `docker`, `plans`, `.private`, `.local`, and `scratch` are
ignored for normal Git usage and rejected by the scanner if tracked.
It also blocks local data artifacts that are easy to leak by accident:
databases, logs, source maps, map/location exports, route files (`.gpx`,
`.kml`, `.fit`, `.tcx`, `.geojson`, `.mbtiles`, and route/location JSON), and
credential/token JSON files.

For a clean snapshot or raw directory copy audit, run:

```bash
python scripts/secret_scan.py --all-files
```

This also checks ignored local files while skipping vendored/generated
directories such as `.venv`, `build`, and `dist`. It is expected to fail if
private local workspace folders contain files; publish from `git archive` or
remove those folders from the export.

Lane 1 is deterministic and does not call a model:

```bash
make health-eval
```

Plan C deterministic UX/eval scaffolding has a separate lane:

```bash
make health-eval-plan-c
make health-eval-plan-c-full
```

The deterministic judge and full local scorecard run on the same fixture without
calling a model. The baseline target remains available for quick fixture rebuilds:

```bash
make health-eval-judge
make health-eval-full
make health-eval-baseline
```

The current CI workflow runs the secret tripwire before deterministic health
evals on push. It also runs an all-files scan, a TruffleHog verified-secret
scan, full unit tests, Python compile checks, package artifact inspection, and a
clean snapshot export scan. Run the full local unit suite with
`python -m pytest` before publishing a release.

## Preparing a clean export

The repository itself is public now, so ordinary changes should be published by
committing and pushing to `main` or via pull request.

If you ever need a sanitized downstream export or one-off clean snapshot, do not
use `git clone --mirror` or `git push --mirror`. Export only the current tracked
`HEAD` instead:

```bash
python scripts/secret_scan.py
uv run --extra dev python -m pytest tests/test_secret_scan.py tests/test_context_commands.py tests/test_visual_catalog.py tests/test_register_contract.py
uv run --with build python scripts/release_safety.py
scripts/create_public_snapshot.sh ../hermes-health-data-public
cd ../hermes-health-data-public
git commit -m "Release Hermes health data plugin"
git remote add origin <TARGET_REPO_URL>
git push -u origin main
```

The snapshot script refuses to run with uncommitted changes, exports only the
current committed `HEAD` with `git archive`, re-runs the secret scan inside the
snapshot, and initializes a brand-new Git repository. This keeps private refs,
ignored folders, and historical local-only blobs out of the exported repo.
