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
muse sessions list | show ID | repair ID
muse sessions export ID... --format csv|edf|session [--out PATH]   # session = importable archive (.zip)
muse sessions rename ID LABEL | delete ID... [--yes] | import FILE.zip
muse analyze ID [-c pipeline.toml] [--report out.html]       # offline replay
muse models pull PLUGIN [--revision SHA]
muse train-probe ID... [--plugin cbramod] [--label-from markers|calibration] [--out head.npz]
         # cross-validation holds out whole sessions (one session: time blocks, flagged optimistic);
         # prints per-fold balanced accuracy for the probe and for band power, and a verdict
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
| `PATCH /sessions/{id}` · `DELETE /sessions/{id}` | Rename (`{label}`) / delete a recording and its derived data (409 while running) |
| `POST /sessions/{id}/stop` | Stop |
| `POST /sessions/{id}/replay?speed=` | Play a recording back as a live session (nothing recorded); body `{analyzers}` optional, default: as recorded + `describe` |
| `POST /sessions/{id}/markers` | Add marker |
| `POST /sessions/{id}/calibrate` | Start the eyes-open/closed protocol |
| `GET /sessions/{id}/views` | `ViewSpec[]` |
| `GET /sessions/{id}/streams` | Recorded streams |
| `GET /sessions/{id}/streams/{stream}?from=&to=&format=json\|csv&max_points=` | Data slice (min/max decimated) |
| `GET /sessions/{id}/events` | Events |
| `POST /sessions/{id}/analyses` → `GET /jobs/{id}` | Offline run |
| `GET /sessions/{id}/reports` · `GET /sessions/{id}/reports/{name}` | HTML reports |
| `POST /sessions/{id}/export?format=csv\|edf` | File download (CSV as zip) |
| `POST /sessions/{id}/repair` | Merge leftover parts after a crash |
| `POST /sessions/export` | `{ids, format: session|csv|edf}` → one file (several sessions: a zip, a folder each) |
| `POST /sessions/import` | Body: a session archive (.zip). Returns `{imported, skipped}`; existing sessions are never overwritten |
| `POST /models/{plugin}/pull?revision=` | Download a model plugin's pinned weights |
| `POST /probes` | Train a probe head (`{sessions, plugin, label_from, out, options}`); the head is written on the server |

Errors use RFC 9457 problem JSON.

### Session archives

A `.zip` with one folder per session (`<id>/session.json`, `raw/`, `events.jsonl`, `derived/`, `analyses/`,
`reports/`; generated `export/` files are left out) plus `MUSE-ARCHIVE.json`. Import treats it as untrusted:
every path must stay inside its session folder (no absolute paths, `..` or drive letters, checked before
anything is unpacked), the unpacked size (8 GB) and compression ratio are capped, ids must be plain names,
an existing session is skipped rather than overwritten, and a session is added only after its streams are read.

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

A static page served at `/`, built from plain scripts with a small canvas/SVG renderer. There is no build step
and no CDN. Pages are arranged by the questions a user asks: is the signal good, what is it doing now, how did
it change, what happened. Technical detail sits in collapsed **Details** sections. Every heading, field,
button and column has an ⓘ explanation; plugin views bring their own (`ViewSpec.description`).

| Page | Contents |
|---|---|
| Live: start | Source cards (Headband over Bluetooth, Via Mind Monitor, Test signals), label, record toggle; **Advanced** for source options and analyzers (always run in dependency order); link to the last session |
| Live: running | Header (label, status, elapsed, battery, how data arrives or `replay N×`, Stop); Add marker and Calibrate (hidden in replays); calibration banner with countdown; **Electrode signal** (Signal strength head map, usable share, warning when under half); **Brain waves** (latest description, band columns with 2-minute lines); **Trends**, **Signal**, **Events** sections that appear as their cards get data; **Details** (plugin health, session, device) |
| Sessions | Table: start, label, length, device, usable share, data received, first line of the description, status; search by label or description; checkboxes (and select-all) with a selection bar: Rename (one), Export (session archive, CSV, EDF+), Delete (confirmed); **Import…** for session archives |
| Session | Replay (1/2/5/10×), Analyze, Export menu (CSV, EDF+); **Summary** (key numbers, Signal strength over the session, descriptions for the whole session and each segment); **Timeline** (band power over time and a recorded stream on one axis, segment chips zoom both); **Charts** (whole-session views); **Details** (data completeness, HTML reports, plugin versions, plugin numbers) |
| Plugins | Status, hints for missing dependencies, every option with default and explanation (reference; linked at the right of the top bar) |

Views are placed by `ViewSpec.section`: `quality` → Electrode signal, `now` → Brain waves, `trends`, `signal`,
`events`. There is one render function per `ViewSpec.type`, all in `renderers.js` (also inlined into reports).
UI copy describes measurements ("share of alpha power"), not mental states.
