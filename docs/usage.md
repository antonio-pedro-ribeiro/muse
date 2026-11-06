# Using muse-monitor

muse-monitor records and analyzes EEG from a Muse S Athena headband. It connects to the headband
directly over Bluetooth. You can drive it from the command line (`muse`) or from a web interface
(`muse serve`). Both use the same engine, so a session started in one shows up in the other.

> The numbers it shows (band power, signal quality, model outputs) are measurements from a consumer
> device. They are not a medical assessment.

## Contents

1. [Setup](#1-setup)
2. [Quick start](#2-quick-start)
3. [Command line](#3-command-line)
4. [Web interface](#4-web-interface)
5. [Pipelines (TOML)](#5-pipelines-toml)
6. [Models and probes](#6-models-and-probes)
7. [Files, settings and troubleshooting](#7-files-settings-and-troubleshooting)

## 1. Setup

You need Python 3.13 and [uv](https://docs.astral.sh/uv/).

```
uv sync                    # install the app
uv sync --extra models     # optional: add the AI model (torch, braindecode, ...), about 1 GB
```

Prefix the commands below with `uv run` (for example `uv run muse sessions list`), or activate the
virtual environment in `.venv` first.

Before connecting:

- **Don't pair the headband in Windows Bluetooth settings.** If it's paired, remove it there.
  The app connects without pairing, and a paired headband drops the connection.
- **Close the Muse phone app.** The headband is only visible when nothing else is connected to it.
- Turn the headband on and put it on. Wet the sensors slightly if the signal is poor.

## 2. Quick start

```
muse devices                                  # is the headband visible?
muse check                                    # 10 s connection test: rates, packet loss, contact
muse run --calibrate --duration 10m           # record 10 minutes with a terminal dashboard
muse sessions list                            # find the session id
muse analyze <ID>                             # re-analyze it and write an HTML report
```

Or, for the web interface:

```
muse serve                                    # then open http://127.0.0.1:8765
```

No headband? Use the built-in test source: `muse run --source synthetic --duration 1m`.

## 3. Command line

Every command has `--help`. Commands that read or change sessions also accept:

| Option | Meaning |
|---|---|
| `--json` | Print machine-readable JSON instead of tables |
| `--server URL` | Run the command against a running `muse serve` (for example `http://127.0.0.1:8765`) instead of locally. Also read from `MUSE_SERVER` |
| `--token TOKEN` | Token for a server that needs one. Also read from `MUSE_TOKEN` |

Exit codes: `0` ok, `1` runtime error, `2` usage or configuration error, `3` headband not found.

Session ids are long (for example `01M6RW2KD771V17B86FV25ZE5M`). If the table cuts them off, use
`muse sessions list --json`.

### Devices and connection

| Command | What it does |
|---|---|
| `muse devices [--source openmuse]` | Scans for headbands and lists name, address and signal strength (RSSI). |
| `muse check [--duration 30] [--save raw.txt]` | Connects, streams briefly and reports sample rates, packet loss, battery and a rough contact check per electrode. Passes when EEG arrives at 90% or more of 256 Hz with at most 2% loss. Same as `muse-check test`. |

### Live sessions: `muse run`

Connects to the source, shows a live dashboard in the terminal and records to disk.

```
muse run [--source openmuse] [-a ANALYZER]... [-o key=value]... [--label TEXT]
         [--duration 30s|10m|1h] [--calibrate] [--no-record] [-c pipeline.toml]
```

| Option | Meaning |
|---|---|
| `--source` | `openmuse` (the headband, default) or `synthetic` (test signals) |
| `-a, --analyzer` | Analyzers to run (repeatable). Default: `quality`, `filter`, `baseline`, `bandpower`, `artifacts` |
| `-o, --option key=value` | Source setting, for example `-o address=00:55:DA:BB:E0:4C` or `-o preset=p21` |
| `--label` | A name for the session, shown in lists and reports |
| `--duration` | Stop automatically after this time. Without it, the session runs until you press `q` |
| `--calibrate` | Start with a 2-minute baseline: 60 s eyes open, then 60 s eyes closed. Prompts appear in the dashboard |
| `--no-record` | Watch only, store nothing |
| `-c, --config` | Use a pipeline file instead of the options above ([section 5](#5-pipelines-toml)) |

Keys while it runs: **`m`** adds a marker (labelled "marker"), **`q`** stops. To add markers with
your own labels, use the web interface.

At the end it prints a summary table per stream with samples, measured rate, expected rate, gaps
and missing samples, followed by each analyzer's report.

### Recorded sessions: `muse sessions`

| Command | What it does |
|---|---|
| `muse sessions list [--since 7d] [--label TEXT] [--mode live\|offline\|all]` | Lists sessions, newest first: start, label, duration, status and gaps. `offline` lists analysis runs. |
| `muse sessions show ID` | Shows metadata, device, firmware, per-stream statistics and analyzer reports. |
| `muse sessions export ID --format csv\|edf [--out PATH]` | CSV: one file per stream plus events. EDF+: one file for EEG tools (EDFbrowser, EEGLAB, MNE), with markers, artifacts and gaps as annotations. `--out` can be a file or a folder. |
| `muse sessions repair ID` | Finishes a session that ended in a crash: merges the partial files and marks it stopped. |

Session status:

| Status | Meaning |
|---|---|
| `running` | Data is streaming. |
| `stopped` | The session ended normally. |
| `failed` | The session ended because of an error. |
| `incomplete` | The app quit while recording. The data written so far (all but the last few seconds) is readable. Run `repair` to tidy the files. |

**About EDF+ files.** EDF stores every channel at an exact, fixed rate, but the headband's clocks run slightly
off (EEG at about 256.7 Hz instead of 256). The export therefore places each sample at its real time on the
fixed grid, so markers stay aligned with the signal even in long sessions. Gaps are filled with zeros and marked
with a `gap` annotation. Your raw recording is unchanged. MNE opens every channel as EEG; set the others
yourself, for example `raw.set_channel_types({"ACC x": "misc"})`.

### Offline analysis: `muse analyze`

```
muse analyze ID [-a ANALYZER]... [-c pipeline.toml] [--report copy.html]
```

Replays a recording through the analyzers, as if it were live but as fast as possible and without
dropping anything. It saves their results next to the raw data and writes an HTML report to
`recordings/<ID>/reports/`. The raw recording is never changed, and running it again replaces the
previous results. Without `-a` or `-c`, it uses the analyzers the session was recorded with.

### Plugins: `muse plugins`

| Command | What it does |
|---|---|
| `muse plugins list [--kind source\|analyzer\|sink]` | All plugins and their status: `ok`, `unavailable` (a dependency is missing, with an install hint), `incompatible` or `broken`. |
| `muse plugins info NAME` | A plugin's description and every config option with its default. |

You can add your own plugins as `.py` files in a `plugins/` folder in the working directory.

### Models: `muse models` and `muse train-probe`

| Command | What it does |
|---|---|
| `muse models pull cbramod [--revision SHA]` | Downloads the model weights. This is the only command that downloads anything. |
| `muse train-probe ID... [--label-from markers\|calibration] [--out head.npz]` | Trains a classifier on your labelled sessions and compares it with band power. See [section 6](#6-models-and-probes). |

### Web server: `muse serve`

```
muse serve [--host 127.0.0.1] [--port 8765] [--token-file PATH]
```

Starts the web interface and the REST/WebSocket API at `http://HOST:PORT`. Interactive API docs
are at `/docs`. By default it only listens on this computer. To use it from another device, bind
to another address (for example `--host 0.0.0.0`). A token is then required: it's created on first
start, printed once, and stored in `recordings/.muse/token`.

## 4. Web interface

Start `muse serve` and open `http://127.0.0.1:8765`. The top bar has three pages: **Live**,
**Sessions** and **Plugins**.

Every heading, field, button and table column has an **ⓘ icon**. Hover over it or tap it to read
what the element means.

If the server needs a token, the page asks for it once and remembers it in this browser.

### Live

**When no session is running**, the page shows a *Start a session* form:

| Field | Meaning |
|---|---|
| Source | `openmuse` for the headband, `synthetic` for test signals. Sources whose dependencies are missing are greyed out. |
| Label | Optional session name. |
| Record to disk | Save the raw data (on by default). If off, nothing is stored. |
| Source options | Settings for the source, generated from the plugin. For the headband, leave *address* empty to use the strongest one in range, and keep *preset* at p1041 for all sensors. |
| Analyzers | Tick the processing steps to run. Each one adds charts. Hover over ⓘ for a description. |

Press **Start session**. If the last session failed, the page shows why above the form.

**While a session runs**, the page shows:

- **Header:** the session status, elapsed time and headband battery.
- **Add marker:** type a label (for example "eyes closed" or "reading") and press the button. It's
  saved with a timestamp and drawn as a dashed line in the charts. Labelled markers are what
  `train-probe` learns from.
- **Calibrate (2 min):** a personal baseline. Keep your eyes open and relaxed for 60 s, then closed
  for 60 s. Band power is then also shown relative to this baseline.
- **Stop:** ends the session and finishes writing the recording.
- **Plugin chips:** one per plugin. Green means working. Yellow means degraded, for example
  Bluetooth packets lost. Red means it stopped after an error, while the rest keeps running.
  "dropped" counts chunks an analyzer skipped because it fell behind. The recording never drops
  data.
- **Charts**, one card per analyzer view, updated about 10 times a second:

| Card (analyzer) | What you see |
|---|---|
| Signal quality, Usable channels (`quality`) | Per electrode: good, poor contact, noisy or flat, and how many electrodes are usable. Fix contact before trusting anything else. |
| EEG (filtered) (`filter`) | The last seconds of each channel, band-passed to 1–40 Hz with mains hum removed. Display only; raw data is stored unfiltered. |
| Relative band power (`bandpower`) | Share of power in delta (1–4 Hz), theta (4–8), alpha (8–13), beta (13–30) and gamma (30–45). Alpha usually rises with eyes closed; blinks and movement inflate delta. |
| Band power over time, Spectrum | The same values over the last 2 minutes, and the full spectrum. A sharp spike at 50/60 Hz is mains interference. |
| Band power vs. baseline | Power divided by your eyes-open calibration (1 = same as baseline). Empty until you calibrate. |
| Artifacts (`artifacts`) | Blinks, jaw clenches and movement as events. |
| Probe output (`cbramod` with a head) | Probability per label from your trained probe ([section 6](#6-models-and-probes)). |

### Sessions

A table of all recordings, newest first, with start time, label, duration, device, number of gaps
and status. Click a row to open the session.

### Session

The page for one recording:

- **Analyze offline:** replays the recording through the analyzers and creates a report. A progress
  bar shows while it runs.
- **Export CSV / Export EDF:** downloads the data. CSV comes as a zip with one file per stream plus
  events; EDF+ is a single file for EEG tools.
- **Recorded signal:** the whole recording for one stream, reduced for display with peaks kept,
  plus markers. Pick the stream from the list: `eeg` (brain signal), `acc` (head movement), `gyro`
  (rotation) or `optics` (blood-flow sensors).
- **Streams:** samples, measured rate, expected rate, gaps and missing samples per stream. A rate
  well below the expected one, or many gaps, points to a weak Bluetooth link.
- **Reports:** HTML summaries from offline analyses. Each opens in a new tab and is a single file
  you can keep.
- **Plugin reports and charts:** each analyzer's end-of-session summary, plus charts over the whole
  session once an offline analysis has run.

### Plugins

One card per plugin with its kind, version, status and description. If a dependency is missing,
the card shows a hint, for example `uv sync --extra models`. It also has a table of every option
with its default; hover over ⓘ for what the option does.

## 5. Pipelines (TOML)

A pipeline file sets up a session in full: the source and its settings, the analyzers with their
options, and the outputs. Use it with `muse run -c FILE` or `muse analyze ID -c FILE`.

```toml
[session]
label = "evening, eyes closed practice"

[source]
plugin = "openmuse"
preset = "p1041"          # 8 EEG + optics + motion; "p21" = 4 EEG channels only

[[analyzer]]
plugin = "quality"

[[analyzer]]
plugin = "bandpower"
window_s = 4.0            # steadier estimates
step_s = 1.0

[[analyzer]]
plugin = "cbramod"
head = "head.npz"         # probe from `muse train-probe`

[[sink]]
plugin = "recorder"

[[sink]]
plugin = "export"         # also write CSV/EDF+ when the session closes
formats = ["csv", "edf"]
```

`muse plugins info NAME` lists every option a plugin accepts. A plugin can appear twice if each
copy has its own `id`, for example two `bandpower` blocks with `id = "bp_short"` and
`id = "bp_long"`.

## 6. Models and probes

The `cbramod` analyzer runs CBraMod, a pretrained EEG model (4.9M parameters, runs in real time on
a CPU). On its own it produces an *embedding*: 200 numbers per 4 s window that describe the
signal. Those numbers mean nothing to read directly. To get something useful you train a
**probe**, a small classifier that maps embeddings to your own labels.

1. **Install and download** (once):

   ```
   uv sync --extra models
   muse models pull cbramod
   ```

2. **Record labelled sessions.** Use at least 3, on different occasions. Either:
   - run `muse run --calibrate`, which gives the labels `eyes_open` and `eyes_closed`; or
   - add markers with your own labels in the web interface. Each marker labels the time until the
     next one. A marker labelled `-` or `rest` ends a labelled stretch.

3. **Train and compare:**

   ```
   muse train-probe ID1 ID2 ID3 --label-from calibration --out eyes.npz
   ```

   It prints one row per held-out session, with the balanced accuracy of the model probe and of
   the same classifier on plain band power. Chance level is shown for reference. Each session is
   tested by a probe that never saw it, which shows how well it carries over to a new day. With a
   single session it falls back to time blocks within it, and labels the result optimistic.

   The verdict says whether the model beats band power. If it doesn't, band power is the simpler
   and better choice for that task, and the model isn't worth running.

4. **Use the probe live or offline.** Add `cbramod` with `head = "eyes.npz"` to a pipeline file
   ([section 5](#5-pipelines-toml)). The *Probe output* cards then show the probability per label.
   A probe only works with the model version, electrodes and window length it was trained with;
   otherwise that analyzer stops with an explanation and the rest of the session continues.

## 7. Files, settings and troubleshooting

### Where things are stored

| Path | Contents |
|---|---|
| `recordings/<ID>/session.json` | Metadata: device, firmware, preset, channels, rates, pipeline, statistics |
| `recordings/<ID>/raw/*.parquet` | Raw samples per stream (EEG in µV, centred on zero), never modified. Recordings made before 2026-11-06 store EEG with a +725 µV offset; filtering and band power are unaffected |
| `recordings/<ID>/events.jsonl` | Markers, calibration, gaps, plugin events |
| `recordings/<ID>/derived/` | Analyzer results from the latest offline analysis |
| `recordings/<ID>/reports/` | HTML reports |
| `~/.cache/muse-monitor/models/` | Downloaded model weights |

The headband's serial number is stored only as a salted hash. EEG is personal data, so keep
`recordings/` private. It is excluded from git.

### Environment variables

| Variable | Effect |
|---|---|
| `MUSE_DATA_DIR` | Where recordings are stored (default `./recordings`) |
| `MUSE_MODELS_DIR` | Where model weights are stored |
| `MUSE_SERVER`, `MUSE_TOKEN` | Defaults for `--server` and `--token` |
| `MUSE_KEEP_SERIALS` | Store the headband serial number unhashed |

### Troubleshooting

| Symptom | What to try |
|---|---|
| `device not found` (exit code 3) | Turn the headband on, close the Muse phone app, and remove the headband from Windows Bluetooth settings if it's paired. |
| Connects, then drops immediately | The headband is paired in Windows; remove it there. |
| Signal quality shows poor contact or noisy | Adjust the band so the forehead and behind-the-ear sensors touch the skin, moisten them, and sit still for a few seconds. |
| Many gaps or a low rate in the Streams table | Move closer to the computer and away from other 2.4 GHz devices. About 1–2% loss is normal. |
| `incomplete` session | The app was killed while recording. The data is readable; run `muse sessions repair ID`. |
| `cbramod` shows as `unavailable` | Run `uv sync --extra models`. |
| `weights not found ... run muse models pull` | Run `muse models pull cbramod`. |
| Web page asks for a token | Copy it from the `muse serve` output or from `recordings/.muse/token`. |
