# Interfaces

CLI, API and web are thin adapters over `KernelFacade`. Anything one interface can do, the others can.

```python
class KernelFacade:
    def list_plugins(self, kind=None) -> list[PluginManifest]
    async def discover_devices(self, source: str) -> list[DeviceInfo]
    async def start_session(self, pipeline: dict) -> SessionInfo     # live; one at a time
    async def run_offline(self, session_id: str, pipeline: dict) -> Job
    async def stop_session(self, session_id: str) -> SessionInfo
    def get_session(self, id) -> SessionInfo
    def list_sessions(self, **filters) -> list[SessionInfo]
    def subscribe(self, id, streams, max_hz) -> AsyncIterator[Chunk | Event]
    def views(self, id) -> list[ViewSpec]
    async def add_marker(self, id, label: str) -> Event
```

## 1. CLI (`muse`)

Every command takes `--json`. `--server URL` targets a running `muse serve` instead of working
in-process.

```
muse plugins list [--kind K]           # status shows "unavailable: pip install muse-monitor[models]"
muse plugins info NAME
muse devices [--source openmuse]
muse check                             # existing connectivity test

muse run [-c pipeline.toml] [--no-record] [--duration 10m] [--calibrate]
         # live Rich dashboard; keys: m = marker, q = stop
muse sessions list | show ID | export ID --format csv|edf | repair ID
muse analyze ID [-c pipeline.toml] [--report out.html]       # offline replay
muse models pull PLUGIN
muse train-probe --plugin cbramod --sessions ID... --label-from markers --out head.pt
         # cross-validation holds out whole sessions; prints per-fold scores
muse serve [--host 127.0.0.1] [--port 8765]
```

Exit codes: `0` ok, `1` runtime error, `2` usage/config error, `3` device not found.

## 2. API (`/api/v1`, OpenAPI at `/docs`)

| Method & path | Purpose |
|---|---|
| `GET /plugins` | List plugins with config schemas |
| `GET /devices?source=` | Discover devices |
| `POST /sessions` | Start a live session (pipeline as JSON) |
| `GET /sessions` · `GET /sessions/{id}` | List / detail + health |
| `POST /sessions/{id}/stop` | Stop |
| `POST /sessions/{id}/markers` | Add marker |
| `GET /sessions/{id}/views` | `ViewSpec[]` |
| `GET /sessions/{id}/streams/{stream}?from=&to=&format=json\|csv` | Data slice |
| `GET /sessions/{id}/events` | Events |
| `POST /sessions/{id}/analyses` → `GET /jobs/{id}` | Offline run |

Errors use RFC 9457 problem JSON.

**WebSocket** `GET /api/v1/sessions/{id}/live?streams=a,b&max_hz=20`:

```jsonc
{"type":"hello","session":{…},"views":[…]}
{"type":"chunk","stream":"bandpower/eeg_rel","t":[…],"data":[[…]],"first_index":1234}
{"type":"event","event":{…}}
```

The server decimates each stream to `max_hz` (min/max envelope for traces). The client can send
`{"type":"marker","label":"…"}`.

**Security.** The server binds to `127.0.0.1`. Any other bind address requires a bearer token
(generated on first run), and the server refuses to start without one. The token is required on REST
and WebSocket alike. Use a reverse proxy for TLS.

## 3. Web UI

A static page served at `/`, built from plain ES modules with vendored uPlot. There is no build step
and no CDN.

| Page | Contents |
|---|---|
| Live | Connection, battery, quality bars, analyzer views, marker button, calibrate, start/stop |
| Sessions | List; open, export, analyze |
| Session | Recorded traces with event overlay, derived views, reports |
| Plugins | Status and config forms generated from JSON schema |

There is one renderer per `ViewSpec.type` (`renderers/<type>.js`). UI copy describes measurements
("relative alpha power"), not mental states.
