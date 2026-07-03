# apollo-sense

`apollo-sense` is a small native (Rust + C) companion binary for the Hermes
`health-data` plugin. It is the privacy-sensitive edge of two additive capabilities —
**ambient audio** (loudness + silence) and **location clustering** — and it is deliberately
**not** part of the Python plugin: it builds separately, is not installed into the Hermes
profile, and runs as its own OS process.

Its entire job is to turn raw sensor input into a handful of harmless **numbers** and append
them to a line-delimited JSON file (a "spool"). The Python plugin reads that spool like any
other data source.

## Why a separate process

```
┌─────────────────────────────────────────────────────────────┐
│  Hermes health-data plugin  (PURE PYTHON)                    │
│  audio.py / location.py  →  raw_records  →  canonical tables  │
└──────────────▲───────────────────────────────────────────────┘
               │  reads only aggregate rows from the JSONL spool
               │  OS PROCESS BOUNDARY — raw audio / GPS never crosses it
┌──────────────┴───────────────────────────────────────────────┐
│  apollo-sense  (NATIVE, separate process)                     │
│  Rust: mic capture (cpal), windowing, GPS clustering, spool   │
│  C:    rms_db(), peak_db()  (the hot DSP kernel)              │
└───────────────────────────────────────────────────────────────┘
```

Keeping capture in a separate native process means (a) the plugin stays pure-Python for CI
and packaging, and (b) **raw audio samples and GPS coordinates physically never enter the
plugin's memory or the database** — only the derived aggregates are ever written to disk.

## The privacy guarantee

- **Audio:** samples live only in an in-memory ring buffer for the length of one window.
  What leaves the process is a *loudness histogram* (counts of one-second sub-frames at each
  integer-dBFS level) plus the window's average and peak. No waveform, no spectrum, no
  words, no time-ordering within the window.
- **Location:** raw GPS pings are consumed in-process and never written. What leaves the
  process is a *completed visit*: an anonymous cluster id (`Location_A`), arrival/departure
  timestamps, and a reduced-precision (~100 m, 3-decimal) centroid.
- The spools live under `~/.hermes/` (outside the repo) and are never committed. The
  project's `scripts/secret_scan.py` blocks accidental commits of `*.db`, spool, and
  location fixture files.

## Build

Requires a Rust toolchain and a C compiler. On Linux, `cpal`'s ALSA backend also needs the
ALSA development headers.

```sh
# Debian/Ubuntu: sudo apt-get install libasound2-dev   (if not already present)
cd apollo-sense
cargo build --release          # drives the C kernel via the `cc` crate
cargo test                     # unit tests (no mic or GPS required)
```

The binary is produced at `apollo-sense/target/release/apollo-sense`.

## Commands

```
apollo-sense <once|run|check|calibrate|cluster> [seconds] [flags]
```

| Command | What it does |
|---------|--------------|
| `run [seconds]` | Continuously capture audio; append one spool row per window. This is the long-running mode. Default window 60 s. |
| `once [seconds]` | Capture a single window, print its features, and append it. Useful for a quick look. |
| `check [seconds]` | Microphone check gate: capture a few seconds and report whether the input is live (`PASS`), silent/dead (`FAIL`), or produced no clear sound (`INCONCLUSIVE`). Default 5 s. |
| `calibrate [seconds]` | Measure the ambient noise floor and emit a suggested silence threshold as one JSON line on stdout. Default 8 s. Consumed by `hermes health calibrate-audio`. |
| `cluster --pings FILE` | Read raw GPS pings and append completed visits to the location spool. Reads from `FILE` (or stdin). |

Flags: `--spool PATH` (override the spool file), `--pings PATH` (GPS ping input for
`cluster`), `--window-seconds N`, `--silence-db DB` (display-only; the real threshold is
applied downstream — see below).

Spool locations default to `~/.hermes/audio_spool.jsonl` and
`~/.hermes/location_spool.jsonl`, honoring the `HERMES_HOME` environment variable.

## Spool contracts

The spool is the "API" the Python connector consumes. Both files are append-only, one JSON
object per line, with a `schema` field so the format can evolve.

**Audio (`audio_spool.jsonl`, `schema: "v2"`):**

```json
{"window_start":"2026-07-02T09:00:00Z","window_end":"2026-07-02T09:01:00Z",
 "avg_noise_db":-12.3,"peak_noise_db":4.8,
 "level_hist_db":{"-23":40,"-10":20},
 "coverage_seconds":60,"sample_rate_hz":44100,"schema":"v2"}
```

`level_hist_db` maps integer dBFS → count of one-second sub-frames at that level.

**Location (`location_spool.jsonl`, `schema: "v1"`, completed visits only):**

```json
{"location_id":"Location_A","arrival_at":"2026-07-02T09:00:00Z",
 "departure_at":"2026-07-02T09:10:00Z","centroid_lat_coarse":37.422,
 "centroid_lng_coarse":-122.084,"radius_meters":0.0,"schema":"v1"}
```

## Silence threshold (calibration)

`apollo-sense` does **not** decide what counts as "quiet." It reports the loudness
distribution, and the Python plugin applies a silence threshold downstream when it computes
`quiet_minutes`. This means the threshold can be changed at any time and is applied
retroactively to all stored history — the running recorder is never touched.

Because a good threshold depends on the microphone and room (a digital-mic noise floor of
~−30 dBFS is common), it is tuned per environment:

```sh
hermes health calibrate-audio          # measures your floor, suggests a value, you confirm
hermes health calibrate-audio --yes    # accept the suggestion non-interactively
hermes health calibrate-audio --threshold -20   # set a value directly
```

## Typical setup

```sh
cd apollo-sense && cargo build --release
export APOLLO_SENSE_BIN=$PWD/target/release/apollo-sense   # so the plugin can find it

hermes health calibrate-audio          # tune the silence threshold to your mic/room
./target/release/apollo-sense run &     # start capturing (see "Running continuously")
hermes health connect-audio            # register the source; report spool status
hermes health sync                     # ingest new spool rows into the database
```

## Running continuously

`run` is a foreground process. How you keep it alive long-term is an operational choice and
intentionally left to the deploying user rather than imposed by the plugin. On Linux a
`systemd` user service (`Restart=on-failure`, `Environment=APOLLO_SENSE_BIN=...`) is the
natural fit. A crash mid-window simply produces a gap in the spool (lower coverage for that
minute), never corruption — the spool is atomic-append.

## Status and limitations

- **Audio** is complete and validated against a real microphone.
- **Location clustering** logic is complete and validated against synthetic ping fixtures.
  **Live GPS sourcing is not yet implemented** — `cluster` currently reads pings from a file
  or stdin (`<timestamp> <lat> <lng>` per line). Wiring a real GPS source (gpsd / NMEA /
  geoclue) is the remaining piece and requires GPS hardware to build and test.
- Location capture is opt-in: the Python side refuses to run unless
  `precise_location_opt_in` is set, so nothing is captured or stored until the user enables
  it.
