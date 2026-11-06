# Plugins

## 1. Common contract

Every plugin is a Python class that subclasses one of the three base classes in `muse_monitor.sdk`.

```python
from typing import ClassVar
from pydantic import BaseModel

class Plugin:
    # --- manifest (class attributes, read without instantiating) ---
    name: ClassVar[str]                 # unique, kebab-case: "bandpower"
    version: ClassVar[str]              # plugin semver
    kind: ClassVar[str]                 # "source" | "analyzer" | "sink"
    api_version: ClassVar[int] = 1      # sdk contract version it targets
    summary: ClassVar[str] = ""
    Config: ClassVar[type[BaseModel]]   # validated config; JSON schema exposed to UI/API
    requires: ClassVar[list[str]] = []  # importable modules; missing -> "unavailable"
    cpu_bound: ClassVar[bool] = False   # run_blocking uses a process pool if True

    def __init__(self, config: BaseModel, ctx: "Context") -> None: ...
    async def start(self) -> None: ...  # acquire resources
    async def stop(self) -> None: ...   # release resources; must be idempotent
    def health(self) -> "Health": ...   # ok | degraded | failed + message + counters
```

`Context` is what the kernel gives a plugin. It is the only way a plugin talks to the kernel:

```python
class Context:
    session_id: str
    log: logging.Logger                       # prefixed with plugin name
    def emit(self, item: Chunk | Event) -> None
    def declare(self, info: StreamInfo) -> None   # register an output stream
    async def run_blocking(self, fn, *args)       # thread/process pool
    def data_dir(self) -> Path                    # per-plugin writable dir (models cache etc.)
    http: PluginRouter | None                     # mount routes at /api/v1/x/{plugin-name}/
    cli: PluginCli | None                         # add `muse x {plugin-name} …` commands
```

Plugins **must not** import from `muse_monitor.kernel` or other plugins. They communicate only through
streams and events.

## 2. Source

Produces data. Exactly one source per session in v1. Multiple sources (for example a Muse plus an
external sensor) are a later extension, and the bus already supports them.

```python
class Source(Plugin):
    kind = "source"

    @classmethod
    async def discover(cls, config) -> list[DeviceInfo]:
        """Optional. List devices in reach (BLE scan, mDNS…). Used by `muse devices`."""
        return []

    async def open(self) -> list[StreamInfo]:
        """Connect and return the streams this device will produce (EEG, ACC, …)."""

    async def run(self) -> None:
        """Loop: read from device, ctx.emit(Chunk(...)). Return when stop() is called
        or the device ends (replay). Raise SourceDisconnected to trigger reconnect policy."""
```

Kernel-provided behaviour for sources:

- **Reconnect policy** (from config `reconnect = {max_attempts, backoff_s}`): on `SourceDisconnected`
  the kernel calls `open()` again, emits `source.disconnected` / `source.reconnected` events, and the
  gap is recorded.
- **Device metadata** (`DeviceInfo`: id, model, firmware, preset, serial) is stored in the session.

## 3. Analyzer

Consumes streams and produces streams, events and views. It may contain plain logic or an ML model.

```python
class Analyzer(Plugin):
    kind = "analyzer"

    inputs: ClassVar[list[StreamSelector]]   # e.g. [StreamSelector(kind="eeg")]

    def setup(self, inputs: dict[str, StreamInfo]) -> list[StreamInfo]:
        """Called once input streams are known. Validate (rates, channels) and
        return output StreamInfos. Raise IncompatibleInput to refuse."""

    async def process(self, chunk: Chunk) -> None:
        """Called per input chunk. Emit derived Chunks/Events via ctx.emit."""

    def views(self) -> list[ViewSpec]:
        """Declarative visualizations bound to this plugin's output streams."""
        return []

    async def summarize(self) -> Report | None:
        """Optional. Called at session end (live or offline). Returns a report section
        (metrics, tables, figures as ViewSpecs) for `muse analyze` / the Reports page."""
        return None
```

Rules:
- `process()` must not block. Use `ctx.run_blocking` for anything slow (rule of thumb: over 5 ms).
- Analyzers handle windowing themselves with `sdk.Windower(size_s, step_s)`, a helper that
  accumulates chunks and yields overlapping windows with correct timestamps.
- Analyzers are deterministic for a given input + config + version (models: fixed seed, eval mode).
  This is what makes derived data reproducible.

### 3.1 Model analyzers

ML models are just analyzers. The SDK provides a base class so model plugins stay small:

```python
class ModelAnalyzer(Analyzer):
    cpu_bound = True
    class Config(BaseModel):
        weights: str                 # HF repo id or local path, e.g. "brain-bzh/reve-base"
        revision: str | None = None  # pin for reproducibility
        window_s: float = 4.0
        step_s: float = 1.0
        device: str = "cpu"
        head: str | None = None      # optional path to a fitted probe (sklearn/torch)

    def load(self) -> None: ...                       # load weights into memory (in worker)
    def preprocess(self, window: Window) -> Any: ...  # resample, re-reference, channel mapping
    def infer(self, x) -> np.ndarray: ...             # embedding or class probabilities
```

- Weights are downloaded **only** into `ctx.data_dir()` and only when the user runs
  `muse models pull <plugin>`. There are no implicit downloads during a session.
- Output is either an `embedding` stream (for probes and trends) or a `score` stream with named classes.
- `views()` defaults to a time series of scores and a table of model and revision.

## 4. Sink

Consumes streams and has side effects (disk, network).

```python
class Sink(Plugin):
    kind = "sink"
    inputs: ClassVar[list[StreamSelector]] = [StreamSelector(any=True)]
    lossless: ClassVar[bool] = False   # True -> unbounded spill queue, never dropped

    async def open(self, streams: dict[str, StreamInfo], session: SessionInfo) -> None: ...
    async def write(self, item: Chunk | Event) -> None: ...
    async def close(self) -> None: ...  # flush; must leave files valid after a crash too
```

## 5. Visualizations: `ViewSpec`

```python
class ViewSpec(BaseModel):
    id: str                      # unique within plugin
    title: str
    type: Literal["timeseries", "spectrum", "bars", "metric", "heatmap",
                  "table", "events", "custom"]
    streams: list[str]           # stream ids it reads
    channels: list[str] | None = None
    window_s: float = 10.0       # visible history
    y: Axis | None = None        # label, unit, min/max, log
    thresholds: list[Threshold] = []   # colored bands, e.g. quality ok/bad
    refresh_hz: float = 10.0     # max UI update rate (server decimates)
    asset: str | None = None     # only for type="custom": JS module shipped in plugin package
```

Renderers:
- **Web UI** renders all types. `custom` loads the plugin's JS module (`export function render(el, spec, data)`).
- **CLI** (`muse run`) renders `metric`, `bars`, `table` and `events` as a Rich live layout, and
  `timeseries` as a sparkline. Unsupported types are skipped.
- **Reports**: static HTML that embeds the same renderer and a data snapshot.

## 6. Discovery and loading

1. Entry points group **`muse_monitor.plugins`** (installed packages, including built-ins):
   ```toml
   [project.entry-points."muse_monitor.plugins"]
   bandpower = "muse_monitor.plugins.bandpower:BandPower"
   ```
2. **Local plugins**: `*.py` files or packages in `./plugins/` or in `$MUSE_PLUGIN_PATH`, for quick
   experiments without packaging. Every subclass of `Plugin` defined there is registered.

Loading is lazy. The manifest is read from class attributes, and modules listed in `requires` are only
checked with `importlib.util.find_spec` until the plugin is actually used. A name collision is an error
unless the user sets `override = true` in config.

## 7. Lifecycle

```
discovered → (config validated) → created → started → running ─┬→ stopped
                                                               └→ failed (exception) → stopped
```

- Start order: sinks → analyzers (topological) → source. Stop order is the reverse, so no data is lost.
- An exception in `process`/`write`/`run` sets the plugin to `failed`, emits a `plugin.failed` event
  with the traceback summary, and unsubscribes it. Exception: when the **source** or a `lossless`
  sink fails, the session stops, because continuing would silently lose data.

## 8. Pipeline config (TOML)

```toml
# pipelines/live-default.toml
[session]
label = "morning meditation"

[source]
plugin = "openmuse"
preset = "p1041"
address = ""            # empty = strongest Muse found
reconnect = { max_attempts = 10, backoff_s = 2 }

[[analyzer]]
plugin = "quality"

[[analyzer]]
plugin = "bandpower"
window_s = 2.0
step_s = 0.5
bands = { delta = [1,4], theta = [4,8], alpha = [8,13], beta = [13,30], gamma = [30,45] }

[[analyzer]]
plugin = "reve"           # optional; needs `muse-monitor[models]`
enabled = false
weights = "brain-bzh/reve-base"

[[sink]]
plugin = "recorder"       # on by default; `--no-record` disables
```

A plugin may appear more than once if each instance has a distinct `id = "..."`. Stream ids are
namespaced by instance: `bandpower/eeg_abs`, `bandpower-slow/eeg_abs`.

## 9. Built-in plugins (v1)

| Kind | Name | Purpose | Notes |
|---|---|---|---|
| source | `synthetic` | Configurable sines + noise + fake blinks on N channels | Tests, demos, CI. No hardware |
| source | `openmuse` | Muse S Athena over BLE via OpenMuse | Ports the logic in `check.py` |
| source | `replay` | Replays a recorded session (or OpenMuse raw `.txt`) at `speed` = 1×…∞ | Powers offline analysis |
| source | `android-http` | Receives the Android app's HTTP stream via `ctx.http` route | Blocked on the format questions in `AGENTS.md` |
| source | `osc` | UDP/OSC listener (Mind Monitor) | Later |
| analyzer | `filter` | 1–40 Hz band-pass + 50/60 Hz notch → `eeg_filtered` | Display only; raw untouched |
| analyzer | `quality` | Per-channel contact/noise (std, line-noise ratio, flatline) | Metric + bars view with thresholds |
| analyzer | `bandpower` | Welch PSD → absolute and relative band powers per channel | Spectrum + bars + timeseries views |
| analyzer | `artifacts` | Blink/jaw (frontal amplitude), motion (IMU magnitude) → events | Events view |
| analyzer | `reve`, `zuna`, `cbramod` | `ModelAnalyzer` wrappers (embeddings, optional probe head) | Extra `[models]` |
| sink | `recorder` | Lossless Parquet writer + session metadata | `lossless = True`, default on |
| sink | `lsl-out` | Republish streams as LSL outlets | Extra `[lsl]` |
| sink | `export` | EDF+ / BIDS / CSV at session close | Extra `[bids]` |

## 10. Plugin testing kit

`muse_monitor.sdk.testing` ships:
- `check_plugin(cls)`: validates the manifest, config schema, the start/stop idempotence and that
  `stop()` works without `start()`.
- `run_analyzer(cls, config, source=SyntheticSource(...), seconds=10)`: returns the emitted chunks
  and events.
- `assert_peak(band="alpha")` and similar helpers for spectral tests (a 10 Hz sine must come out as alpha).

Every built-in plugin has a test that runs through this kit.
