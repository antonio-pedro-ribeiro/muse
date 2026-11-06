# Architecture and Code Review: Muse Monitor

**Date:** 2026-11-06  
**Version Reviewed:** `0.1.0` (Spec v0.4, Milestones M0–M6 + `describe`, `replay`, `mindmonitor`, session management)  
**Scope:** Full repository (`spec/`, `src/muse_monitor/`, `tests/`, `docs/`, `pyproject.toml`)

---

## 1. Executive Summary

Muse Monitor is a remarkably cohesive, well-engineered Python 3.13 application. It implements a true microkernel architecture with strict separation between the core engine (`kernel/`), the public plugin contract (`sdk/`), built-in plugins (`plugins/`), and user-facing adapters (`interfaces/`). The codebase is compact, readable, idiomatic, and thoughtfully designed around real hardware constraints discovered on the Muse S Athena (clock drift, subpacket counter wrap-around, 14-bit DC offset, mains hum, and crash recovery).

### Codebase Metrics

| Component | Files | Lines of Code | Spec Target / Role |
|---|---:|---:|---|
| `src/muse_monitor/kernel/` | 8 | 1,496 | **Target: ≤ 1,500 LOC** (met: 1,496 LOC; depends only on stdlib, `numpy`, `pydantic`) |
| `src/muse_monitor/sdk/` | 8 | 1,070 | Public types, plugin ABCs, DSP helpers, `ModelAnalyzer`, Parquet storage, testing kit |
| `src/muse_monitor/plugins/` | 14 | 1,987 | 4 sources (`openmuse`, `mindmonitor`, `synthetic`, `replay`), 7 analyzers, 2 sinks |
| `src/muse_monitor/interfaces/` (Python) | 4 | 1,131 | Typer/Rich CLI (`cli.py`), FastAPI REST/WS (`http.py`), HTTP/WS client (`client.py`) |
| `src/muse_monitor/interfaces/web/` | 4 | 1,473 | Dependency-free vanilla JS (`app.js`, `renderers.js`), HTML, CSS |
| Top-level modules | 5 | 616 | `archive.py`, `check.py`, `probe.py`, `report.py`, `__init__.py` |
| `tests/` | 10 | 1,480 | Unit, integration, DSP, storage, CLI, and REST/WebSocket tests |
| **Total** | **53** | **9,253** | Complete end-to-end application + test suite |

### Assessment Scorecard

| Area | Rating | Summary |
|---|---|---|
| **Architecture & Modularity** | **Excellent** | Clean microkernel; strict SDK boundary; unified live/offline execution path; declarative views shared across Web, CLI, and HTML reports. |
| **Data Integrity & Storage** | **Excellent** | Raw data is immutable; atomic `.tmp` → `.parquet` part flushes every 5 s survive `kill -9`; EDF+ export resamples onto the nominal grid so annotations never drift. |
| **Signal Processing (DSP)** | **Good (1 High bug)** | Solid drift/jitter clock reconstruction and hum-resistant quality checks, but `band_powers()` drops 1 Hz boundary bins between adjacent bands, and `cbramod` runs zero-phase `sosfiltfilt` on unpadded 4 s windows. |
| **Concurrency & Reliability** | **Good (2 Medium bugs)** | Dedicated worker threads per `cpu_bound` plugin and bounded queues with explicit policies (`drop`, `block`, `fail`, `latest`), but `openmuse`/`mindmonitor` reconnect paths and `Baseline` → `BandPower` event ordering in fast/offline replay have subtle bugs. |
| **Security & Privacy** | **Good (1 High, 1 Medium)** | Salted SHA-256 serial hashing, defensive zip-archive import validation, and banned-word guardrails in `describe`, but `POST /api/v1/probes` allows arbitrary file writes via `out`, and localhost `POST` endpoints lack CSRF/Origin protection. |
| **Testing & Documentation** | **Very Good** | High-signal tests on synthetic waveforms with known spectra; thorough user and architecture docs. Missing CI workflow, `samples/` hardware captures, and reconnect unit tests. |

---

## 2. Architectural Review

### 2.1 What Works Exceptionally Well

1. **Strict Microkernel & Plugin Boundary (`kernel/` ↔ `sdk/` ↔ `plugins/`):**
   - `src/muse_monitor/kernel/` is **1,496 lines** across 8 files, hitting the `≤ 1,500` LOC budget in [`spec/architecture.md`](../spec/architecture.md#L55).
   - Built-in plugins in `src/muse_monitor/plugins/` are registered through standard `pyproject.toml` entry points (`[project.entry-points."muse_monitor.plugins"]`) and import **only** `muse_monitor.sdk`. The kernel has zero special-casing for Muse S Athena hardware; only `MUSE_POSITIONS` in `sdk/__init__.py` and the device sources know about Muse channels.
   - Optional heavyweight dependencies (`torch`, `braindecode`, `safetensors`, `huggingface_hub`) are checked lazily via `importlib.util.find_spec` in [`PluginRegistry._status`](../src/muse_monitor/kernel/registry.py#L101-L107) and imported inside plugin methods, so core startup stays fast and missing extras degrade gracefully to `status="unavailable"` with an actionable `install_hint`.

2. **"One Code Path" for Live and Offline Processing (`Replay` + `StreamBus`):**
   - Offline analysis ([`KernelFacade.run_offline`](../src/muse_monitor/kernel/facade.py#L332-L356)) replaces the pipeline's source with `replay` (`speed=0.0`) and runs the exact same `Session`, `StreamBus`, `Analyzer`s, and `Recorder` sink in `mode="offline"`.
   - Flow control adapts cleanly via `Subscriber.policy` ([`kernel/bus.py`](../src/muse_monitor/kernel/bus.py#L17-L68)):
     - Live analyzers use `policy="drop"` (dropping the oldest queued chunk on overflow, setting `gap=True` on the next chunk delivered to that analyzer so its `Windower` or filter resets, and emitting a rate-limited `kernel.overflow` event).
     - Live critical sinks (`recorder`) use `policy="fail"` (aborting the session loudly rather than silently losing raw samples).
     - Offline mode uses `policy="block"` across all subscribers, with sources awaiting `ctx.pace()` so zero samples are ever dropped.
   - [`Replay._chunks`](../src/muse_monitor/plugins/replay.py#L62-L76) splits chunks at `np.diff(idx) != 1` boundaries and merges streams and replayed events in timestamp order via `heapq.merge`, ensuring downstream analyzers see the exact same `stream.gap` events and `gap=True` flags offline as they did live.

3. **Declarative Views (`ViewSpec`) Across Three Targets:**
   - Analyzers declare `ViewSpec` models ([`sdk/views.py`](../src/muse_monitor/sdk/views.py)) with `type`, `section`, `streams`, `y` axis, `thresholds`, and plain-language `description`.
   - The Web UI ([`renderers.js`](../src/muse_monitor/interfaces/web/renderers.js)), standalone HTML reports ([`report.py`](../src/muse_monitor/report.py), which inlines `style.css` and `renderers.js` with a JSON snapshot), and the Rich terminal dashboard ([`cli.py`](../src/muse_monitor/interfaces/cli.py#L216-L326)) all render from `ViewSpec` without plugin-specific UI code.
   - Automated tests ([`test_every_builtin_view_explains_itself`](../tests/test_plugins.py#L129-L135) and [`test_every_builtin_option_is_described`](../tests/test_plugins.py#L137-L143)) enforce that every built-in view and config field has user-facing documentation.

4. **Crash-Safe Storage & Drift-Corrected EDF+ Export:**
   - [`StreamWriter`](../src/muse_monitor/sdk/storage.py#L47-L83) buffers chunks for `flush_s` (5 s), writes a self-contained Parquet file to `raw/<stream>.parts/NNNNNN.tmp`, and atomically renames it to `.parquet`. `read_stream` and `stats_from_files` read both compacted files and uncompacted `.parts`, so a process killed with `SIGKILL` / `taskkill /F` loses at most 5 s and remains immediately readable even before `muse sessions repair` runs.
   - [`export_edf`](../src/muse_monitor/plugins/export.py#L100-L203) solves a classic EEG engineering pitfall: because EDF+ requires integer sample counts per 1-second record at the nominal rate (`256 Hz`), while real Muse oscillators run slightly off nominal (~`256.7 Hz`), naive sample-counter placement drifts by ~5 seconds over a 30-minute session relative to wall-clock annotations. Resampling each contiguous run (`np.diff(idx) == 1`) onto the nominal timestamp grid while zero-filling and annotating gaps keeps signal traces and markers aligned to the millisecond.

5. **Non-Clinical Guardrails:**
   - [`plugins/describe.py`](../src/muse_monitor/plugins/describe.py#L35-L45) enforces a regex check (`BANNED`) against diagnostic, clinical, or mental-state phrasing on every generated description, backed by an exhaustive combinatorial test ([`test_wording_never_states_or_diagnoses`](../tests/test_describe.py#L52-L64)).

---

### 2.2 Spec vs. Implementation Discrepancies

| Spec Reference | Specified Behavior | Actual Implementation | Impact |
|---|---|---|---|
| [`spec/architecture.md` §3](../spec/architecture.md#L52) | *"SessionManager builds the plugin DAG from config (cycles are rejected)"* | [`Session.start`](../src/muse_monitor/kernel/session.py#L226-L227) sets up analyzers strictly in list order (`for rt in self.analyzers:`). Only streams from earlier plugins exist in `self.streams` when `_resolve()` runs. | Cycles are impossible because forward references cannot resolve, **but** analyzers are not topologically sorted. Listing `bandpower` before `quality` causes `bandpower`'s `optional=True` mask selector to silently miss `quality/mask`. |
| [`spec/data.md` §1](../spec/data.md#L7) | `StreamInfo.kind`: `eeg \| optics \| acc \| gyro \| battery \| derived \| embedding \| score \| mask` | [`Filter.setup`](../src/muse_monitor/plugins/filter.py#L38) emits `kind="eeg_filtered"`; `ModelAnalyzer` emits `kind="derived"` for `embedding` and `probs` ([`sdk/model.py`](../src/muse_monitor/sdk/model.py#L139-L150)). | Prevents `Filter` from colliding with `StreamSelector(kind="eeg")`, which is good, but `spec/data.md` should be updated to match the actual `kind` values used (`eeg_filtered`, `ppg`, `derived`). |
| [`spec/interfaces.md`](../spec/interfaces.md#L6-L16) | `KernelFacade` methods typed as returning `list[PluginManifest]`, `SessionInfo`, `Job`, `AsyncIterator[Chunk \| Event]` | [`KernelFacade`](../src/muse_monitor/kernel/facade.py) returns plain JSON-serializable `dict`s from all methods so `RemoteFacade` (`interfaces/client.py`) is a drop-in replacement. | Returning dicts is a pragmatic design choice for `KernelFacade` / `RemoteFacade` symmetry; update the signature block in `spec/interfaces.md` to reflect `dict[str, Any]`. |
| [`docs/pending.md` §5](../docs/pending.md#L83-L89) & [`describe.py`](../src/muse_monitor/plugins/describe.py#L175-L177) | Probe is recommended only if `model_mean - baseline_mean >= 0.05` **and** it wins in most held-out sessions. | [`probe.py:135, 164`](../src/muse_monitor/probe.py#L135) sets `model_beats_baseline = out["model_mean"] > out["baseline_mean"]` (any margin > 0, ignoring fold wins) and does not filter bad-quality windows. | `muse train-probe` can print a green `"The probe beats the band-power baseline"` verdict for a head that `describe` immediately rejects as unvalidated. |

---

## 3. Detailed Code & Correctness Findings

### 3.1 [HIGH] Piecewise Trapezoidal Integration Drops Boundary Bins in `band_powers`

- **Location:** [`src/muse_monitor/sdk/helpers.py:111-125`](../src/muse_monitor/sdk/helpers.py#L111-L125) and [`src/muse_monitor/plugins/bandpower.py:78-83`](../src/muse_monitor/plugins/bandpower.py#L78-L83)
- **Code:**
  ```python
  def welch_psd(data: np.ndarray, rate: float, segment_s: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
      from scipy.signal import welch
      nperseg = min(data.shape[0], max(8, round(segment_s * rate)))
      return welch(data, fs=rate, nperseg=nperseg, axis=0, detrend="constant")

  def band_powers(freqs: np.ndarray, psd: np.ndarray, bands: dict[str, tuple[float, float]]) -> np.ndarray:
      """Absolute band power (integrated PSD), shape (bands, channels)."""
      out = []
      for lo, hi in bands.values():
          m = (freqs >= lo) & (freqs < hi)
          out.append(np.trapezoid(psd[m], freqs[m], axis=0) if m.sum() > 1 else np.full(psd.shape[1], np.nan))
      return np.array(out)
  ```
- **Problem:**
  With `segment_s = 1.0` at `rate = 256.0`, `welch` produces frequency bins spaced at **$\Delta f = 1.0\text{ Hz}$**: `freqs = [0.0, 1.0, 2.0, 3.0, 4.0, ...]`.
  Because `band_powers` selects bins with a strict upper inequality `(freqs >= lo) & (freqs < hi)` and integrates over `freqs[m]` with `np.trapezoid`:
  - `delta (1.0, 4.0)` selects `freqs = [1.0, 2.0, 3.0]` and integrates only from **$1\text{ Hz}$ to $3\text{ Hz}$** (width $2\text{ Hz}$ instead of $3\text{ Hz}$ — **losing 33% of the delta band**, specifically $3\text{–}4\text{ Hz}$).
  - `theta (4.0, 8.0)` selects `freqs = [4.0, 5.0, 6.0, 7.0]` and integrates from **$4\text{ Hz}$ to $7\text{ Hz}$** (width $3\text{ Hz}$ instead of $4\text{ Hz}$ — **losing 25% of the theta band**, specifically $7\text{–}8\text{ Hz}$).
  - `alpha (8.0, 13.0)` integrates **$8\text{–}12\text{ Hz}$** (missing $12\text{–}13\text{ Hz}$).
  - `beta (13.0, 30.0)` integrates **$13\text{–}29\text{ Hz}$** (missing $29\text{–}30\text{ Hz}$).
  - `gamma (30.0, 45.0)` integrates **$30\text{–}44\text{ Hz}$** (missing $44\text{–}45\text{ Hz}$).
  - Meanwhile, in [`BandPower._window`](../src/muse_monitor/plugins/bandpower.py#L81-L83), `tot = band_powers(f, p, {"total": (1.0, 45.0)})[0]` selects `freqs = [1.0, ..., 44.0]`, which **includes** the intervals $[3, 4]$, $[7, 8]$, $[12, 13]$, and $[29, 30]$!
  - Consequently, for white noise (`psd = 1`), the five bands integrate to $2 + 3 + 4 + 16 + 14 = 39$, while `total` integrates to $43$ (or $44$ up to $45\text{ Hz}$). The relative band powers `rel = absp / tot` **do not sum to 1.0** (for a signal with power near band boundaries such as $3.5\text{ Hz}$ or $7.5\text{ Hz}$, up to half the power vanishes from all five bands while remaining in `total`).
- **Fix:**
  For piecewise trapezoidal integration over contiguous intervals $[a, b]$ and $[b, c]$ to satisfy $\int_a^b + \int_b^c = \int_a^c$, both intervals must include the shared boundary point $b$:
  ```python
  m = (freqs >= lo) & (freqs <= hi)
  ```
  With `(freqs >= lo) & (freqs <= hi)`, `delta [1..4] + theta [4..8] + alpha [8..13] + beta [13..30] + gamma [30..45]` equals `total [1..45]` to machine precision (`1e-16`), and relative band powers sum to exactly `1.0`.

---

### 3.2 [HIGH] Arbitrary File Write / Path Traversal in `POST /api/v1/probes`

- **Location:** [`src/muse_monitor/interfaces/http.py:251-254`](../src/muse_monitor/interfaces/http.py#L251-L254), [`src/muse_monitor/kernel/facade.py:398-401`](../src/muse_monitor/kernel/facade.py#L398-L401), [`src/muse_monitor/probe.py:162-163`](../src/muse_monitor/probe.py#L162-L163)
- **Code:**
  ```python
  # http.py
  @app.post(f"{P}/probes", dependencies=deps)
  async def train_probe(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
      """Body: {sessions, plugin, label_from, out, options}. The head is written on the server."""
      return await facade.train_probe(**body)

  # probe.py
  path = Path(out).resolve()
  head.save(path)
  ```
- **Problem:**
  1. Unlike `session_id` (which is strictly validated with `r"[0-9A-Za-z_-]{1,64}"` in `KernelFacade._check_id`) and report names (`Path(name).name`), the `out` parameter in `POST /api/v1/probes` is resolved directly with `Path(out).resolve()` and opened with `open(path, "wb")` in `ProbeHead.save()`. Any caller of the HTTP API can overwrite or create arbitrary files anywhere the server user has write permission.
  2. Similarly, `options` in `POST /api/v1/probes` is forwarded into `cls.Config(**options)` where `weights` can be set to an arbitrary server filesystem directory (`ModelAnalyzer.weights_dir`).
  3. When `muse train-probe --out my_head.npz --server http://...` is run from a remote machine ([`cli.py:589-590`](../src/muse_monitor/interfaces/cli.py#L589-L590)), the head is written on the *server's* working directory rather than downloaded to the client's `--out` path.
- **Fix:**
  - When called through the kernel/API, store trained probe heads in a managed directory (e.g., `self.data_dir / ".muse" / "probes" / f"{_check_id(name)}.npz"`), or validate that `out` stays within `self.data_dir` when invoked via HTTP.
  - Add a `GET /api/v1/probes/{name}` download endpoint (or return the `.npz` bytes / store by ID) so `muse train-probe --server URL --out local.npz` writes the file locally on the client as expected.

---

### 3.3 [MEDIUM] Reconnect Bugs in `OpenMuseSource` and `MindMonitor`

- **Locations:**
  - [`src/muse_monitor/plugins/openmuse.py:138-170, 267-281`](../src/muse_monitor/plugins/openmuse.py#L138-L170)
  - [`src/muse_monitor/plugins/mindmonitor.py:70-96, 194-209, 271-286`](../src/muse_monitor/plugins/mindmonitor.py#L70-L96)
  - [`src/muse_monitor/kernel/session.py:305-345`](../src/muse_monitor/kernel/session.py#L305-L345)
- **Problems in `OpenMuseSource`:**
  1. **Previous `BleakClient` is not disconnected before reconnect:** When `run()` raises `SourceDisconnected("BLE link lost")`, `Session._run_source()` calls `_reconnect()`, which calls `src.plugin.open()`. `open()` immediately overwrites `self._client = bleak.BleakClient(...)` without disconnecting/cleaning up the old `self._client` instance or clearing `self._pending`.
  2. **Packets arriving during reconnect `initialize_device` receive wrong sample indices:** During `open()` on reconnect, `self._live` is `False` (line 141). Notifications are started (line 156) and `await MuseS.initialize_device(self._client, cfg.preset)` runs (line 162), which takes ~1–2 s during which BLE notifications arrive and are appended to `self._pending` (`openmuse.py:222`). Then lines 165–169 advance `st.index += int(outage * st.clock.rate)` using `time.time()` *after* `initialize_device` finished (so `outage` includes the `initialize_device` duration), and when `run()` subsequently drains `self._pending`, those packets are decoded starting from the already-incremented `st.index`!
  3. **Wall-clock vs. monotonic clock:** `self._last_disconnect = time.time()` (line 280) and `outage = time.time() - ...` (line 165) use wall-clock time instead of `time.monotonic()`, contrary to [`AGENTS.md`](../AGENTS.md) (*"Use monotonic clocks to measure gaps"*).
- **Problems in `MindMonitor`:**
  1. **Outages longer than 60 seconds produce zero index gap:** In [`_Timeline.place`](../src/muse_monitor/plugins/mindmonitor.py#L70-L96):
     ```python
     off = arrival - (self.index + n) / self.rate
     while self.window and arrival - self.window[0][0] > 60.0:
         self.window.popleft()
     best = min([o for _, o in self.window] + [off])
     deficit = (off - best) * self.rate
     ```
     If Mind Monitor stops sending for `> 60 s` (e.g., phone screen lock or Wi-Fi drop, triggering `SourceDisconnected` after `silence_s = 10 s` and reconnecting 65 s later), `self.window` is completely emptied by the `> 60.0` check! Then `best` becomes `off`, so `deficit = (off - best) * self.rate` is **`0.0`**. No gap is inferred (`self.index` does not advance), while `self.last_t` is 65 s old so `t` jumps by 65 s without an index jump!
  2. **`_last_eeg` is not reset if non-EEG packets arrive or during reconnect loop:** In `open()` on reconnect (line 199), `while not self._buffers.get("/muse/eeg")` waits for one EEG message and returns at line 209, which works, but `_Timeline` should keep at least its last anchor `(arrival, off)` across `> 60 s` silences until `deficit` has been evaluated, or `open()` on reconnect should advance `_Timeline.index` by the silence duration.

---

### 3.4 [MEDIUM] `Baseline` → `BandPower` Race in Offline Mode & Missing Pre-Calibration `norm`

- **Location:** [`src/muse_monitor/plugins/baseline.py:78`](../src/muse_monitor/plugins/baseline.py#L78), [`src/muse_monitor/plugins/bandpower.py:61-68, 100-102`](../src/muse_monitor/plugins/bandpower.py#L61-L68), [`src/muse_monitor/kernel/facade.py:332-355`](../src/muse_monitor/kernel/facade.py#L332-L355)
- **Problem:**
  1. **Async queue race during fast/offline replay:** Each analyzer has its own `Subscriber` queue (`OFFLINE_QUEUE = 32` chunks) and its own `_analyzer_worker` task. During offline analysis (`speed = 0.0`), `Replay` emits `calibration.end` followed immediately by up to 32 EEG chunks (8 seconds of data at `chunk_s = 0.25`) into both `Baseline`'s queue and `BandPower`'s queue before `Baseline`'s worker task runs `_finish()` and calls `self.ctx.emit(Event("baseline.ready", ...))`. That `baseline.ready` event is appended to the *tail* of `BandPower`'s queue—behind the EEG chunks that were already enqueued! Depending on task scheduling and whether `BandPower` or `Baseline` gets CPU time first, `BandPower` processes several seconds of post-calibration EEG before seeing `baseline.ready`.
  2. **First 2 minutes of offline re-analysis lack `bandpower/norm`:** During a live session, `baseline` is only known after the 2-minute calibration finishes, so `bandpower/norm` starts at $t = 120\text{ s}$. However, the resulting baseline is saved in `session.json` (`info.baseline`). When the user runs `muse analyze <ID>` (offline analysis of the whole recording), `BandPower` starts with `self.baseline = None` and waits for `calibration.end` at $t = 120\text{ s}$ again, leaving the eyes-open and eyes-closed calibration segments themselves without `bandpower/norm` values (so `describe`'s segment summaries for the calibration periods cannot report `× baseline` even in offline analysis).
- **Fix:**
  - In `KernelFacade.run_offline`, if `parent.baseline` is already present in the recorded `session.json`, emit `Event("baseline.ready", parent.started_at.timestamp(), data=parent.baseline)` at session start (or pass initial session metadata via `Context`) so `bandpower/norm` is computed for the entire offline replay from $t = 0$.

---

### 3.5 [MEDIUM] `train-probe` vs. `describe` Verdict Mismatch & Unfiltered Bad-Contact Windows

- **Location:** [`src/muse_monitor/probe.py:58-90, 116-136, 164-166`](../src/muse_monitor/probe.py#L58-L90) vs. [`src/muse_monitor/plugins/describe.py:171-183`](../src/muse_monitor/plugins/describe.py#L171-L183)
- **Problem:**
  As noted in [`docs/pending.md`](../docs/pending.md#L102-L110), there are two gaps in `probe.py`:
  1. **Inconsistent validation rule:** `probe.py:135` sets:
     ```python
     out["model_beats_baseline"] = out["model_mean"] > out["baseline_mean"]
     ```
     and prints a green `"The probe beats the band-power baseline."` verdict even if the margin is `0.001` or only 1 of 3 sessions won. Meanwhile, [`Describe.setup`](../src/muse_monitor/plugins/describe.py#L175-L177) checks:
     ```python
     ok = (str(v.get("scheme", "")).startswith("session")
           and (v.get("model_mean") or 0) - (v.get("baseline_mean") or 0) >= c.probe_min_margin
           and v.get("folds", 0) > 0 and v.get("model_fold_wins", 0) * 2 > v["folds"])
     ```
     If a user trains a probe that beats band power by `0.03` (or on a single session via time-blocks), `muse train-probe` tells them it succeeded, but `describe` refuses to use it.
  2. **No quality gating during probe training:** `probe.collect()` (`probe.py:67-70`) runs only the model plugin and `bandpower(..., use_quality=False)` without `quality`. Windows where an electrode was flat (`MASK_FLAT`) or railing (`MASK_POOR_CONTACT`) are included in training and evaluation, injecting high-amplitude artifact variance into both `StandardScaler` and `LogisticRegression`.
- **Fix:**
  - Include `PluginEntry(plugin="quality", id="quality")` in `probe.collect()`'s offline pipeline, read `quality/usable`, and require `usable >= min_usable` (e.g., `0.5` or `1.0`) in `keep` (`probe.py:86`).
  - Unify the verdict logic in `probe.cross_validate()` with `describe.py` (`scheme == "session-held-out"`, `model_mean - baseline_mean >= 0.05`, and `model_fold_wins * 2 > len(folds)`), and include `model_fold_wins` directly in `cv`.

---

### 3.6 [MEDIUM] Edge Transients from Per-Window `sosfiltfilt` in `CBraMod.preprocess`

- **Location:** [`src/muse_monitor/plugins/cbramod.py:47-67`](../src/muse_monitor/plugins/cbramod.py#L47-L67)
- **Code:**
  ```python
  def preprocess(self, w: Window, rate: float) -> np.ndarray:
      from scipy.signal import resample_poly, sosfiltfilt
      x = w.data.astype(np.float64).T                              # (channels, samples), µV
      x = x - x.mean(axis=1, keepdims=True)
      x = sosfiltfilt(self._sos(rate), x, axis=1)
  ```
- **Problem:**
  `self._sos(rate)` is a 4th-order Butterworth bandpass filter with a low cutoff of **$0.3\text{ Hz}$** (effective 8th-order zero-phase filter under `sosfiltfilt`). A $0.3\text{ Hz}$ high-pass filter has an impulse response settling time of $\sim 1.5\text{–}2.0\text{ s}$. Applying `sosfiltfilt` independently to each isolated $4.0\text{ s}$ window (`1024` samples at `256 Hz`), especially when low-frequency sweat drift or a blink sits near the start or end of the window, causes boundary ringing across the first and last patch of the 4-second window.
- **Fix:**
  Either maintain continuous causal filter state across chunks (like `Filter` does with `sosfilt` + `sosfilt_zi`, resetting on `gap=True`) before windowing, or pass `padtype="even", padlen=min(512, x.shape[1] - 1)` to `sosfiltfilt`.

---

### 3.7 [MEDIUM] Localhost CSRF Surface & Unbounded `KernelFacade.jobs` Memory Retention

- **Locations:**
  - [`src/muse_monitor/interfaces/http.py:58-79, 134-189, 243-255`](../src/muse_monitor/interfaces/http.py#L58-L79)
  - [`src/muse_monitor/kernel/facade.py:130, 350-375`](../src/muse_monitor/kernel/facade.py#L350-L375)
- **Problems:**
  1. **Cross-origin requests to `127.0.0.1:8765`:** When `muse serve` binds to `127.0.0.1` (default), `token` is `None`. While browser Same-Origin Policy blocks cross-origin pages from *reading* responses, any website open in another tab in the user's browser can send cross-origin `POST` requests without preflight if they don't require `application/json` (e.g., `POST /api/v1/sessions/{id}/stop`, `POST /api/v1/models/cbramod/pull`, `POST /api/v1/sessions/import` with a raw body, or opening a `WebSocket` to `ws://127.0.0.1:8765/api/v1/sessions/current/live` to inject markers).
  2. **Unbounded `self.jobs` dictionary in `KernelFacade`:** Every `run_offline()` call creates a `Job(id=..., session=session)` and stores it in `self.jobs[job.id]` (`facade.py:353`). Completed jobs are never evicted, and `job.session` holds `session.snapshot` (which stores up to `50,000` rows per derived stream and `50,000` events in Python lists). Over many offline analyses or `train-probe` runs in a long-lived `muse serve` process, this leaks tens of megabytes per job.
- **Fix:**
  - In `http.py`, add a lightweight check in middleware or `auth()` rejecting state-changing requests (`POST`, `PATCH`, `DELETE`) and WebSocket handshakes when an `Origin` header is present and its host does not match the server's `Host` header.
  - In `KernelFacade._run_job()` (`facade.py:375`), set `job.session = None` in the `finally:` block after copying `job.session.info.id` to `job` (since `to_dict()` only needs `job.session` for live progress while `status == "running"`), and cap `self.jobs` to the most recent ~100 jobs.

---

### 3.8 [LOW] Implicit Analyzer Ordering vs. Dependency Resolution

- **Location:** [`src/muse_monitor/kernel/session.py:226-227, 258-275`](../src/muse_monitor/kernel/session.py#L226-L227)
- **Problem:**
  Analyzers are set up in the exact order they appear in `Pipeline.analyzer`:
  - `bandpower` has `inputs = [StreamSelector(kind="eeg"), StreamSelector(kind="mask", optional=True)]`.
  - If a user runs `muse run -a bandpower -a quality` (or orders `[[analyzer]]` blocks that way in TOML), `bandpower.setup()` runs before `quality.setup()`, so `quality/mask` is not in `self.streams` yet. Because `optional=True` is set on the mask selector, `bandpower` silently runs without quality masking (`self.mask_stream = None`) even though `quality` is active in the same session!
  - Similarly, `describe` has `optional=True` on its `StreamSelector(kind="derived", optional=True)` and raises `IncompatibleInput("needs bandpower/mean_rel: list bandpower before describe")` if placed before `bandpower`, or silently ignores `quality/usable` if placed before `quality`.
- **Fix:**
  Either topologically sort analyzers (or enforce canonical ordering for known dependencies like `quality` → `baseline` → `bandpower` / `artifacts` → `cbramod` → `describe`, as `app.js:191` already does in the Web UI), or log a clear warning when a later analyzer declares a stream that matches an earlier analyzer's `optional=True` selector.

---

### 3.9 [LOW] Unbounded State & $O(N^2)$ Scans in `Describe` + Hard Cutoff in `Snapshot`

- **Locations:**
  - [`src/muse_monitor/plugins/describe.py:184-230`](../src/muse_monitor/plugins/describe.py#L184-L230)
  - [`src/muse_monitor/kernel/session.py:102-119`](../src/muse_monitor/kernel/session.py#L102-L119)
- **Problem:**
  1. `Describe` stores every sample of `bandpower/mean_rel`, `bandpower/norm`, `quality/usable`, and `cbramod/probs` in `self.rows[sid]` as a Python list of `(float, np.ndarray)` tuples for the entire session (`describe.py:210`), and `_mean(sid, t0, t1)` scans the entire list `[r for t, r in self.rows.get(sid, []) if t0 <= t <= t1]` on every 30-second emission. For a 30-minute session this is tiny (~3,600 tuples), but for an 8-hour overnight sleep session it performs repeated linear scans over ~60,000 tuples.
  2. `Snapshot.add` (`session.py:116`) stops appending once `len(rows) >= SNAPSHOT_MAX_ROWS` (`50,000`). At `2 Hz` (`bandpower`), `50,000` rows is ~6.9 hours; beyond that, HTML reports silently truncate the tail of the session rather than decimating.
- **Fix:**
  - In `Describe._mean`, use `bisect` on a parallel list of timestamps (or slice from the tail during live `every_s` emissions).
  - In `Snapshot.add`, when `len(rows) >= SNAPSHOT_MAX_ROWS`, decimate existing rows by $2\times$ (`rows[:] = rows[::2]`) so the full session span is always preserved.

---

### 3.10 [LOW] CLI & `RemoteFacade` Polish Items

- **Locations:**
  - [`src/muse_monitor/interfaces/cli.py:166-170, 638-648`](../src/muse_monitor/interfaces/cli.py#L638-L648)
  - [`src/muse_monitor/interfaces/client.py:18-24, 150-151`](../src/muse_monitor/interfaces/client.py#L18-L24)
- **Problems:**
  1. **`muse analyze ID --report out.html --server URL` silently drops `--report`:** In `cli.py:638`:
     ```python
     if name and report and not server:
         from ..kernel import KernelFacade
         assert isinstance(f, KernelFacade)
         report.write_bytes(f.report_path(session_id, name).read_bytes())
     ```
     When `--server` is used, `--report` is silently ignored because `RemoteFacade` has no `report_bytes(session_id, name)` method (even though `GET /api/v1/sessions/{sid}/reports/{name}` exists on the HTTP server!).
  2. **`RemoteFacade.close()` is never called by the CLI:** `cli.py:48` instantiates `RemoteFacade(server, token)` inside `asyncio.run(go())` without closing its `httpx.AsyncClient` before the event loop closes.
  3. **Web UI `sessions` endpoint loads full `session.json` for every session on disk:** `KernelFacade.list_sessions` (`facade.py:250-266`) reads and parses every `*/session.json` on every `GET /api/v1/sessions` request. This is fast for dozens of sessions, though caching by `(path, mtime_ns)` would keep it instantaneous with hundreds of sessions.

---

## 4. Testing & Verification Review

### Strengths
- **Contract-driven plugin testing (`sdk.testing`):** `check_plugin(cls)` verifies manifest attributes, Pydantic JSON schema generation, and `stop()` idempotence (both with and without `start()`) across every built-in plugin (`test_builtin_plugins_pass_check`).
- **Known-spectrum DSP tests:** `test_bandpower_peak`, `test_filter_removes_drift_keeps_alpha`, `test_quality_tolerates_ordinary_mains_hum_but_not_extreme_hum_or_rails`, and `test_artifacts_ignore_mains_hum` test real edge cases discovered on hardware (e.g., 50 Hz hum via Mind Monitor and stuck AUX rails on Athena).
- **Hardware regression tests without hardware:** `test_openmuse_handles_settle_backlog_before_new_packets`, `test_openmuse_eeg_is_centred_on_zero`, `test_edf_opens_in_mne_aligned_with_annotations`, and `test_mindmonitor.py` reproduce real protocol/timing subtleties deterministically.

### Gaps to Address
1. **No reconnect unit tests:** Neither `OpenMuseSource` nor `MindMonitor` has a test simulating `SourceDisconnected` followed by `Session._reconnect()` (which would immediately catch finding **3.3**).
2. **Band power sum-to-one test:** `test_bandpower_peak` only checks `argmax()`, which is why the missing boundary bins in `band_powers()` (finding **3.1**) went unnoticed. Adding `assert np.allclose(out["mean_rel"].data.sum(axis=1), 1.0, atol=0.02)` on a broadband signal will lock in the fix.
3. **No CI workflow or static type checking config:** [`spec/roadmap.md` §3](../spec/roadmap.md#L31-L33) specifies `uv run pytest -m "not device"`, `ruff`, and `pyright --strict` on `sdk/` and `kernel/`, but neither `.github/workflows/` nor `[tool.ruff]` / `[tool.pyright]` in `pyproject.toml` is configured yet.
4. **No `samples/` fixture captures:** `AGENTS.md` and `spec/roadmap.md` call for anonymized short captures in `samples/` for golden replay tests.

---

## 5. Prioritized Recommendations

### Priority 1 — Fix Before Recording M5 Validation Sessions
1. **Fix `band_powers` boundary integration (`sdk/helpers.py:122`):** Change `(freqs >= lo) & (freqs < hi)` to `(freqs >= lo) & (freqs <= hi)` so adjacent bands share boundary endpoints and relative band powers sum to 100%.
2. **Unify `train-probe` quality gating & verdict (`probe.py`):**
   - Run `quality` during `probe.collect()` and exclude windows where usable electrode share is below threshold.
   - Apply the same validation criterion in `probe.cross_validate()` as `describe.py` (`model_mean - baseline_mean >= 0.05` and majority of held-out sessions won).
3. **Reduce boundary transients in `CBraMod.preprocess` (`plugins/cbramod.py:61`):** Pass `padtype="even", padlen=min(512, x.shape[1] - 1)` to `sosfiltfilt` (or filter continuously before windowing).

### Priority 2 — Security & Reconnect Hardening
4. **Restrict `out` path in `POST /api/v1/probes` (`interfaces/http.py`, `probe.py`):** Confine HTTP-triggered probe output paths to a managed directory under `data_dir` and add report/probe download support to `RemoteFacade`.
5. **Protect localhost HTTP/WS against cross-origin requests (`interfaces/http.py`):** Reject state-changing HTTP methods and WebSocket upgrades when an `Origin` header is present and does not match `Host`.
6. **Fix `OpenMuseSource` and `MindMonitor` reconnect state (`plugins/openmuse.py`, `plugins/mindmonitor.py`):**
   - Disconnect any old `BleakClient` before re-creating it, use `time.monotonic()` for outage measurement, and advance `st.index` *before* starting notifications on reconnect.
   - Preserve the last offset anchor in `MindMonitor._Timeline` across outages `> 60 s` so long disconnects still advance `self.index`.

### Priority 3 — Lifecycle & Developer Experience
7. **Seed `baseline` on offline runs & release `Job.session` after completion (`kernel/facade.py`):** Emit `baseline.ready` at the start of `run_offline` when `parent.baseline` exists in `session.json`, and clear `job.session = None` once `_run_job` finishes to free `Snapshot` memory.
8. **Enforce analyzer topological order (`kernel/session.py`):** Sort analyzers by stream dependencies (or warn when an `optional=True` selector matches a stream declared by a later analyzer in the pipeline).
9. **Add `[tool.ruff]` and `[tool.pyright]` to `pyproject.toml` and a GitHub Actions workflow.**

