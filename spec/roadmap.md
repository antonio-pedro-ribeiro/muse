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
| **M5 Models** | `ModelAnalyzer`, `zuna` or `cbramod`, `models pull`, `train-probe` | Probe beats the band-power baseline under session-held-out CV, or the model plugin is not recommended |
| **M6 Extras** | `artifacts`, `export` (CSV/EDF+), `android-http` | Fixture capture in `samples/` drives parser tests |

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

## 4. Open questions

1. **Device link (blocks M1 scope).** `AGENTS.md` describes Android → HTTP, while `check.py` uses
   direct BLE via OpenMuse. Proposal: `openmuse` first, `android-http` once the format is known.
2. **Default model.** ZUNA (Apache-2.0) or CBraMod (BSD-3). REVE (license "other") and LUNA
   (CC-BY-ND) are excluded from the built-ins unless their licenses permit it.
3. **Persist live derived streams?** Proposed default: no, regenerate them offline.
