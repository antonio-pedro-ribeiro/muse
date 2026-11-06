# Roadmap, testing, acceptance

## 1. Milestones

Each milestone is shippable and leaves `main` green.

| # | Deliverable | Done when |
|---|---|---|
| **M0 Kernel** | `sdk` types + base classes, `PluginRegistry`, `StreamBus`, `SessionManager`, TOML config, `synthetic` source, `bandpower` analyzer, `muse plugins list`, `muse run --source synthetic` with Rich dashboard | 10 Hz synthetic sine shows as max alpha; crash in a dummy analyzer doesn't stop the session |
| **M1 Device + recording** | `openmuse` source (port `check.py`), `quality`, `filter`, `recorder` sink, `muse record`, `muse sessions list/show` | 30 min real recording: 0 unreported gaps, EEG effective rate within 1% of 256 Hz, files readable after `kill -9` |
| **M2 Offline** | `replay` source, `muse analyze`, derived writer, HTML report via `summarize()` | Analyzing a recording twice yields byte-identical derived Parquet |
| **M3 API** | FastAPI REST + WebSocket, `muse serve`, `--server` mode in CLI, token auth | Every CLI command works with `--server`; OpenAPI schema committed and checked in CI |
| **M4 Web UI** | Live, Sessions, Session detail, Plugins pages; schema-generated config forms | Live view sustains 8 ch × 256 Hz + 5 views at < 25% of one CPU core in Chromium |
| **M5 Models** | `ModelAnalyzer` base, `reve` (or `zuna`) plugin, `muse models pull`, probe-head support | Embeddings stream at ≥ 1 Hz on CPU with a 4 s window; result reproducible with a fixed revision |
| **M6 Interop** | `export` (EDF+/BIDS/CSV/FIF), `lsl-out`, `artifacts`, Trends page | Exported BIDS passes `bids-validator`; EDF+ opens in MNE with annotations |
| **M7 Android** | `android-http` (or `osc`) source once the format questions in `AGENTS.md` are answered | Fixture captured in `samples/` drives parser unit tests |

## 2. Testing strategy

| Layer | How |
|---|---|
| SDK types | Unit tests: chunk validation, gap/duplicate detection, Windower timestamps |
| Kernel | Unit tests with fake plugins: start/stop order, failure isolation, backpressure (slow analyzer drops, recorder never drops), DAG cycle rejection |
| Plugins | `sdk.testing.check_plugin` + behaviour tests with the `synthetic` source (known spectrum → known band power) |
| Sources | Parser tests against fixture captures in `samples/` (anonymized); `openmuse` hardware test marked `@pytest.mark.device`, skipped in CI |
| API | `httpx` + FastAPI TestClient; WebSocket tests with synthetic source; OpenAPI snapshot test |
| CLI | Typer `CliRunner` with `--json` output assertions |
| Web | Playwright smoke test: start synthetic session, see views update, add marker, stop |
| Reproducibility | Golden test: replay fixture → derived Parquet hash matches stored hash |

CI runs `uv run pytest -m "not device"`, `ruff`, and `pyright` on `sdk/` and `kernel/` in strict mode.

## 3. Non-functional requirements

| Requirement | Target |
|---|---|
| End-to-end live latency (device → web chart) | p95 < 250 ms on LAN |
| Recording loss | 0 samples dropped by the app; device/BLE gaps detected and logged 100% |
| Kernel overhead | < 5% of one core at 8 ch EEG + IMU + optics |
| Startup | `muse plugins list` < 500 ms (lazy imports) |
| Platforms | Linux, macOS, Windows (BLE via bleak) |

## 4. Open questions

1. **Source of truth for the device link.** `AGENTS.md` describes an Android → HTTP path, but the
   existing `check.py` connects directly over BLE with OpenMuse. The plugin design supports both. Which
   one is primary for M1?
2. Multiple simultaneous sources (e.g. a Muse plus a chest-strap ECG). Defer to after M4?
3. Model licenses: REVE is "other" and LUNA is CC-BY-ND, which may limit redistributing fine-tuned
   heads. Default to ZUNA (Apache-2.0) or CBraMod (BSD-3) for the built-in model plugin?
4. Should derived streams from live sessions be persisted by default, or only on request? (Default
   proposed: no, regenerate offline.)
5. Do we want a plugin sandbox (subprocess per plugin) for untrusted third-party plugins? This is not
   planned for v1: plugins run in-process and are trusted like any Python package.
