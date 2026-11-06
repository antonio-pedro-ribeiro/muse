# Muse Monitor

Record, watch and analyze EEG from a **Muse S Athena** headband on your own computer. Muse Monitor
connects to the headband directly over Bluetooth (or through the Mind Monitor phone app), shows the signal
live in your browser or terminal, saves every session losslessly, and describes what it measured in plain
language.

> Muse Monitor is not a medical device. Its charts and descriptions are measurements of an electrical
> signal from a consumer headband; they don't diagnose anything or describe states of mind.

## What it does

- **Live view.** See whether each electrode touches the skin (a head map with what to adjust), how the
  EEG power is spread over the classic frequency bands, the raw and filtered traces, and blinks, jaw
  clenching and movement.
- **Plain-language descriptions.** Every 30 seconds and for the whole session, a short description of the
  measurements, compared with your own eyes-open baseline once you calibrate.
- **Lossless recording.** Raw samples with timestamps, device and preset metadata, in Parquet. Lost
  Bluetooth packets are detected and reported, never filled in. Files stay readable after a crash.
- **Look back.** Replay a session live at up to 10×, re-analyze it, zoom into marked segments, and get a
  one-page HTML report.
- **Manage sessions.** Rename, delete, export (CSV, EDF+ or an importable session archive) and import.
- **Optional AI model.** Train a small classifier on top of the CBraMod EEG foundation model with your
  own labelled sessions; the app tells you honestly whether it beats plain band power.
- **Three ways to drive it:** a web interface, a command line (`muse`) and a REST/WebSocket API.

Everything stays on your computer. The only download is the optional AI model, and only when you ask for it.

## Quick start

You need Python 3.13 and [uv](https://docs.astral.sh/uv/). Developed and tested on Windows 11.

```
uv sync                          # install
uv run muse check                # 10 s connection test with the headband on
uv run muse serve                # web interface at http://127.0.0.1:8765
```

Before connecting: close the Muse phone app, and don't pair the headband in Windows Bluetooth settings
(the app connects without pairing).

No headband at hand? Choose **Test signals** in the web interface, or run
`uv run muse run --source synthetic --duration 1m`.

More commands:

```
uv run muse run --calibrate --duration 10m     # record with a live terminal dashboard
uv run muse sessions list                      # recorded sessions
uv run muse analyze <ID>                       # re-analyze a session, write an HTML report
uv run muse sessions export <ID> --format edf  # EDF+ for EDFbrowser, EEGLAB or MNE
uv run muse run --source mindmonitor           # data relayed by the Mind Monitor app (OSC, UDP 5000)
```

## Documentation

| Document | For |
|---|---|
| [docs/getting-started.md](docs/getting-started.md) | New users: concepts, and your first session step by step |
| [docs/usage.md](docs/usage.md) | Every command, option and page of the web interface; pipeline files; troubleshooting |
| [docs/pending.md](docs/pending.md) | How to complete the open AI-model milestone with your own recordings |
| [spec/architecture.md](spec/architecture.md) | Goals, principles, architecture and decisions |
| [spec/plugins.md](spec/plugins.md) · [spec/data.md](spec/data.md) · [spec/interfaces.md](spec/interfaces.md) | Plugin contracts, data and storage, CLI/API/web interfaces |
| [spec/roadmap.md](spec/roadmap.md) | Milestones and their status |
| [AGENTS.md](AGENTS.md) | Device facts and engineering conventions for contributors and AI agents |

## How it is built

A small kernel loads **plugins** and routes data between them:

```
 source ──▶ analyzers ──▶ sinks            CLI · REST/WebSocket API · web UI
 openmuse     quality       recorder              │
 mindmonitor  filter        export                ▼
 synthetic    bandpower                       KernelFacade
 replay       artifacts, baseline,
              describe, cbramod
```

Analyzers describe their charts declaratively, so the web UI, the terminal and the HTML reports all draw
them without plugin-specific code. Offline analysis replays a recording through the same pipeline, so live
and offline results agree. You can add your own plugins as Python files in a `plugins/` folder; see
[spec/plugins.md](spec/plugins.md).

Stack: Python 3.13, uv, NumPy, SciPy, pydantic, Typer + Rich, FastAPI, PyArrow, OpenMuse/bleak for
Bluetooth, python-osc for Mind Monitor, and a plain-JavaScript web UI with no build step. The optional AI
model needs `uv sync --extra models` (PyTorch, braindecode).

## Status

Working on real hardware (Muse S Athena, firmware 3.1.19): direct Bluetooth recording with gap detection,
live monitoring, offline analysis, reports, EDF+ export verified in MNE, Mind Monitor input, and session
management. Still open: checking the AI model against band power on real labelled recordings, and measuring
web UI performance. See [spec/roadmap.md](spec/roadmap.md).

## Development

```
uv sync --extra models           # include the AI model dependencies
uv run pytest                    # the test suite (hardware tests are marked `device` and skipped)
```

Tests run on synthetic signals with known content (a 10 Hz sine must show up as alpha) and on real
recordings where noted. Conventions, device facts and lessons learned are in [AGENTS.md](AGENTS.md).

## Privacy

EEG is personal data. Recordings are stored only in the local `recordings/` folder, which is excluded from
git. The headband's serial number is stored as a salted hash. The web server listens only on this
computer unless you bind it elsewhere, and then requires a token.

## Acknowledgements

Built on [OpenMuse](https://github.com/DominiqueMakowski/OpenMuse) for the Athena Bluetooth protocol,
[CBraMod](https://huggingface.co/braindecode/cbramod-pretrained) via
[braindecode](https://braindecode.org) for the optional model, and works with
[Mind Monitor](https://mind-monitor.com/). Not affiliated with Interaxon (makers of Muse).

## License

No license has been chosen yet.
