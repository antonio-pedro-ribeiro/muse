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

## 4. Status

| Milestone | State |
|---|---|
| M0–M2 | Done. Verified with synthetic data; the 30 min real-device check for M1 is still to do |
| M3 | Done. Every CLI command was run against `muse serve` with `--server` |
| M4 | Done. Smoke-tested in Chromium; CPU/latency targets not measured yet |
| M5 | Not started (needs real recordings to evaluate the probe) |
| M6 | `artifacts` and CSV/EDF+ export done; EDF+ not yet opened in MNE |

## 5. Decided

- **Device link:** direct BLE via OpenMuse (`openmuse` source). Phone/HTTP ingestion is out of scope.
- **Built-in model:** CBraMod.

## 6. Open questions

1. **Persist live derived streams?** Proposed default: no, regenerate them offline.
