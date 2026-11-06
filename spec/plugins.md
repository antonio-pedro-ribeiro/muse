# Plugins

## 1. Base contract

```python
class Plugin:
    name: ClassVar[str]               # "bandpower"
    version: ClassVar[str]            # semver
    kind: ClassVar[str]               # "source" | "analyzer" | "sink"
    api_version: ClassVar[int] = 1
    Config: ClassVar[type[BaseModel]] # validated; JSON schema drives UI forms
    requires: ClassVar[list[str]] = []    # modules; missing -> "unavailable"
    cpu_bound: ClassVar[bool] = False     # True -> dedicated worker thread

    def __init__(self, config, ctx: Context): ...
    async def start(self) -> None: ...
    async def stop(self) -> None: ...     # idempotent; safe without start()
    def health(self) -> Health: ...       # ok | degraded | failed + message + counters

class Context:                            # the plugin's only handle on the kernel
    session_id: str
    instance_id: str
    mode: Literal["live", "offline"]
    recordings_dir: Path                  # data root (recorder, export)
    log: logging.Logger
    def emit(self, item: Chunk | Event) -> None   # analyzer stream ids are namespaced automatically
    def data_dir(self) -> Path            # per-plugin cache (model weights, …)
    async def run_blocking(self, fn, *args)       # off the loop; dedicated thread if cpu_bound
    async def pace(self) -> None          # sources call after emitting: offline waits for room
```

The on-disk format is public too: `muse_monitor.sdk.storage` (writer, reader, compaction), so sources,
sinks and tools share one implementation without importing each other.

## 2. Source

```python
class Source(Plugin):
    kind = "source"
    @classmethod
    async def discover(cls, config) -> list[DeviceInfo]: return []   # optional scan
    async def open(self) -> list[StreamInfo]: ...    # connect; declare streams (with positions)
    async def run(self) -> None: ...                 # emit chunks until stopped or end of data
```

When the source raises `SourceDisconnected`, the kernel calls `open()` again according to
`reconnect = {max_attempts, backoff_s}` and emits `source.disconnected` / `source.reconnected`
events. The gap is recorded.

## 3. Analyzer

```python
class Analyzer(Plugin):
    kind = "analyzer"
    inputs: ClassVar[list[StreamSelector]]          # e.g. [StreamSelector(kind="eeg")]

    def setup(self, inputs: dict[str, StreamInfo]) -> list[StreamInfo]: ...  # outputs; may raise IncompatibleInput
    async def process(self, chunk: Chunk, gap: bool) -> None: ...  # gap=True -> reset state first
    async def on_event(self, event: Event) -> None: ...            # markers, calibration, baseline.ready
    def views(self) -> list[ViewSpec]: return []
    async def summarize(self) -> Report | None: return None       # end of session, for reports
```

- `process()` must not block. Set `cpu_bound = True` for heavy work.
- Output must be deterministic for the same input, config and version.
- SDK helpers:
  - `Windower(size_s, step_s)` produces overlapping windows with correct timestamps and resets on a gap.
  - `Aligner(streams)` joins streams of different rates by time (e.g. EEG 256 Hz with IMU 52 Hz).
  - `QualityMask` reads the standard `quality/mask` stream (see [data.md](data.md#3-quality-mask)), so
    analyzers can skip bad windows.

### Model analyzers

```python
class ModelAnalyzer(Analyzer):
    cpu_bound = True
    class Config(BaseModel):
        weights: str              # HF repo id or local path
        revision: str             # required: pins the weights
        window_s: float = 4.0
        step_s: float = 1.0
        head: str | None = None   # probe from `muse train-probe`
    def load(self) -> None: ...                    # in worker thread
    def preprocess(self, w: Window) -> Any: ...    # resample, map channels via positions
    def infer(self, x) -> np.ndarray: ...          # embedding, or class probabilities if head
```

Weights are downloaded only by `muse models pull`. There are no downloads during a session.

## 4. Sink

```python
class Sink(Plugin):
    kind = "sink"
    inputs: ClassVar[list[StreamSelector]] = [StreamSelector(any=True)]
    critical: ClassVar[bool] = False      # True (recorder): failure or full queue stops the session
    async def open(self, streams: dict[str, StreamInfo], session: SessionInfo) -> None: ...
    async def write(self, item: Chunk | Event) -> None: ...
    async def close(self) -> None: ...    # flush; files must stay readable after a crash
```

## 5. ViewSpec

```python
class ViewSpec(BaseModel):
    id: str
    title: str
    type: Literal["timeseries", "spectrum", "bars", "metric", "table", "events"]
    description: str = ""              # plain-language explanation, shown behind an info icon
    streams: list[str]
    channels: list[str] | None = None
    window_s: float = 10.0
    y: Axis | None = None              # label, unit, min, max, log
    thresholds: list[Threshold] = []   # colored bands
    event_types: list[str] = []        # "events" views: type prefixes to show
```

Every view and every config option (`Field(description=...)`) carries a short plain-language
explanation; the web UI and reports show it behind an "i" icon, and tests require it for built-ins.

The web UI renders all types. The CLI renders `metric`, `bars`, `table` and `events`, plus
`timeseries` as a sparkline. Reports embed the web renderer together with a data snapshot.

## 6. Discovery

- Entry points group `muse_monitor.plugins`, for installed packages and built-ins.
- `./plugins/*.py` or `$MUSE_PLUGIN_PATH`, for local experiments.

Duplicate names are an error.

## 7. Pipeline config

```toml
[source]
plugin = "openmuse"
preset = "p1041"

[[analyzer]]
plugin = "quality"

[[analyzer]]
plugin = "bandpower"
window_s = 2.0

[[sink]]
plugin = "recorder"   # default on; --no-record disables
```

A plugin can appear more than once with distinct `id`s. Stream ids are namespaced by instance:
`bandpower/eeg_rel`.

## 8. Built-ins

| Kind | Name | Purpose |
|---|---|---|
| source | `synthetic` | Sines + noise + fake blinks; tests and demos |
| source | `openmuse` | Muse S Athena over BLE (ports `check.py`) |
| source | `replay` | Recorded session as a source; powers offline analysis |
| analyzer | `quality` | Per-channel contact/noise → `quality/mask` + views |
| analyzer | `filter` | 1–40 Hz band-pass + 50/60 Hz notch, for display |
| analyzer | `baseline` | Eyes-open/closed calibration → per-session normalization |
| analyzer | `bandpower` | Welch → absolute, relative and baseline-normalized band powers |
| analyzer | `artifacts` | Blink/jaw (frontal amplitude) and motion (IMU) → events |
| analyzer | `cbramod` | `ModelAnalyzer` for `braindecode/cbramod-pretrained` (`[models]` extra); resamples to the model's rate, maps channels by position |
| sink | `recorder` | Parquet, `critical = True` |
| sink | `export` | CSV / EDF+ at session close |

## 9. Example plugin

```python
# plugins/alpha_ratio.py
import numpy as np
from pydantic import BaseModel
from scipy.signal import welch
from muse_monitor.sdk import Analyzer, StreamSelector, StreamInfo, Chunk, ViewSpec, Windower

class AlphaRatio(Analyzer):
    name, version = "alpha-ratio", "0.1.0"
    inputs = [StreamSelector(kind="eeg")]

    class Config(BaseModel):
        window_s: float = 2.0

    def setup(self, inputs):
        (self.src,) = inputs.values()
        self.win = Windower(self.config.window_s, step_s=0.5, rate=self.src.rate)
        return [StreamInfo(id="ratio", kind="derived", channels=self.src.channels,
                           units=["1"] * len(self.src.channels), rate=2.0)]

    async def process(self, chunk: Chunk, gap: bool):
        if gap:
            self.win.reset()
        for w in self.win.push(chunk):
            f, p = welch(w.data, fs=self.src.rate, axis=0)
            alpha = p[(f >= 8) & (f < 13)].mean(0)
            beta = p[(f >= 13) & (f < 30)].mean(0)
            self.ctx.emit(Chunk.single("ratio", alpha / beta, t=w.t_end))

    def views(self):
        return [ViewSpec(id="ratio", title="Alpha/beta ratio", type="timeseries", streams=["ratio"])]
```

The matching test:

```python
def test_alpha_ratio():
    out = run_analyzer(AlphaRatio, {}, source=synthetic(sines={10: 20}), seconds=10)
    assert (out["ratio"].data > 1).all()
```

## 10. Testing kit (`sdk.testing`)

- `check_plugin(cls)`: manifest, config schema, stop idempotence.
- `run_analyzer(cls, config, source, seconds)`: returns emitted streams and events.
- `synthetic(...)`: builds a `synthetic` source with a known spectrum, gaps and blinks.
