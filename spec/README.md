# Muse Monitor: Application Spec

Status: **draft v0.3**

| File | Contents |
|---|---|
| [README.md](README.md) | Goals, principles, architecture, decisions |
| [plugins.md](plugins.md) | Plugin contracts, config, built-ins, example |
| [data.md](data.md) | Types, time, storage, export |
| [interfaces.md](interfaces.md) | CLI, API, web UI |
| [roadmap.md](roadmap.md) | Milestones, testing, open questions |

## 1. What it does

A **source plugin** connects to an EEG device. **Analyzer plugins** (logic, ML models, chart
descriptions) process the data. **Sink plugins** record it. A CLI, a REST/WebSocket API and a web UI
drive it through one kernel facade. The first device is the Muse S Athena, but the kernel knows
nothing about Muse.

## 2. Principles

1. **Microkernel.** The kernel only loads plugins, routes streams, runs sessions and validates config.
   Everything else is a plugin, built-ins included.
2. **Three plugin kinds**: `source`, `analyzer`, `sink`.
3. **One code path.** Offline analysis replays a recording through the same pipeline.
4. **Declarative views.** Analyzers describe charts (`ViewSpec`); interfaces render them.
5. **Raw data is immutable.** Derived data can be regenerated from raw data plus plugin version and config.
6. **Nothing hard-coded about channels.** Names, units, rates and positions come from `StreamInfo`.
7. **Fail locally.** A failing analyzer is stopped and reported. A failing source or recorder stops the
   session, because continuing would lose data silently.
8. **Local first, not medical.** Binds to `127.0.0.1`, no telemetry, no diagnostic wording.

## 3. Architecture

```
      CLI (Typer)        REST + WebSocket (FastAPI)        Web UI (static)
           └──────────────────────┬──────────────────────────────┘
                            KernelFacade
┌─────────────────────────────────▼───────────────────────────────┐
│ KERNEL   PluginRegistry · StreamBus · SessionManager · Config    │
└───────┬───────────────────────┬──────────────────────┬──────────┘
   SOURCE ──streams──▶ ANALYZER(s) ──streams──▶ SINK(s)
   openmuse (BLE)      quality, filter,          recorder
   replay, synthetic   bandpower, artifacts,     export
                       baseline, cbramod
```

- **PluginRegistry** discovers plugins (entry points and `./plugins/`), checks `api_version`, and
  imports a plugin's dependencies only when it's used.
- **StreamBus** is a pub/sub of `Chunk`/`Event` per stream id, with one bounded queue per subscriber.
- **SessionManager** builds the plugin DAG from config (cycles are rejected) and starts sinks, then
  analyzers, then the source. It stops them in reverse order. Only **one live session** runs at a time.
- **KernelFacade** is the async API that every interface uses.

The kernel depends only on `numpy` and `pydantic`, with a target of ≤ 1,500 lines of code.

### Runtime

- One `asyncio` loop. Each plugin with `cpu_bound = True` gets a **dedicated worker thread**; numpy
  and torch release the GIL, so threads run in parallel.
- **Flow control:**

  | Mode | Analyzer queue full | Recorder queue full |
  |---|---|---|
  | live | drop oldest; analyzer receives `gap=True` on next chunk; `kernel.overflow` event | session fails loudly |
  | offline | source waits (never drops) | source waits |

### Layout

```
src/muse_monitor/
  sdk/          # public plugin API: types, base classes, helpers, testing kit
  kernel/       # registry, bus, session, config, facade
  interfaces/   # cli/, http/, web/
  plugins/      # built-ins, registered via entry points like third-party plugins
```

Plugins import only `muse_monitor.sdk`.

## 4. Stack

Python 3.13, uv, numpy, scipy, pydantic v2, `tomllib`, Typer + Rich, FastAPI + uvicorn, pyarrow,
uPlot (vendored). Optional extras: `[models]` (torch, braindecode/transformers), `[edf]` (pyedflib).
A plugin whose extra isn't installed is listed as `unavailable` with an install hint.

## 5. Decisions

| Decision | Why | Rejected |
|---|---|---|
| Python | MNE, SciPy, OpenMuse, braindecode all in Python | Rust (muse-rs): smaller ML ecosystem |
| In-process plugins | Simple, fast, debuggable; plugins are trusted like any package | Subprocess per plugin: IPC cost, more code |
| Parquet for storage | Open, columnar, append in row groups, readable after a crash | HDF5: corruption risk on crash; CSV: size, no types |
| Worker thread per CPU-bound plugin | Keeps state (filters, model weights) in place | Process pool: state can't move cheaply, chunks get copied |
| Declarative views | One description for web, CLI and reports | Per-plugin frontend code |
| Replay for offline | One code path, live and offline results agree | Separate batch API |
| Direct BLE via OpenMuse | Already working in `check.py`; one less hop, real packet counters for gap detection | Android app → HTTP: extra app, unknown format, more latency |
| CBraMod as the built-in model | 4.9M params (CPU real time), BSD-3, already in braindecode | ZUNA: 382M params, unproven on 4 dry channels; REVE/LUNA: restrictive licenses |
| No plugin HTTP routes or ingest hook | Nothing needs push ingestion with BLE; kernel stays framework-free | `ctx.http`; add an ingest hook only if a push source appears |

## 6. SDK compatibility

`api_version` is an integer. Within a version, changes are additive only (new optional fields and
helpers). Removing or changing behaviour bumps the version. The kernel loads plugins with
`api_version <=` its own and refuses newer ones with a clear message.
