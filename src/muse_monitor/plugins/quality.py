"""Per-channel signal quality -> the standard quality/mask stream."""

from __future__ import annotations

import numpy as np
from pydantic import BaseModel, Field

from muse_monitor.sdk import (MASK_FLAT, MASK_LABELS, MASK_NOISY, MASK_OK, MASK_POOR_CONTACT, Analyzer, Axis, Chunk,
                              IncompatibleInput, Report, StreamInfo, StreamSelector, Threshold, ViewSpec, Windower,
                              eeg_channels)


class Quality(Analyzer):
    name = "quality"
    version = "1.2.0"            # 1.1: "usable" counts head electrodes only. 1.2: noise judged in the EEG band
                                 # (mains hum removed); hum alone makes a channel noisy only when extreme
    summary = "Per-channel contact/noise check (std, drift, line noise) -> quality/mask"
    inputs = [StreamSelector(kind="eeg")]
    produces = ["mask", "derived"]

    class Config(BaseModel):
        window_s: float = Field(2.0, description="Seconds of signal judged at a time.")
        step_s: float = Field(1.0, description="How often the quality is updated (seconds).")
        flat_uv: float = Field(1.0, description="Variation below this (µV) counts as flat: usually no skin contact.")
        noisy_uv: float = Field(100.0, description="Variation in the EEG band (1-40 Hz, µV) above this counts as "
                                                   "noisy: movement, muscle tension or a loose band.")
        drift_uv: float = Field(500.0, description="Peak-to-peak swing above this (µV) counts as poor contact.")
        line_hz: float = Field(50.0, description="Mains frequency: 50 Hz in Europe, 60 Hz in the Americas.")
        line_uv: float = Field(150.0, description="Mains hum (µV rms at line_hz) above which a channel counts as "
                                                  "noisy. Smaller hum is common near chargers and lamps, is filtered "
                                                  "out and does not affect band power.")

    def setup(self, inputs: dict[str, StreamInfo]) -> list[StreamInfo]:
        if len(inputs) != 1:
            raise IncompatibleInput(f"expected one EEG stream, got {list(inputs)}")
        (self.src,) = inputs.values()
        self.win = Windower(self.config.window_s, self.config.step_s, self.src.rate)
        ch = self.src.channels
        self.head = eeg_channels(ch) or list(range(len(ch)))       # AUX inputs are not on the head
        self.counts = np.zeros((len(ch), 4), dtype=np.int64)
        rate = 1.0 / self.config.step_s
        positions = {c: p for c, p in (self.src.positions or {}).items() if c in ch} or None
        return [StreamInfo("mask", "mask", ch, ["1"] * len(ch), rate, positions=positions,
                           meta={"labels": {str(k): v for k, v in MASK_LABELS.items()}}),
                StreamInfo("std", "derived", ch, ["uV"] * len(ch), rate),
                StreamInfo("usable", "derived", ["fraction"], ["1"], rate)]

    def classify(self, x: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        c = self.config
        x = x - x.mean(axis=0)
        n = x.shape[0]
        power = np.abs(np.fft.rfft(x, axis=0)) ** 2
        f = np.fft.rfftfreq(n, 1.0 / self.src.rate)
        rms = lambda sel: np.sqrt(2 * power[sel].sum(axis=0)) / n          # noqa: E731  (Parseval)
        std = rms((f >= 1) & (f <= 40))                                      # EEG band: drift and mains removed
        hum = rms(np.abs(f - c.line_hz) <= 1.5)
        mask = np.full(x.shape[1], MASK_OK)
        mask[(std > c.noisy_uv) | (hum > c.line_uv)] = MASK_NOISY
        mask[np.ptp(x, axis=0) > c.drift_uv] = MASK_POOR_CONTACT            # swings to the rails: no contact
        mask[x.std(axis=0) < c.flat_uv] = MASK_FLAT
        return mask, std

    async def process(self, chunk: Chunk, gap: bool) -> None:
        if gap:
            self.win.reset()
        for w in self.win.push(chunk):
            mask, std = self.classify(w.data)
            self.counts[np.arange(len(mask)), mask] += 1
            self.ctx.emit(Chunk.single("mask", mask, w.t_end))
            self.ctx.emit(Chunk.single("std", std, w.t_end))
            self.ctx.emit(Chunk.single("usable", [float(np.mean(mask[self.head] == MASK_OK))], w.t_end))

    def views(self) -> list[ViewSpec]:
        c = self.config
        return [
            ViewSpec(id="electrodes", title="Signal strength", section="quality", type="electrodes",
                     streams=["mask"],
                     description="Your head seen from above, nose at the top: each dot is an electrode where it "
                                 "touches you (TP9 and TP10 behind the ears, AF7 and AF8 on the forehead; the grey "
                                 "diamond is the reference point). Solid green: good contact. Half-filled yellow: "
                                 "noisy, often movement or tension. Empty red ring: poor contact. Dashed grey: no "
                                 "signal. The line below says what to adjust."),
            ViewSpec(id="std", title="Signal variation per electrode", section="signal", type="bars", streams=["std"],
                     description="How much each electrode's signal varies in the EEG band (1-40 Hz, standard "
                                 f"deviation over the last {c.window_s:g} s, mains hum removed). Green is a normal "
                                 "range. Near zero means a flat signal, usually "
                                 "no skin contact; very high values mean noise, often movement or a loose band. "
                                 "Moisten the sensors or adjust the band if a channel stays red.",
                     y=Axis(label="std", unit="µV", min=0, max=c.noisy_uv * 1.5),
                     thresholds=[Threshold(max=c.flat_uv, level="bad", label="flat"),
                                 Threshold(min=c.flat_uv, max=c.noisy_uv, level="ok", label="ok"),
                                 Threshold(min=c.noisy_uv, level="bad", label="noisy")]),
            ViewSpec(id="usable", title="Usable channels", section="quality", type="metric", streams=["usable"],
                     description="Share of the electrodes on your head that currently pass the quality check (not "
                                 "flat, not noisy, no large drift, little mains hum). AUX inputs are not counted. "
                                 "Band power averages only use these electrodes.",
                     y=Axis(unit="%", min=0, max=1),
                     thresholds=[Threshold(max=0.5, level="bad"), Threshold(min=0.5, max=0.99, level="warn"),
                                 Threshold(min=0.99, level="ok")]),
        ]

    async def summarize(self) -> Report | None:
        total = self.counts.sum(axis=1)
        if not total.any():
            return None
        metrics: dict[str, float | int | str | None] = {
            f"usable_{ch}_pct": round(100 * self.counts[i, MASK_OK] / total[i], 1)
            for i, ch in enumerate(self.src.channels)}
        metrics["windows"] = int(total[0])
        return Report(title="Signal quality", metrics=metrics,
                      notes=["Percent of windows where each channel was in good contact."])
