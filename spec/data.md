# Data model and storage

## 1. Core types (`muse_monitor.sdk.types`)

```python
@dataclass(frozen=True)
class StreamInfo:
    id: str                    # "eeg", "acc", "bandpower/eeg_rel" (namespaced by producer)
    kind: str                  # eeg | optics | acc | gyro | ppg | battery | derived | embedding | score
    channels: list[str]        # ["TP9","AF7","AF8","TP10"] — never assumed, always declared
    units: list[str]           # per channel: "uV", "g", "deg/s", "%", "uV^2/Hz", "1"
    rate: float                # nominal Hz; 0.0 = irregular
    dtype: str = "float32"
    producer: str = ""         # plugin instance id
    meta: dict = field(default_factory=dict)   # e.g. {"preset":"p1041","scale":0.0885}

@dataclass(frozen=True)
class Chunk:
    stream: str                # StreamInfo.id
    data: np.ndarray           # shape (n_samples, n_channels)
    t: np.ndarray              # shape (n_samples,), float64 UTC seconds (POSIX)
    first_index: int           # running sample index since stream start (gap detection)
    received: float            # monotonic clock at arrival (latency/jitter stats)

@dataclass(frozen=True)
class Event:
    type: str                  # "artifact.blink", "source.disconnected", "plugin.failed", "marker"
    t: float                   # UTC seconds
    duration: float = 0.0
    source: str = ""           # plugin instance id
    data: dict = field(default_factory=dict)
```

Kernel guarantees per stream:
- Chunks arrive in order of `first_index`.
- `first_index` jumps are reported as `stream.gap` events (`{stream, missing_samples, t}`). Gaps are
  never filled silently.
- Duplicate chunks (same `first_index`) are dropped and counted.

## 2. Time

- `t` is UTC seconds and is the source's best estimate. Muse Athena has no per-sample timestamps, so
  the `openmuse` source reconstructs `t` from the sample rate and packet counters, anchored to the
  receive clock and corrected slowly for drift (linear fit over a sliding 60 s window).
- `received` uses `time.monotonic()` and is used only for latency, jitter and gap statistics.
- User markers (`POST /sessions/{id}/markers`, CLI keypress `m`) are `Event(type="marker")`.

## 3. Session

```python
class SessionInfo(BaseModel):
    id: str                      # ULID (sortable by time)
    label: str | None
    started_at: datetime         # UTC
    ended_at: datetime | None
    mode: Literal["live", "offline"]
    parent: str | None           # offline analysis: the recording it was derived from
    device: DeviceInfo | None    # model, serial (hashed by default), firmware, preset
    streams: list[StreamInfo]
    pipeline: dict               # full resolved config
    plugins: dict[str, str]      # instance id -> "name==version"
    app_version: str
    stats: dict                  # per stream: samples, gaps, dropped, effective rate
```

## 4. Storage layout (recorder sink)

```
recordings/                       # gitignored; location set by MUSE_DATA_DIR
  01JB…XYZ/                       # session id
    session.json                  # SessionInfo (rewritten at start, every 30 s and at close)
    raw/
      eeg.parquet                 # columns: t (float64), index (int64), TP9, AF7, … (float32)
      acc.parquet
      gyro.parquet
      optics.parquet
      battery.parquet
    events.jsonl                  # one Event per line, append-only
    derived/                      # written by offline runs and by live runs if enabled
      bandpower/eeg_rel.parquet
      quality/quality.parquet
    reports/
      2026-11-06T10-31Z.html
```

Rules:
- Parquet files are written in **row groups every 5 s** (configurable). A crash loses at most the
  current row group, and previous groups remain readable. On abnormal termination the recorder writes a
  sidecar `*.partial` marker, and `muse sessions repair` finalizes the footer.
- `raw/` is write-once. Offline runs only ever write to `derived/` and `reports/`.
- Each Parquet file stores its `StreamInfo` as JSON in the schema metadata, so a file can be
  interpreted on its own.
- EEG is stored in µV after the device's documented scaling. The scaling factor and raw dtype go in
  `StreamInfo.meta` so the raw counts can be recovered.

## 5. Export

| Format | Command | Use |
|---|---|---|
| EDF+ | `muse sessions export ID --format edf` | Clinical/legacy tools, EEGLAB |
| BIDS-EEG | `--format bids --bids-root DIR` | Sharing and research pipelines (via mne-bids) |
| CSV | `--format csv` | Spreadsheets, quick looks |
| FIF | `--format fif` | MNE-Python |

Markers and artifact events become EDF+ annotations or BIDS `events.tsv`.

## 6. Privacy

- Device serials are hashed (SHA-256 with a per-install salt) unless `privacy.keep_serials = true`.
- `recordings/`, `data/` and `plugins/*/data` are in `.gitignore`.
- No telemetry. Sinks that send data off the machine (`webhook`, a remote `lsl-out`) must be enabled
  explicitly and are labelled "external" in the UI.
