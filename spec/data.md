# Data

## 1. Types (`muse_monitor.sdk`)

```python
@dataclass(frozen=True)
class StreamInfo:
    id: str                       # "eeg", "bandpower/eeg_rel"
    kind: str                     # eeg | optics | acc | gyro | battery | derived | embedding | score | mask
    channels: list[str]
    units: list[str]              # per channel: "uV", "g", "deg/s", "%", "1"
    rate: float                   # nominal Hz; 0.0 = irregular
    positions: dict[str, tuple[float, float, float]] | None = None  # metres, MNE head frame
    meta: dict = field(default_factory=dict)   # preset, scale factor, raw dtype

@dataclass(frozen=True)
class Chunk:
    stream: str
    data: np.ndarray              # (n_samples, n_channels)
    t: np.ndarray                 # (n_samples,) float64 UTC seconds
    first_index: int              # sample counter since stream start

@dataclass(frozen=True)
class Event:
    type: str                     # "marker", "artifact.blink", "stream.gap", "plugin.failed", …
    t: float
    duration: float = 0.0
    source: str = ""
    data: dict = field(default_factory=dict)
```

Kernel guarantees per stream:
- Chunks are in order.
- A jump in `first_index` emits `stream.gap {stream, missing_samples}`. Gaps are never filled.
- Duplicate chunks are dropped and counted.

**Positions.** Sources declare electrode positions; for Muse, the standard 10-20 coordinates for
TP9/AF7/AF8/TP10/FPz from MNE's `standard_1005`. Model analyzers and export use them.

## 2. Time

- `t` is UTC. Muse has no per-sample timestamps, so `openmuse` reconstructs them from the sample rate
  and packet counter, anchored to receive time and corrected for drift (linear fit over 60 s).
- Gaps and latency are measured with the monotonic clock.

## 3. Quality mask

The `quality` analyzer emits `quality/mask`: a `kind="mask"` stream at 1 Hz, with one channel per EEG
channel. Values: `0` = ok, `1` = poor contact, `2` = noisy, `3` = flat. Analyzers read it via
`QualityMask` to skip or flag windows. Reports summarize the percentage of usable data per channel.

## 4. Baseline

A session may start with a calibration: 60 s eyes open, then 60 s eyes closed, with markers emitted by
the CLI or UI. The `baseline` analyzer stores per-channel band powers for each condition in session
metadata. `bandpower` then also emits values normalized to that baseline. Cross-session trends use
the normalized values.

## 5. Session

```python
class SessionInfo(BaseModel):
    id: str                       # ULID
    label: str | None
    mode: Literal["live", "offline"]
    parent: str | None            # offline: source recording
    started_at: datetime          # UTC
    ended_at: datetime | None
    device: DeviceInfo | None     # model, hashed serial, firmware, preset
    streams: list[StreamInfo]
    pipeline: dict                # resolved config
    plugins: dict[str, str]       # instance -> "name==version"
    baseline: dict | None
    stats: dict                   # per stream: samples, gaps, dropped, effective rate
    app_version: str
```

## 6. Storage

```
recordings/<session-id>/          # MUSE_DATA_DIR; gitignored
  session.json
  raw/<stream>.parquet            # t, index, one column per channel; write-once
  events.jsonl
  derived/<instance>/<stream>.parquet
  reports/<timestamp>.html
```

- Parquet row groups are flushed every 5 s. After a crash, at most the last 5 s are lost, and
  `muse sessions repair` finalizes the file.
- `StreamInfo` is stored in each Parquet file's schema metadata, so every file describes itself.
- EEG is stored in µV. The scale factor goes in `meta`, so the raw counts can be recovered.
- Device serials are hashed with a per-install salt.

## 7. Export

`muse sessions export ID --format csv|edf`. Markers and events become EDF+ annotations. BIDS and FIF
can be added later as plugins.
