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
class ModelAnalyzer(Analyzer):                     # muse_monitor.sdk
    cpu_bound = True
    embedding_dim: ClassVar[int]
    class Config(BaseModel):
        weights: str              # HF repo id or local folder
        revision: str             # required: pins the weights
        window_s: float = 4.0
        step_s: float = 1.0
        head: str | None = None   # probe from `muse train-probe` (.npz)
        channels: list[str] | None = None
    def load(self, path: Path) -> None: ...                  # worker thread, on the first window
    def preprocess(self, w: Window, rate: float) -> Any: ... # resample, filter, scale
    def infer(self, x) -> np.ndarray: ...                    # 1-D embedding
```

The base class emits `embedding` and, with a head, `probs` (one channel per class) plus views.
Weights are downloaded only by `muse models pull` into `~/.cache/muse-monitor/models`
(`MUSE_MODELS_DIR`). There are no downloads during a session; missing weights fail only that plugin.

A **probe head** is a standardizer plus a linear layer stored as plain arrays in an `.npz` (no
pickle), applied with numpy. It records the model, weights revision, channels and window it was
trained with; a mismatch fails the plugin instead of giving silently wrong output.

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
    type: Literal["timeseries", "spectrum", "bars", "metric", "table", "events", "text"]
    description: str = ""              # plain-language explanation, shown behind an info icon
    streams: list[str]
    channels: list[str] | None = None
    window_s: float = 10.0
    y: Axis | None = None              # label, unit, min, max, log
    thresholds: list[Threshold] = []   # colored bands
    event_types: list[str] = []        # "events"/"text" views: type prefixes ("text": paragraphs in data.text)
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



| Kind     | Plugin        | Description                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| -------- | ------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| source   | `openmuse`  | Connects directly to a Muse S Athena over Bluetooth through OpenMuse, with no phone app. It finds the strongest headband in range, or uses `address`. It then sends the startup sequence for `preset` (default p1041: 8 EEG, 16 optics, accelerometer and gyroscope, battery). It waits `settle_s` to see which sensors stream before declaring them. Sample times are rebuilt from the packet counters and the arrival time, using the minimum offset over 60 s to remove jitter and follow drift. Lost subpackets show up as index gaps. Packets that arrive during settle are decoded in order before live decoding starts. After a disconnect it reconnects and accounts for the outage. EEG is in µV centred on zero: `(raw14 − 8192) × 1450/16383`, range ±725 µV. OpenMuse leaves the midpoint in, so the source subtracts it (v1.1+) and records `offset_uv` and the conversion in the stream metadata. |
| source   | `mindmonitor` | Receives Muse data relayed by the Mind Monitor phone app as OSC over UDP (default port 5000; set the app's target IP to this computer). `/muse/eeg` (TP9, AF7, AF8, TP10, then AUX), `/muse/acc`, `/muse/gyro`, `/muse/ppg`, `/muse/optics`, `/muse/batt`, `/Marker/N` (→ markers). OSC has no packet counter or device timestamp: sample times are rebuilt from the rate and arrival time (minimum offset over 60 s), and EEG gaps are inferred when data stays later than `gap_tolerance_s` for `gap_confirm_s`; other streams' rates are measured at start and their gaps are not inferred. Mind Monitor's EEG is not centred on zero; `eeg_offset_uv` subtracts a midpoint. |
| source   | `synthetic` | Generates sines plus noise on configurable channels, optionally with fake frontal blinks, simulated sample loss (`gaps`), and an accelerometer and battery. It can run in real time or as fast as the pipeline allows. Used for tests, demos and plugin development without hardware.                                                                                                                                                                                                                                                                                                                                                                                                                                                         |
| source   | `replay`    | Plays back a recorded session (raw streams and events) through the same pipeline for offline analysis. Speed 0 means as fast as possible with no drops (backpressure). It keeps the original timestamps and sample indices, so gaps are reproduced exactly.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                     |
| analyzer | `quality`   | Checks each EEG channel's contact and noise in 1 s windows. It emits `quality/mask` (0 ok, 1 poor contact, 2 noisy, 3 flat), `quality/std`, and the share of usable channels. Other analyzers read the mask through `QualityMask` to skip bad channels. The report gives the percentage of usable data per electrode.                                                                                                                                                                                                                                                                                                                                                                                                                     |
| analyzer | `filter`    | Display filter: 1–40 Hz band-pass plus a 50/60 Hz notch, applied causally to EEG. It outputs `filter/eeg` and never changes the raw data.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| analyzer | `bandpower` | Welch power spectrum per window (default 2 s, updated every 0.5 s). Outputs: absolute (`abs`) and relative (`rel`) power per channel and band; `mean_rel`, the average over channels that pass the quality check; the spectrum (`psd`); and `norm`, power relative to the eyes-open baseline once calibration has run. Default bands: delta 1–4, theta 4–8, alpha 8–13, beta 13–30, gamma 30–45 Hz.                                                                                                                                                                                                                                                                                                                              |
| analyzer | `baseline`  | Runs the calibration protocol: 60 s eyes open, then 60 s eyes closed. It computes a personal band-power baseline from those periods and publishes `baseline.ready`, which `bandpower` uses for `norm`.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                    |
| analyzer | `artifacts` | Detects blinks and jaw clenches from the amplitude on frontal (AF7/AF8) and temporal channels, and movement from the accelerometer. Each is emitted as an event with a duration, and the report gives counts per minute.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                        |
| analyzer | `cbramod`   | A `ModelAnalyzer` for `braindecode/cbramod-pretrained` (4.9M parameters, BSD-3). It needs the `[models]` extra and weights fetched with `muse models pull cbramod`; nothing is downloaded during a session. Each 4 s window (default step 1 s) is band-passed to 0.3–75 Hz, notch-filtered, resampled to 200 Hz and scaled to units of 100 µV. The model output is averaged into a 200-value `embedding`. With a `head` from `muse train-probe`, it also emits class probabilities. Takes about 20 ms per window on CPU.                                                                                                                                                                                                        |
| analyzer | `describe`  | Plain-language description of the measurements, every `every_s` (30 s) and per marker/calibration segment in its report: signal quality first (below `min_usable` it says only that), band power vs. the eyes-open baseline (or vs. earlier in the session), blinks, jaw clenching and movement, with artifact caveats. A trained probe is quoted only if it beat band power on held-out sessions by `probe_min_margin` in most sessions. Template-based and local; wording describes measurements, never mental states, and a banned-word check guards every text. Emits `description` events (text + facts) and a `text` view. List it after the analyzers it reads. |
| sink     | `recorder`  | Saves every raw stream losslessly to Parquet, plus events and session metadata. Data is written every `flush_s` (default 5 s) as complete part files, so a crash loses at most one buffer. Parts are merged when the session closes, or later with `sessions repair`. Critical: if it fails, the session stops.                                                                                                                                                                                                                                                                                                                                                                                                                             |
| sink     | `export`    | When the session closes, writes CSV, or EDF+ with EEG first and markers and events as annotations. Uses a built-in EDF+ writer, because pyedflib has no Python 3.13 Windows wheel. EDF needs exact fixed rates but the headband's clocks run slightly off nominal, so each continuous run of samples is resampled onto the nominal grid by its timestamps (raw files stay exact); gaps are zero-filled and annotated. Units are written as ASCII (`uV`). Verified in MNE.                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                                              |

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
