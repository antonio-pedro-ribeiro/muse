"""Eyes-open / eyes-closed calibration -> per-session baseline for normalising band powers."""

from __future__ import annotations

import time

import numpy as np
from pydantic import BaseModel, Field

from muse_monitor.sdk import (DEFAULT_BANDS, Analyzer, Chunk, Event, IncompatibleInput, Report, StreamInfo,
                              StreamSelector, ViewSpec, Windower, band_powers, eeg_channels, welch_psd)

CONDITIONS = {"calibration.eyes_open": "eyes_open", "calibration.eyes_closed": "eyes_closed"}


class Baseline(Analyzer):
    name = "baseline"
    version = "1.0.0"
    summary = "Eyes-open/closed calibration -> baseline band powers (emits baseline.ready)"
    inputs = [StreamSelector(kind="eeg")]
    requires = ["scipy"]

    class Config(BaseModel):
        window_s: float = Field(2.0, description="Seconds of signal per estimate.")
        step_s: float = Field(1.0, description="Seconds between estimates.")
        settle_s: float = Field(3.0, description="Seconds ignored after each instruction while you settle.")
        bands: dict[str, tuple[float, float]] = Field(default_factory=lambda: dict(DEFAULT_BANDS),
                                                      description="Frequency bands in Hz: name -> [low, high].")
        channels: list[str] | None = Field(None, description="Electrodes to use; empty = all except AUX inputs.")

    def setup(self, inputs: dict[str, StreamInfo]) -> list[StreamInfo]:
        if len(inputs) != 1:
            raise IncompatibleInput(f"expected one EEG stream, got {list(inputs)}")
        (self.src,) = inputs.values()
        self.idx = eeg_channels(self.src.channels, self.config.channels)
        self.chans = [self.src.channels[i] for i in self.idx]
        self.win = Windower(self.config.window_s, self.config.step_s, self.src.rate)
        self.state: str | None = None
        self.state_t = 0.0
        self.powers: dict[str, list[np.ndarray]] = {"eyes_open": [], "eyes_closed": []}
        self.result: dict | None = None
        return []

    async def on_event(self, event: Event) -> None:
        if event.type in CONDITIONS:
            self.state, self.state_t = CONDITIONS[event.type], event.t
            self.powers[self.state] = []
            self.win.reset()
        elif event.type == "calibration.end":
            self.state = None
            self._finish(event.t)

    async def process(self, chunk: Chunk, gap: bool) -> None:
        if self.state is None:
            return
        if gap:
            self.win.reset()
        for w in self.win.push(chunk):
            if w.t_start >= self.state_t + self.config.settle_s:
                f, p = welch_psd(w.data[:, self.idx], self.src.rate)
                self.powers[self.state].append(band_powers(f, p, self.config.bands))

    def _finish(self, t: float) -> None:
        if not self.powers["eyes_open"] or not self.powers["eyes_closed"]:
            self.ctx.emit(Event("baseline.failed", t, data={"reason": "not enough data in one of the conditions"}))
            return
        bands = list(self.config.bands)
        result: dict = {"bands": self.config.bands, "channels": self.chans}
        for cond, rows in self.powers.items():
            med = np.median(np.stack(rows), axis=0)          # (bands, channels), robust to artifacts
            result[cond] = {c: {b: float(med[i, j]) for i, b in enumerate(bands)} for j, c in enumerate(self.chans)}
            result[f"{cond}_windows"] = len(rows)
        if "alpha" in bands:
            eo = np.mean([result["eyes_open"][c]["alpha"] for c in self.chans])
            ec = np.mean([result["eyes_closed"][c]["alpha"] for c in self.chans])
            result["alpha_reactivity"] = float(ec / eo) if eo > 0 else None
        self.result = result
        self.ctx.emit(Event("baseline.ready", t or time.time(), data=result))

    def views(self) -> list[ViewSpec]:
        return [ViewSpec(id="events", title="Calibration", section="events", type="events",
                         description="Progress of the 2-minute calibration: 60 s eyes open, then 60 s eyes closed. "
                                     "The result is a personal baseline used to normalise band power. Alpha "
                                     "reactivity (closed vs. open) above 1 is typical.",
                         event_types=["calibration.", "baseline."])]

    async def summarize(self) -> Report | None:
        if self.result is None:
            return None
        return Report(title="Baseline", metrics={
            "alpha_reactivity": self.result.get("alpha_reactivity"),
            "eyes_open_windows": self.result["eyes_open_windows"],
            "eyes_closed_windows": self.result["eyes_closed_windows"]},
            notes=["Alpha reactivity = mean alpha power eyes closed / eyes open. Values above 1 are typical."])
