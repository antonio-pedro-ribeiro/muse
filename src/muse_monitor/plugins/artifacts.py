"""Artifact detection: blinks and jaw clench (EEG), motion (accelerometer) -> events."""

from __future__ import annotations

import numpy as np
from pydantic import BaseModel, Field

from muse_monitor.sdk import (Aligner, Analyzer, Chunk, Event, IncompatibleInput, Report, StreamInfo, StreamSelector,
                              ViewSpec, Windower)


class Artifacts(Analyzer):
    name = "artifacts"
    version = "1.0.0"
    summary = "Blink / jaw clench (EEG amplitude) and motion (IMU) -> artifact.* events"
    inputs = [StreamSelector(kind="eeg"), StreamSelector(kind="acc", optional=True)]

    class Config(BaseModel):
        window_s: float = Field(0.5, description="Seconds of signal checked at a time.")
        step_s: float = Field(0.25, description="Seconds between checks.")
        frontal: list[str] = Field(default_factory=lambda: ["AF7", "AF8", "FPz"],
                                   description="Forehead electrodes, where blinks show up.")
        blink_uv: float = Field(150.0, description="Forehead swing (µV, peak-to-peak) that counts as a blink.")
        jaw_uv: float = Field(15.0, description="High-frequency muscle noise (µV above 30 Hz) that counts as jaw "
                                                "clenching.")
        motion_g: float = Field(0.05, description="Accelerometer variation (g) that counts as head movement.")
        merge_s: float = Field(0.5, description="Detections closer than this (seconds) merge into one event.")

    def setup(self, inputs: dict[str, StreamInfo]) -> list[StreamInfo]:
        eeg = [s for s in inputs.values() if s.kind == "eeg"]
        if len(eeg) != 1:
            raise IncompatibleInput(f"expected one EEG stream, got {[s.id for s in eeg]}")
        self.src = eeg[0]
        self.acc = next((s for s in inputs.values() if s.kind == "acc"), None)
        self.front = [i for i, c in enumerate(self.src.channels) if c in self.config.frontal]
        self.chans = [i for i, c in enumerate(self.src.channels) if not c.upper().startswith("AUX")]
        self.win = Windower(self.config.window_s, self.config.step_s, self.src.rate)
        self.align = Aligner(max_age_s=5.0)
        self.open: dict[str, list[float]] = {}         # type -> [start, end, peak]
        self.counts: dict[str, int] = {}
        self.t_first: float | None = None
        self.t_last = 0.0
        return []

    async def process(self, chunk: Chunk, gap: bool) -> None:
        if self.acc is not None and chunk.stream == self.acc.id:
            if gap:
                self.align.reset(chunk.stream)
            self.align.push(chunk)
            return
        if gap:
            self.win.reset()
        for w in self.win.push(chunk):
            self.t_first = w.t_start if self.t_first is None else self.t_first
            self.t_last = w.t_end
            self._detect(w.data, w.t_start, w.t_end)

    def _detect(self, x: np.ndarray, t0: float, t1: float) -> None:
        c = self.config
        x = x - x.mean(axis=0)
        if self.front:
            ptp = float(np.ptp(x[:, self.front], axis=0).max())
            if ptp > c.blink_uv:
                self._hit("artifact.blink", t0, t1, ptp)
        if self.chans and self.src.rate > 64:
            spec = np.fft.rfft(x[:, self.chans], axis=0)
            f = np.fft.rfftfreq(x.shape[0], 1.0 / self.src.rate)
            hf = (f >= 30) & (f <= min(100.0, self.src.rate / 2 - 1))
            rms = float(np.sqrt(2 * (np.abs(spec[hf]) ** 2).sum(axis=0) / x.shape[0] ** 2).mean())
            if rms > c.jaw_uv:
                self._hit("artifact.jaw", t0, t1, rms)
        if self.acc is not None:
            a = self.align.slice(self.acc.id, t0, t1)
            if len(a) >= 3:
                mag_std = float(np.linalg.norm(a, axis=1).std())
                if mag_std > c.motion_g:
                    self._hit("artifact.motion", t0, t1, mag_std)
        self._close_stale(t0)

    def _hit(self, kind: str, t0: float, t1: float, value: float) -> None:
        cur = self.open.get(kind)
        if cur and t0 <= cur[1] + self.config.merge_s:
            cur[1], cur[2] = max(cur[1], t1), max(cur[2], value)
        else:
            if cur:
                self._emit(kind, cur)
            self.open[kind] = [t0, t1, value]

    def _close_stale(self, now: float) -> None:
        for kind, cur in list(self.open.items()):
            if now > cur[1] + self.config.merge_s:
                self._emit(kind, cur)
                del self.open[kind]

    def _emit(self, kind: str, cur: list[float]) -> None:
        self.counts[kind] = self.counts.get(kind, 0) + 1
        self.ctx.emit(Event(kind, cur[0], duration=cur[1] - cur[0], data={"peak": round(cur[2], 3)}))

    def views(self) -> list[ViewSpec]:
        return [ViewSpec(id="events", title="Artifacts", section="events", type="events", event_types=["artifact."],
                         description="Moments when the signal was disturbed by something other than brain "
                                     "activity: blinks (large frontal spikes), jaw clenching (high-frequency muscle "
                                     "noise) and head movement (accelerometer). Some are normal; many in a row "
                                     "mean the data from that period is less reliable.")]

    async def summarize(self) -> Report | None:
        for kind, cur in list(self.open.items()):
            self._emit(kind, cur)
        self.open.clear()
        minutes = (self.t_last - self.t_first) / 60 if self.t_first is not None else 0
        if minutes <= 0:
            return None
        metrics: dict[str, float | int | str | None] = {"minutes": round(minutes, 2)}
        for kind in ("artifact.blink", "artifact.jaw", "artifact.motion"):
            n = self.counts.get(kind, 0)
            metrics[f"{kind.split('.')[1]}_count"] = n
            metrics[f"{kind.split('.')[1]}_per_min"] = round(n / minutes, 2)
        return Report(title="Artifacts", metrics=metrics)
