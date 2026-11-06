# Interfaces: CLI, API, Web

All three are thin adapters over `KernelFacade`. No business logic lives in an interface. If the CLI
can do something, the API can do it too, and the other way round.

```python
class KernelFacade:
    def list_plugins(self, kind: str | None = None) -> list[PluginManifest]
    async def discover_devices(self, source: str, config: dict | None = None) -> list[DeviceInfo]
    async def start_session(self, pipeline: dict) -> SessionInfo          # live
    async def run_offline(self, session_id: str, pipeline: dict) -> SessionInfo   # replay through analyzers
    async def stop_session(self, session_id: str) -> SessionInfo
    def get_session(self, session_id: str) -> SessionInfo
    def list_sessions(self, filters) -> list[SessionInfo]
    def subscribe(self, session_id: str, streams: list[str], max_hz: float) -> AsyncIterator[Chunk | Event]
    def views(self, session_id: str) -> list[ViewSpec]
    def health(self, session_id: str) -> dict[str, Health]
    async def add_marker(self, session_id: str, label: str) -> Event
```

## 1. CLI (`muse`)

Built with Typer. Entry point `muse` (the existing `muse-check` stays as an alias of `muse check`).
Every command accepts `--json` for machine-readable output and `--server URL` to run against a remote
`muse serve` instead of in-process.

```
muse plugins list [--kind source|analyzer|sink]       # name, version, kind, status (ok/unavailable + hint)
muse plugins info NAME                                 # summary, config schema, views, streams
muse devices [--source openmuse]                       # discover() of the source plugin
muse check [--address MAC] [--preset p1041]            # existing connectivity test

muse run   [-c pipeline.toml] [--source NAME] [--analyzer NAME ...] [--no-record] [--duration 10m]
           # live session with a Rich dashboard (quality, band powers, battery, events);
           # keys: m = marker, q = stop
muse record [-c pipeline.toml] [--duration 30m]        # headless run: source + recorder only

muse sessions list [--since 7d]
muse sessions show ID
muse sessions export ID --format edf|bids|csv|fif [--out PATH]
muse sessions repair ID
muse sessions delete ID                                # asks to confirm; raw data is never deleted silently

muse analyze ID [-c pipeline.toml | --analyzer NAME ...] [--report out.html]
           # offline: replay source + analyzers + derived writer + report
muse analyze trend [--since 30d] --metric bandpower/eeg_rel:alpha   # cross-session trend

muse models pull PLUGIN [--revision REV]               # explicit model download
muse serve [--host 127.0.0.1] [--port 8765] [--token-file PATH]
muse config show | path
```

Exit codes: `0` ok, `1` runtime failure, `2` usage/config error, `3` device not found.

## 2. HTTP API

FastAPI app, versioned under `/api/v1`. The OpenAPI schema is at `/api/v1/openapi.json` and the docs at `/docs`.

### 2.1 REST

| Method & path | Body / query | Returns |
|---|---|---|
| `GET /plugins` | `?kind=` | `PluginManifest[]` (includes config JSON schema) |
| `GET /plugins/{name}` | | `PluginManifest` |
| `GET /devices` | `?source=openmuse` | `DeviceInfo[]` |
| `POST /sessions` | pipeline (JSON form of the TOML) | `201 SessionInfo` (live) |
| `GET /sessions` | `?since=&mode=&label=` | `SessionInfo[]` |
| `GET /sessions/{id}` | | `SessionInfo` + `health` |
| `POST /sessions/{id}/stop` | | `SessionInfo` |
| `POST /sessions/{id}/markers` | `{label}` | `Event` |
| `GET /sessions/{id}/views` | | `ViewSpec[]` |
| `GET /sessions/{id}/streams/{stream}` | `?from=&to=&format=json\|csv\|parquet&decimate=` | data slice |
| `GET /sessions/{id}/events` | `?type=&from=&to=` | `Event[]` |
| `POST /sessions/{id}/analyses` | pipeline (analyzers only) | `202 {job_id, session_id}` (offline run) |
| `GET /jobs/{job_id}` | | status, progress 0–1, result session id |
| `GET /sessions/{id}/reports` · `GET /reports/{rid}` | | list / HTML |
| `POST /sessions/{id}/export` | `{format}` | `202 job` → file download link |
| `GET /health` | | kernel + active sessions health |

Plugin-contributed routes are mounted under `/api/v1/x/{plugin-instance}/…`. For example, the
`android-http` source exposes `POST /api/v1/x/android-http/ingest`.

Errors use RFC 9457 problem JSON (`type`, `title`, `detail`, `plugin`).

### 2.2 WebSocket live stream

`GET /api/v1/sessions/{id}/live?streams=eeg_filtered,bandpower/eeg_rel,quality&max_hz=20`

Server → client messages:

```jsonc
{"type":"hello","session":{...SessionInfo},"views":[...ViewSpec]}
{"type":"chunk","stream":"bandpower/eeg_rel","t":[...],"data":[[...],...],"first_index":1234}
{"type":"event","event":{"type":"artifact.blink","t":1762425600.12,"source":"artifacts"}}
{"type":"health","plugins":{"openmuse":{"status":"ok","rate":{"eeg":255.8}}}}
```

- The server decimates each stream to `max_hz` using min/max envelope for traces and latest value for
  metrics. Raw full-rate data is available with `max_hz=0`.
- Optional binary mode `?encoding=msgpack` for full-rate EEG.
- Client → server: `{"type":"subscribe","streams":[...]}`, `{"type":"marker","label":"eyes closed"}`.

### 2.3 Security

- Binds to `127.0.0.1` by default.
- Binding to any other address **requires** a bearer token (`--token-file`, or one generated on first
  run and printed once). Without a token the server refuses to start.
- CORS is off by default. TLS is out of scope; use a reverse proxy.

## 3. Web UI

A static single-page app served by `muse serve` at `/`. It uses plain ES modules with no build step,
vendored uPlot for traces, and no CDN, so it works offline and leaks nothing.

| Page | Contents |
|---|---|
| **Live** | Device/connection/battery header; per-channel quality bars; all `ViewSpec`s from active analyzers in a responsive grid; marker button; start/stop |
| **New session** | Pick source → discovered devices → analyzers (forms generated from each plugin's config JSON schema) → sinks; save as pipeline preset |
| **Sessions** | Table: date, label, duration, device, gaps, quality summary; open / export / analyze |
| **Session detail** | Scrollable recorded traces with event overlay, derived views, list of offline analyses and reports |
| **Trends** | Metric chosen from derived streams across sessions (e.g. mean relative alpha per session) |
| **Plugins** | Installed plugins, status, missing extras hint, config schema |

Rendering contract: `renderers/{type}.js` exports `render(el, spec, store)`, where `store` holds a
rolling buffer per stream. Adding a new built-in view type therefore means adding one file. A plugin
can ship a `custom` view as a JS module under `<plugin package>/web/`, served at
`/plugins/{name}/assets/`.

UI copy follows the non-medical rule: "relative alpha power", not "relaxation level", unless a plugin
explicitly labels a score and documents how it was derived.
