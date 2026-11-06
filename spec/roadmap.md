# Roadmap

## 1. MVP (M0–M2)

| # | Deliverable | Done when |
|---|---|---|
| **M0 Kernel** | SDK types and helpers, registry, bus, session manager, config, `synthetic`, `bandpower`, `muse plugins list`, `muse run` | 10 Hz synthetic sine → alpha is the top band; a failing dummy analyzer doesn't stop the session |
| **M1 Device + recording** | `openmuse`, `quality`, `filter`, `recorder`, `muse sessions` | 30 min real recording: every gap reported, EEG effective rate within 1% of 256 Hz, files readable after `kill -9` |
| **M2 Offline** | `replay`, `muse analyze`, HTML reports, `baseline` | Re-analysis on the same machine and versions matches within 1e-6; zero drops in offline mode |

## 2. After MVP

| # | Deliverable | Done when |
|---|---|---|
| **M3 API** | REST, WebSocket, `muse serve`, `--server`, token auth | All CLI commands work via `--server` |
| **M4 Web UI** | Live, Sessions, Session, Plugins pages | 4 ch EEG + views stay smooth on a mid-range laptop (measure CPU and latency here, then set targets) |
| **M5 Models** | `ModelAnalyzer`, `cbramod`, `models pull`, `train-probe` | Probe beats the band-power baseline under session-held-out CV, or the model plugin is not recommended |
| **M6 Extras** | `artifacts`, `export` (CSV/EDF+) | EDF+ opens in MNE with annotations |

## 3. Testing

| Layer | How |
|---|---|
| Kernel | Fake plugins: start/stop order, failure isolation, live drop + `gap` flag, offline flow control, cycle rejection |
| Plugins | `sdk.testing` with `synthetic` sources (known spectrum → known output) |
| Sources | Fixture captures in `samples/`; hardware tests marked `device` and skipped in CI |
| Offline | Golden test: replay fixture → derived values match stored values within tolerance |
| API / CLI | FastAPI TestClient, Typer CliRunner with `--json` |
| Web | Playwright smoke test on a synthetic session |

CI: `uv run pytest -m "not device"`, `ruff`, `pyright --strict` on `sdk/` and `kernel/`.

**Not in place yet:** CI, the Playwright smoke test, fixture captures in `samples/` (so no parser or golden replay tests from real data) and a cycle-rejection test. 65 tests run locally; the CBraMod test is skipped until `muse models pull cbramod` has run.

## 4. Status

| Milestone | State |
|---|---|
| M0, M2 | Done. Verified with synthetic data |
| M1 | Done on real hardware (Muse S Athena, fw 3.1.19, preset p1041, 2026-11-06). 30 min recording: EEG sample clock 256.70 Hz (+0.27%), 1.35% of EEG samples lost in 1,105 gaps, every gap reported (index gaps = `stream.gap` events = missing counts, no time jumps without an index gap; same for motion and optics), no disconnects. Delivered EEG rate 253.2 Hz because of that loss. Readable after a forced kill (`taskkill /F /T`): all streams replay, ~1 s lost, `sessions show` rebuilds stats from files. Fixed on hardware: settle backlog decoded out of order; CLI crash on non-UTF-8 consoles; EEG carried a +725 µV offset (OpenMuse scales 0–1450 µV), now centred on zero (`openmuse` 1.1, conversion stored in stream meta; older recordings keep the offset). **Open:** signal quality with the headband worn |
| M3 | Done. Every CLI command was run against `muse serve` with `--server`, including `models pull` and `train-probe` |
| M4 | Done. Smoke-tested in Chromium; CPU/latency targets not measured yet |
| M5 | Built: `ModelAnalyzer`, `cbramod` (pinned weights, ~18 ms per 4 s window on CPU), `models pull`, `train-probe` (probe vs band power, session-held-out CV), `synthetic` `script` option for labelled test data. Verified end to end from the CLI, locally and with `--server`, on scripted synthetic sessions, where band power wins as expected (1.00 vs 0.92). **Pending:** the acceptance check on real labelled recordings (e.g. 3+ sessions with `--calibrate` or markers) |
| M6 | Done. `artifacts`, CSV and EDF+ export. The 30 min real recording's EDF+ opens in MNE 1.13 without warnings: 30 channels, units `uV`, 2,673 annotations (gaps, blinks, jaw); every channel matches the raw data within 16-bit rounding up to the last sample; markers land at their exact times. Fixed while verifying: samples were placed by counter at the nominal rate, which drifted ~5 s from the annotations over 30 min (device clocks run off nominal), so the export now resamples by timestamp; µ was written as `?` in the unit field; `--out` with a folder failed. Regression test reads the EDF+ back with MNE (`mne` is a dev dependency) |
| Describe | Done (2026-11-06). `describe` analyzer + `text` view type: plain-language descriptions every 30 s and per segment in the report, quality-gated, artifact-aware, probe quoted only if validated (held-out margin ≥ 0.05, most sessions); banned-word check in code and tests. Verified via CLI, report and web Session page on scripted synthetic sessions; not yet read on real recordings |
| Replay | Done (2026-11-06). Session page **Replay** (1/2/5/10×) plays a recording back as a live session (no recording; analyzers as recorded + `describe`), so the live views and descriptions can be watched on old data. `POST /sessions/{id}/replay`. Verified in Chromium |
| Mind Monitor | Done (2026-11-06). `mindmonitor` source: OSC over UDP (port 5000), EEG/acc/gyro/PPG/optics, battery, `/Marker/N` → markers; times rebuilt from rate + arrival; EEG gaps inferred from timing; rates measured at start. Tested with a scripted OSC sender on localhost (unit, CLI, discovery). **Pending:** a run with the real app and headband; Athena-specific paths, battery units and EEG offset are unconfirmed (vendor spec blocks automated access) |
| Web UI layout | Done (2026-11-06). Pages arranged by question: Live = signal check, right now, trends, signal, events, details (collapsed); calibration banner with countdown; cards appear with data. Start form: source cards, Advanced for options/analyzers (dependency order enforced). Session: summary (key numbers + descriptions), timeline with segment zoom, charts, details. Sessions: usable %, data received, description, search. Plugins place views via `ViewSpec.section`. Verified in Chromium at desktop and phone width |
| Docs | Done. [`docs/getting-started.md`](../docs/getting-started.md) (concepts, first session), [`docs/usage.md`](../docs/usage.md) (CLI, web UI, pipelines, troubleshooting) and [`docs/pending.md`](../docs/pending.md) (step-by-step instructions to complete M5) |

**Next:** follow [`docs/pending.md`](../docs/pending.md): record 3+ calibrated sessions with the headband worn, which closes the M1 signal-quality check and gives M5 its real acceptance data; measure M4 CPU/latency during one of them.

## 5. Decided

- **Device link:** direct BLE via OpenMuse (`openmuse` source), the default. Alternative: the `mindmonitor` source (Mind Monitor app → OSC over UDP), added 2026-11-06. Other phone apps and HTTP ingestion are out of scope.
- **Built-in model:** CBraMod.

## 6. Open questions

1. **Persist live derived streams?** Proposed default: no, regenerate them offline.
