"""Welch band powers: absolute, relative, and normalised to the session baseline."""

from __future__ import annotations

import numpy as np
from pydantic import BaseModel, Field

from muse_monitor.sdk import (DEFAULT_BANDS, Analyzer, Axis, Chunk, Event, IncompatibleInput, QualityMask, Report,
                              StreamInfo, StreamSelector, ViewSpec, Windower, band_powers, eeg_channels, welch_psd)


class BandPower(Analyzer):
    name = "bandpower"
    version = "1.0.0"
    summary = "Welch PSD -> absolute, relative and baseline-normalised band powers"
    inputs = [StreamSelector(kind="eeg"), StreamSelector(kind="mask", optional=True)]
    requires = ["scipy"]

    class Config(BaseModel):
        window_s: float = Field(2.0, description="Seconds of signal per estimate. Longer is steadier but slower.")
        step_s: float = Field(0.5, description="How often band power is updated (seconds).")
        bands: dict[str, tuple[float, float]] = Field(default_factory=lambda: dict(DEFAULT_BANDS),
                                                      description="Frequency bands in Hz: name -> [low, high].")
        total: tuple[float, float] = Field((1.0, 45.0), description="Frequency range (Hz) that relative power is "
                                                                    "a share of.")
        channels: list[str] | None = Field(None, description="Electrodes to use; empty = all except AUX inputs.")
        use_quality: bool = Field(True, description="Average only electrodes that pass the quality check.")

    def setup(self, inputs: dict[str, StreamInfo]) -> list[StreamInfo]:
        eeg = [s for s in inputs.values() if s.kind == "eeg"]
        if len(eeg) != 1:
            raise IncompatibleInput(f"expected one EEG stream, got {[s.id for s in eeg]}")
        self.src = eeg[0]
        self.mask_stream = next((s for s in inputs.values() if s.kind == "mask"), None)
        try:
            self.idx = eeg_channels(self.src.channels, self.config.channels)
        except ValueError as exc:
            raise IncompatibleInput(str(exc)) from exc
        self.chans = [self.src.channels[i] for i in self.idx]
        self.bands = list(self.config.bands)
        self.win = Windower(self.config.window_s, self.config.step_s, self.src.rate)
        self.mask = QualityMask(self.chans)
        self.baseline: np.ndarray | None = None       # (bands, channels) eyes-open absolute power
        self.rel_sum = np.zeros(len(self.bands))
        self.windows = 0
        self.used = 0
        f = np.fft.rfftfreq(max(8, round(self.src.rate)), 1.0 / self.src.rate)
        self.freqs = f[(f >= self.config.total[0]) & (f <= self.config.total[1])]
        rate = 1.0 / self.config.step_s
        pairs = [f"{c}:{b}" for c in self.chans for b in self.bands]
        nb = len(self.bands)
        return [StreamInfo("abs", "derived", pairs, ["uV^2"] * len(pairs), rate),
                StreamInfo("rel", "derived", pairs, ["1"] * len(pairs), rate),
                StreamInfo("mean_rel", "derived", self.bands, ["1"] * nb, rate,
                           meta={"bands": self.config.bands, "channels": self.chans}),
                StreamInfo("norm", "derived", self.bands, ["1"] * nb, rate,
                           meta={"description": "absolute power / eyes-open baseline"}),
                StreamInfo("psd", "derived", [f"{x:g}" for x in self.freqs], ["log10 uV^2/Hz"] * len(self.freqs),
                           rate, meta={"freqs": self.freqs.tolist()})]

    async def on_event(self, event: Event) -> None:
        if event.type == "baseline.ready":
            eo = event.data.get("eyes_open", {})
            try:
                self.baseline = np.array([[eo[c][b] for c in self.chans] for b in self.bands])
            except KeyError:
                self.ctx.log.warning("baseline does not cover channels/bands; ignoring")

    async def process(self, chunk: Chunk, gap: bool) -> None:
        if self.mask_stream is not None and chunk.stream == self.mask_stream.id:
            self.mask.push(chunk, self.mask_stream.channels)
            return
        if gap:
            self.win.reset()
        for w in self.win.push(chunk):
            self._window(w.data[:, self.idx], w.t_end)

    def _window(self, x: np.ndarray, t: float) -> None:
        f, p = welch_psd(x, self.src.rate)
        absp = band_powers(f, p, self.config.bands)                         # (bands, channels)
        lo, hi = self.config.total
        tot = band_powers(f, p, {"total": (lo, hi)})[0]
        rel = absp / np.where(tot > 0, tot, np.nan)
        ok = self.mask.ok(t) if self.config.use_quality else np.ones(len(self.chans), bool)
        self.windows += 1
        if ok.any():
            mean_rel = np.nanmean(rel[:, ok], axis=1)
            self.rel_sum += mean_rel
            self.used += 1
        else:
            mean_rel = np.full(len(self.bands), np.nan)
        sel = ok if ok.any() else np.ones(len(self.chans), bool)
        m = (f >= lo) & (f <= hi)
        psd = np.log10(np.nanmean(p[m][:, sel], axis=1) + 1e-12)
        if len(psd) == len(self.freqs):
            self.ctx.emit(Chunk.single("psd", psd, t))
        self.ctx.emit(Chunk.single("abs", absp.T.reshape(-1), t))
        self.ctx.emit(Chunk.single("rel", rel.T.reshape(-1), t))
        self.ctx.emit(Chunk.single("mean_rel", mean_rel, t))
        if self.baseline is not None:
            norm = absp / np.where(self.baseline > 0, self.baseline, np.nan)
            self.ctx.emit(Chunk.single("norm", np.nanmean(norm[:, sel], axis=1), t))

    def views(self) -> list[ViewSpec]:
        return [
            ViewSpec(id="mean_rel", title="Relative band power", type="bars", streams=["mean_rel"],
                     description="Share of the EEG power in each frequency band, averaged over good channels: "
                                 "delta 1-4 Hz, theta 4-8, alpha 8-13, beta 13-30, gamma 30-45. The bars add up to "
                                 "about 1. Alpha usually rises with eyes closed; blinks and movement inflate delta. "
                                 "These are measurements, not mental states.",
                     y=Axis(label="relative power", min=0, max=1)),
            ViewSpec(id="trend", title="Relative band power over time", type="timeseries",
                     description="The same relative band powers over the last 2 minutes, updated every "
                                 f"{self.config.step_s:g} s. Useful to see changes, e.g. when you close your eyes.",
                     streams=["mean_rel"], window_s=120.0, y=Axis(label="relative power", min=0, max=1)),
            ViewSpec(id="psd", title="Spectrum (good channels)", type="spectrum", streams=["psd"],
                     description="Power at each frequency (log scale), averaged over good channels. Peaks show "
                                 "rhythms: a bump near 10 Hz is alpha. A sharp spike at 50 or 60 Hz is mains "
                                 "interference, not brain activity.",
                     y=Axis(label="power", unit="log10 µV²/Hz")),
            ViewSpec(id="norm", title="Band power vs. eyes-open baseline", type="timeseries", streams=["norm"],
                     description="Band power divided by your own eyes-open calibration from this session: 1 means "
                                 "the same as baseline, 2 means twice as much. Empty until you run Calibrate. "
                                 "Comparing to a personal baseline makes values more comparable across sessions.",
                     window_s=120.0, y=Axis(label="× baseline", min=0)),
        ]

    async def summarize(self) -> Report | None:
        if not self.windows:
            return None
        metrics: dict[str, float | int | str | None] = {"windows": self.windows,
                                                        "usable_windows_pct": round(100 * self.used / self.windows, 1)}
        if self.used:
            for b, v in zip(self.bands, self.rel_sum / self.used):
                metrics[f"mean_rel_{b}"] = round(float(v), 4)
        return Report(title="Band power", metrics=metrics,
                      notes=["Relative power averaged over windows where at least one channel had good contact."])
