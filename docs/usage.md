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

### Alternative: through the Mind Monitor app

If the headband is paired with the Mind Monitor app on your phone, the app can relay the data to this
computer instead of the app connecting over Bluetooth itself.

1. Put the phone and this computer on the same Wi-Fi network.
2. In Mind Monitor's settings, set **OSC Stream Target IP** to this computer's local address (for
   example `192.168.1.10`; `muse run --source mindmonitor` lists it if no data arrives) and **OSC Stream
   Port** to `5000`. Leave the OSC path prefix empty.
3. Allow UDP port 5000 through the Windows firewall, for example from an administrator terminal:
   `netsh advfirewall firewall add rule name="muse-monitor OSC" dir=in action=allow protocol=UDP localport=5000`
4. Start streaming in Mind Monitor, then check with `muse devices --source mindmonitor`.
5. Record with `muse run --source mindmonitor`, or pick **Via Mind Monitor** as the
   source on the web **Live** page.

Things to know:

- **Timing is reconstructed.** OSC messages carry no packet counter or device timestamp, so sample times
  are rebuilt from the sample rate and arrival times, and lost samples are only *inferred* (a loss
  shorter than about half a second can go unnoticed). The direct Bluetooth link is more exact.
- **EEG offset.** Mind Monitor's raw EEG is not centred on zero, so the source subtracts the midpoint
  automatically: 725 µV for a Muse S Athena (detected from its optics data or 8 EEG channels), 841 µV for
  classic Muse headbands. Set `eeg_offset_uv` to override it. Filtering and band power are unaffected
  either way.
- **Markers.** Mind Monitor's marker buttons arrive as markers ("Mind Monitor marker 1" … "4").
- **Port in use.** Only one program can listen on the port; close other OSC receivers first.

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
| `muse devices [--source openmuse\|mindmonitor]` | Scans for headbands and lists name, address and signal strength (RSSI). With `mindmonitor`, listens on UDP port 5000 for a few seconds and reports whether Mind Monitor is streaming. |
| `muse check [--duration 30] [--save raw.txt]` | Connects, streams briefly and reports sample rates, packet loss, battery and a rough contact check per electrode. Passes when EEG arrives at 90% or more of 256 Hz with at most 2% loss. Same as `muse-check test`. |

### Live sessions: `muse run`

Connects to the source, shows a live dashboard in the terminal and records to disk.

```
muse run [--source openmuse] [-a ANALYZER]... [-o key=value]... [--label TEXT]
         [--duration 30s|10m|1h] [--calibrate] [--no-record] [-c pipeline.toml]
```

| Option | Meaning |
|---|---|
| `--source` | `openmuse` (the headband over Bluetooth, default), `mindmonitor` (relayed by the Mind Monitor app) or `synthetic` (test signals) |
| `-a, --analyzer` | Analyzers to run (repeatable). Default: `quality`, `filter`, `baseline`, `bandpower`, `artifacts`, `describe` |
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
| `muse sessions export ID... --format csv\|edf\|session [--out PATH]` | CSV: one file per stream plus events. EDF+: one file for EEG tools (EDFbrowser, EEGLAB, MNE), with markers, artifacts and gaps as annotations. `session`: a session archive (.zip) with everything about the session, which `import` reads back. Several sessions go into one zip. `--out` can be a file or a folder. |
| `muse sessions rename ID LABEL` | Sets the label; `""` removes it. |
| `muse sessions delete ID... [--yes]` | Permanently deletes recordings and everything made from them. Asks first unless `--yes`; a session that is still recording can't be deleted. |
| `muse sessions import FILE.zip` | Adds the sessions in a session archive (from this or another computer). Sessions that already exist are skipped, never overwritten. |
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
| `muse train-probe ID... [--label-from markers\|calibration] [--out head.npz] [--min-usable 0.5]` | Trains a classifier on your labelled sessions and compares it with band power. See [section 6](#6-models-and-probes). |

### Web server: `muse serve`

```
muse serve [--host 127.0.0.1] [--port 8765] [--token-file PATH]
```

Starts the web interface and the REST/WebSocket API at `http://HOST:PORT`. Interactive API docs
are at `/docs`. By default it only listens on this computer. To use it from another device, bind
to another address (for example `--host 0.0.0.0`). A token is then required: it's created on first
start, printed once, and stored in `recordings/.muse/token`.

## 4. Web interface

Start `muse serve` and open `http://127.0.0.1:8765`. The top bar has **Live** and **Sessions**, with
**Plugins** (a reference page) on the right. Each page is arranged around the questions you ask while
using it: is the signal good, what is it doing now, how did it change, what happened. Technical detail
sits in a collapsed **Details** section at the bottom.

Every heading, field, button and table column has an **ⓘ icon**. Hover over it or tap it to read
what the element means.

If the server needs a token, the page asks for it once and remembers it in this browser.

### Live: starting a session

When no session is running, the page shows *Start a session*:

| Part | Meaning |
|---|---|
| Source | Pick one card: **Headband (Bluetooth)**, **Via Mind Monitor** or **Test signals**. A card is greyed out, with the reason, if its dependency is missing. |
| Label | Optional session name. |
| Record to disk | Save the raw data (on by default). If off, nothing is stored. |
| Advanced ▸ | Source settings (for the headband, leave *address* empty to use the strongest one in range) and which analyzers run. The defaults suit most sessions; the analyzers always run in dependency order. |

The last session is shown above the form, with a link to open it.

### Live: during a session

From top to bottom:

- **Header:** label, status, elapsed time, battery, how the data arrives (*via Bluetooth*, *via Mind
  Monitor*, *test signals*, or *replay 5× · recorded …*), and **Stop**.
- **Add marker** (type a label, press Enter or the button) and **Calibrate (2 min)**. Both are hidden
  during a replay.
- **Calibration banner:** while calibrating, a banner says when to keep your eyes open and when to close
  them, with a countdown, then confirms when the baseline is ready.
- **Electrode signal:**
  - *Signal strength*: your head seen from above (nose at the top) with each electrode where it
    touches you: TP9 and TP10 behind the ears, AF7 and AF8 on the forehead, and the reference point
    between them. Solid green is good contact, half-filled yellow noisy, an empty red ring poor
    contact, dashed grey no signal. A line underneath says what to adjust, for example *Adjust: TP9
    behind the left ear (poor contact)*.
  - *Usable channels*: the share of electrodes with good contact right now (AUX inputs aren't
    counted). A warning appears when it's under half.

  Check this first.
- **Brain waves:**
  - *Session description*: the latest plain-language paragraph (every 30 s).
  - *Share of power by band*: one column per band (delta, theta, alpha, beta, gamma) with its share
    of the power right now, its frequency range and a line showing its last 2 minutes, then the band
    with the largest share. When no electrode has good contact, it says so instead of showing numbers.
  - *Probe output*, if you use a trained probe.
- **Trends:** *Relative band power over time* (last 2 minutes), *Band power vs. eyes-open baseline* once
  you have calibrated, and *Probe output over time* with a probe.
- **Signal:** the filtered EEG traces, *Signal variation per electrode*, and the spectrum.
- **Events:** earlier descriptions, calibration, blinks, jaw clenching and movement.
- **Details** (collapsed): one chip per plugin (green working, yellow degraded, red stopped after an
  error; "dropped" counts chunks an analyzer skipped because it fell behind; the recording never drops
  data), the session id and the device.

Cards appear once they have data, so a section stays hidden until it has something to show.

### Sessions

A table of all recordings, newest first: start time, label, length, device, usable share (good
contact), data received, the first line of the description, and status. Use the search box to filter
by label or description. Click a row to open the session.

**Managing sessions.** Tick the box at the start of a row (the box in the header selects every session
shown). A bar appears above the table:

- **Rename** (one session selected): type the new label and press Enter or **Save**. Leave it empty to
  remove the label.
- **Export ▾**: *Session archive (.zip)* keeps everything about the sessions, for a backup or another
  computer; *CSV (zip)* or *EDF+* for other tools. Several sessions go into one zip.
- **Delete**: asks for confirmation, then deletes the recordings and everything made from them. This
  can't be undone, so export a session archive first if you may want them back. A session that is still
  recording can't be deleted.

**Import…** (top right) adds the sessions in a session archive. Sessions that already exist are
skipped, never overwritten, and the page says what was imported or skipped.

### Session

- **Actions:** **Replay** at 1×, 2×, 5× or 10× plays the recording on the Live page as if it were
  happening now (nothing is saved; markers and calibration are replayed; marker and calibrate
  controls are off). **Analyze** runs the analyzers over the whole recording and refreshes this page.
  **Export ▾** downloads a session archive (.zip), CSV (zip) or EDF+.
- **Summary:** length, usable share, data received, blinks per minute and alpha share, then the session
  description for the whole recording and for each marked or calibration segment. Beside it, *Signal
  strength over the session* shows each electrode's share of the session with good contact (green 80%
  or more, yellow 50–80%, red ring under 50%).
- **Timeline:** band power over time (after Analyze) and the recorded signal, on one time axis with
  markers as dashed lines. Click a segment chip (for example *eyes closed 01:30–03:00*) to zoom every
  chart to it; *Whole session* zooms back out. Pick the stream (`eeg`, `acc`, `gyro`, `optics`) on the
  right.
- **Charts:** the analyzers' charts over the whole session, such as calibration, artifacts and the
  baseline comparison.
- **Details** (collapsed): data completeness per stream (received share, rates, gaps), HTML reports,
  plugin versions, and each analyzer's end-of-session numbers.

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

List `describe` after the analyzers it reads (`quality`, `bandpower`, `artifacts`, and `cbramod` if used).
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

   Windows where fewer than half the electrodes had good contact are skipped (`--min-usable`
   changes the share), and the number skipped is shown.

   The verdict says whether the model beats band power: by at least 0.05 on average **and** in most
   held-out sessions. A single session never counts. This is the same rule the brain-wave
   descriptions use before they quote a probe. If the model doesn't pass, band power is the simpler
   and better choice for that task, and the model isn't worth running.

   With `--server`, the head is stored on the server under `recordings/.muse/probes/` and a copy
   is downloaded to `--out`.

4. **Use the probe live or offline.** Add `cbramod` with `head = "eyes.npz"` to a pipeline file
   ([section 5](#5-pipelines-toml)). *Probe output* then shows the probability per label under **Brain
   waves**, and its line over time under **Trends**.
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
| `no EEG from Mind Monitor on UDP port 5000` | Check the app's target IP is this computer's address (listed in the message), the port matches, the path prefix is empty, streaming is on, both devices share the Wi-Fi, and the firewall allows UDP 5000. |
| Connects, then drops immediately | The headband is paired in Windows; remove it there. |
| Signal strength (under Electrode signal) shows poor contact or noisy | Adjust the band so the forehead and behind-the-ear sensors touch the skin, moisten them, and sit still for a few seconds. |
| Many gaps or a low rate in the Streams table | Move closer to the computer and away from other 2.4 GHz devices. About 1–2% loss is normal. |
| `incomplete` session | The app was killed while recording. The data is readable; run `muse sessions repair ID`. |
| `cbramod` shows as `unavailable` | Run `uv sync --extra models`. |
| `weights not found ... run muse models pull` | Run `muse models pull cbramod`. |
| Web page asks for a token | Copy it from the `muse serve` output or from `recordings/.muse/token`. |
