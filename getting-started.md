# Getting started with muse-monitor

muse-monitor lets you watch your brain activity live while wearing a **Muse S Athena** headband,
record it, and look back at it later. It runs on your own computer, connects to the headband
directly over Bluetooth, and keeps every recording on your disk.

This guide explains what the app does and the ideas behind it, then walks you through your first
session. For every command and option, see [usage.md](usage.md).

> **Please note:** the Muse is a consumer device, and muse-monitor is not a medical tool. The
> charts and numbers are measurements of an electrical signal. They don't diagnose anything, and
> they don't tell you what you're thinking or feeling.

## What you can do with it

- **Watch live.** See your EEG (brain signal), how well each sensor touches your skin, and how much
  of your brain activity falls into the classic rhythms such as alpha or beta.
- **Record.** Every session is saved exactly as it came from the headband, with the time, the
  device and its settings, so nothing is lost or silently changed.
- **Mark moments.** Add labelled markers ("eyes closed", "started meditating") while you record,
  so you can find those moments later.
- **Look back.** Re-analyze any recording, get a one-page HTML report, and compare sessions.
- **Export.** Take your data to a spreadsheet (CSV) or to research tools such as EEGLAB or MNE
  (EDF+).
- **Experiment with AI.** Optionally, let a pretrained EEG model learn to tell your own labelled
  states apart. The app checks honestly whether that does better than simple band power.

## How it works

```
 Muse headband ──Bluetooth──▶  source  ──▶  analyzers  ──▶  charts in the terminal or browser
                                  │            │
                                  └────────────┴─────────▶  recorder ──▶  files on your disk
```

The app is built from small **plugins** of three kinds:

| Kind | Role | Examples |
|---|---|---|
| **Source** | Where data comes from | `openmuse` (your headband), `synthetic` (made-up test signals), `replay` (an earlier recording) |
| **Analyzer** | Turns the signal into something useful, and describes the charts to show | signal quality, filtering, band power, blinks and movement, the CBraMod AI model |
| **Sink** | Where data goes | `recorder` (saves to disk), `export` (CSV/EDF+) |

A **pipeline** is one source, a set of analyzers and a set of sinks. The defaults are sensible, so
most of the time you don't need to choose.

The same pipeline runs live and on old recordings. When you re-analyze a session, the app replays
it through the analyzers exactly as if it were happening again. That's why live results and later
results agree.

You can control everything in two ways:

- the **command line** (`muse …`), with a live dashboard in the terminal; or
- the **web interface** (`muse serve`), in your browser with interactive charts. Every element has
  an ⓘ icon that explains it.

## Key ideas

**Session.** One recording, from start to stop. It has an id, an optional label, a status and its
data. Statuses: *running*, *stopped* (ended normally), *failed* (ended by an error) and
*incomplete* (the app was closed while recording; the data is still readable).

**Channels and sensors.** The headband has EEG electrodes behind the ears (TP9, TP10) and on the
forehead (AF7, AF8). It also has extra inputs, optical sensors that track blood flow, and motion
sensors (accelerometer and gyroscope). The app reads whichever sensors the headband sends.

**Signal quality.** EEG signals are tiny, millionths of a volt, so good skin contact matters more
than anything. The quality analyzer rates each electrode as good, poor contact, noisy or flat.
Check it first: if contact is poor, the other numbers aren't meaningful.

**Band power.** Brain activity is often described by rhythms in frequency bands:

| Band | Frequency | Often associated with |
|---|---|---|
| Delta | 1–4 Hz | Deep sleep; also inflated by blinks and movement |
| Theta | 4–8 Hz | Drowsiness, some meditative states |
| Alpha | 8–13 Hz | Relaxed wakefulness; typically rises when you close your eyes |
| Beta | 13–30 Hz | Active thinking and focus; also muscle tension |
| Gamma | 30–45 Hz | Hard to measure on a headband; mostly muscle activity |

These associations are general patterns from research, not readings of your state. The easiest
change to see for yourself is alpha going up when you close your eyes.

**Calibration (baseline).** Everyone's signal is different. A 2-minute calibration (60 s eyes open,
then 60 s eyes closed) gives the app your personal baseline. Band power is then also shown relative
to it, for example "1.4× your baseline".

**Markers.** Labelled timestamps you add during a session. They appear as dashed lines in charts,
are kept in exports, and are what the AI probe learns from.

**Artifacts.** Blinks, jaw clenches and head movement produce large signals that aren't brain
activity. The app detects them and lists them as events, so you can tell them apart from real
changes.

**Gaps.** Bluetooth sometimes loses a few packets. The app never hides or fills these in: it
records exactly where samples are missing and reports it. About 1–2% loss is normal.

**Raw data is never changed.** Recordings store the signal exactly as it arrived. Filtering,
analysis and reports are separate results that you can recreate at any time.

**Models and probes (optional).** CBraMod is an EEG model pretrained on a large public dataset. It
turns each few seconds of signal into a compact description. A **probe** is a small classifier you
train on your own labelled sessions, for example eyes open versus eyes closed. When you train one,
the app also trains the same kind of classifier on plain band power and tells you which one does
better on sessions it hasn't seen. If the model doesn't win, you don't need it.

## Your first session

### 1. Install

You need Python 3.13 and [uv](https://docs.astral.sh/uv/). In the project folder:

```
uv sync
```

### 2. Prepare the headband

- Charge it and turn it on.
- **Close the Muse phone app.** The headband only accepts one connection at a time.
- **Don't pair it in Windows Bluetooth settings.** If it's already paired, remove it there. The
  app connects without pairing.
- Put it on: the forehead sensors on your skin and the soft sensors behind your ears. Moistening
  the sensors slightly helps.

### 3. Check the connection

```
uv run muse check
```

After about 10 seconds you'll see sample rates, packet loss and battery, and **PASS** if the
connection is good. No headband yet? You can try everything with test signals by adding
`--source synthetic` to the commands below.

### 4. Record with the web interface

```
uv run muse serve
```

Open **http://127.0.0.1:8765** in your browser, then:

1. On the **Live** page, keep the defaults and press **Start session**.
2. Look at **Signal quality** first. Adjust the headband until most electrodes are good.
3. Press **Calibrate (2 min)** and follow the prompts: eyes open, then eyes closed.
4. Watch **Relative band power**: alpha usually rises while your eyes are closed.
5. Type a label and press **Add marker** whenever something changes.
6. Press **Stop** when you're done.

Prefer the terminal? `uv run muse run --calibrate --duration 10m` does the same with a text
dashboard. Press `m` for a marker and `q` to stop.

### 5. Look back

1. Open the **Sessions** page and click your session.
2. Press **Analyze offline** to recalculate everything over the whole session and create a report.
3. Browse the recorded signal, the stream statistics and the charts. Open the HTML report, or
   **Export** the data as CSV or EDF+.

## Where to go next

- **[usage.md](usage.md):** every command, option and page in detail, pipeline files, troubleshooting.
- **Try the AI model:** run `uv sync --extra models` and `uv run muse models pull cbramod`, record
  a few calibrated sessions, then run `uv run muse train-probe ID1 ID2 ID3 --label-from calibration`.
- **[spec/](spec/):** how the app is designed, for anyone who wants to write their own plugin.

## Your data and privacy

Recordings stay on your computer, in the `recordings/` folder. Nothing is uploaded. The only
download the app ever makes is the AI model, and only when you run `muse models pull`. The
headband's serial number is stored as a one-way hash. EEG is personal data, so think twice before
sharing recordings, and keep `recordings/` out of shared folders and version control.
