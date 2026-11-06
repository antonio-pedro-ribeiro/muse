# Muse Monitor: Application Spec

Status: **draft v0.1** · Scope: architecture and contracts for the `muse-monitor` app.

Files in this folder:

| File | Contents |
|---|---|
| [README.md](README.md) | Goals, principles, architecture overview (this file) |
| [plugins.md](plugins.md) | Plugin kinds, Python contracts, discovery, lifecycle, built-in plugins |
| [data.md](data.md) | Data model, timing, storage layout, export formats |
| [interfaces.md](interfaces.md) | CLI, HTTP/WebSocket API, web UI |
| [roadmap.md](roadmap.md) | Milestones, testing strategy, acceptance criteria, open questions |

## 1. What the app does

Connects to an EEG device **through a source plugin**, runs the data through **analyzer plugins**
(signal logic, ML models, visualizations), and sends results to **sink plugins** (recording, streaming
out). Users drive it from a **CLI**, a **REST/WebSocket API**, or a **web UI**. All three use the same
kernel facade.

The first target device is the Muse S Athena (see `AGENTS.md`), but the kernel knows nothing about Muse.

## 2. Design principles

1. **Microkernel.** The kernel only does plugin loading, stream routing, session lifecycle, config and
   errors. Every device, algorithm, model, recorder and visualization is a plugin. This includes the
   built-in ones, which are registered the same way third-party plugins are.
2. **Three plugin kinds only**: `source`, `analyzer`, `sink`. Nothing else is needed (see §4).
3. **One code path for live and offline.** Offline analysis means replaying a recording through the same
   pipeline (the `replay` source), at full speed. Analyzers never have separate offline code unless they
   choose to add a summary step.
4. **Declarative visualizations.** Analyzers describe *what* to show (`ViewSpec`). The web UI and CLI
   decide *how* to render it. Most plugins ship no frontend code.
5. **Raw data is sacred.** The recorder stores unmodified source data. Derived data goes in separate
   files and can be regenerated from raw data plus plugin name, version and config.
6. **Nothing hard-coded about channels.** Channel names, counts, units and rates come from `StreamInfo`
   declared at runtime.
7. **Fail locally.** A crashing plugin is stopped and reported; the kernel and other plugins keep running.
8. **Local first.** Server binds to `127.0.0.1` by default. No data leaves the machine unless a sink
   plugin the user enabled sends it.
9. **Not medical.** No output is worded as a diagnosis.

## 3. Architecture

```
            ┌──────────── interfaces (thin adapters) ────────────┐
            │   CLI (Typer)     REST + WebSocket (FastAPI)   Web UI│
            └───────────────────────┬────────────────────────────┘
                                    │  KernelFacade (single Python API)
┌───────────────────────────────────▼──────────────────────────────────────┐
│ KERNEL                                                                   │
│  PluginRegistry   SessionManager   StreamBus (pub/sub)   Config   Errors │
└──────┬─────────────────────────┬───────────────────────────┬─────────────┘
       │ Chunk/Event             │ Chunk/Event               │ Chunk/Event
┌──────▼──────┐  streams  ┌──────▼───────┐   streams  ┌──────▼──────┐
│ SOURCE      │ ────────▶ │ ANALYZER(s)  │ ─────────▶ │ SINK(s)     │
│ openmuse    │           │ quality      │            │ recorder    │
│ android-http│           │ bandpower    │            │ lsl-out     │
│ osc, replay │           │ artifacts    │            │ webhook     │
│ synthetic   │           │ model (REVE…)│            │             │
└─────────────┘           └──────────────┘            └─────────────┘
```

### 3.1 Kernel components

| Component | Responsibility | Not responsible for |
|---|---|---|
| `PluginRegistry` | Discover plugins (entry points + `plugins/` dir), validate manifest and `api_version`, build config models | Importing heavy deps before a plugin is used |
| `StreamBus` | Topic-based pub/sub of `Chunk`/`Event` keyed by stream id; per-subscriber bounded queues; backpressure policy | Any signal processing |
| `SessionManager` | Build a pipeline from config, start/stop plugins in dependency order, own session id + metadata, collect health | Storage format (the recorder sink owns it) |
| `Config` | Load TOML pipeline files and app settings; validate each plugin's section against its pydantic model | |
| `KernelFacade` | Stable async API used by all interfaces (`list_plugins`, `start_session`, `stop_session`, `subscribe`, `run_offline`, …) | Rendering or transport |

The kernel targets **≤ 1,500 lines** of code and depends only on `numpy`, `pydantic` and the standard library.

### 3.2 Runtime model

- Single `asyncio` event loop per process.
- Plugins are `async`. CPU-heavy work (Welch, model inference) goes through `ctx.run_blocking(fn, …)`,
  which uses a thread pool by default and a process pool if the plugin declares `cpu_bound = True`.
- **Backpressure:**
  - `sink` subscribers marked `lossless` (the recorder) get unbounded queues that spill to disk. They
    never drop data.
  - Analyzers get bounded queues. When one overflows, the kernel drops the oldest chunks, increments
    `dropped_chunks` and emits a `kernel.overflow` event. It never blocks the source.
  - Live viewers (WebSocket) get latest-wins queues.
- The pipeline is a DAG. Analyzers may consume other analyzers' outputs, and cycles are rejected at
  session build time.

### 3.3 Package layout

```
src/muse_monitor/
  sdk/            # public, versioned plugin API: base classes, types, ViewSpec, testing kit
  kernel/         # registry.py, bus.py, session.py, config.py, facade.py, errors.py
  interfaces/
    cli/          # Typer app (entry point `muse`)
    http/         # FastAPI app: REST, WebSocket, plugin-contributed routes
    web/          # static web UI (no build step), served by http/
  plugins/        # built-in plugins, one subpackage each, registered via entry points
spec/             # this folder
```

Third-party plugins import **only** `muse_monitor.sdk`. Anything outside `sdk` is private.

## 4. Why only three plugin kinds

| Concern | Covered by |
|---|---|
| Device / transport (BLE, HTTP from Android, OSC, LSL, file) | `source` |
| Filters, features, quality, artifacts, ML models, scores | `analyzer` |
| Visualizations | `analyzer.views()` returning `ViewSpec`s (+ optional custom JS asset) |
| Recording, export, streaming out, alerts | `sink` |
| Extra API endpoints (e.g. Android ingest) | Any plugin may register routes via `ctx.http` |
| Extra CLI commands | Any plugin may register commands via `ctx.cli` (optional) |

## 5. Tech stack

Python 3.13 (matches `.python-version`), `uv` for packaging.

| Need | Choice | Reason |
|---|---|---|
| Arrays | numpy | Ecosystem standard |
| DSP | scipy.signal | Welch, IIR filters |
| Config/validation | pydantic v2 + `tomllib` | Typed plugin config, JSON schema for the UI for free |
| CLI | Typer + Rich | Typed commands, live terminal tables |
| API | FastAPI + uvicorn | Async, WebSocket, OpenAPI generated |
| Storage | pyarrow (Parquet) | Columnar, append in row groups, open format |
| Web UI | Plain ES modules + uPlot (vendored) | No Node build step; uPlot handles 256 Hz × 8 ch traces cheaply |
| Interop (extras) | mne, mne-bids, pyedflib, pylsl | EDF+/BIDS export, LSL |
| Models (extras) | torch, transformers, braindecode | REVE / ZUNA / CBraMod wrappers |

Heavy dependencies are optional extras (`muse-monitor[models]`, `[lsl]`, `[bids]`). If a plugin's
dependencies are missing, it is listed as `unavailable` with the install hint instead of crashing.
