"""Display filter: band-pass + mains notch. Raw data is never modified."""

from __future__ import annotations

import numpy as np
from pydantic import BaseModel, Field

from muse_monitor.sdk import Analyzer, Axis, Chunk, IncompatibleInput, StreamInfo, StreamSelector, ViewSpec


class Filter(Analyzer):
    name = "filter"
    version = "1.0.0"
    summary = "1-40 Hz band-pass + 50/60 Hz notch for display"
    inputs = [StreamSelector(kind="eeg")]
    requires = ["scipy"]

    class Config(BaseModel):
        low_hz: float = Field(1.0, description="Remove slower changes than this (Hz), e.g. sweat drift.")
        high_hz: float = Field(40.0, description="Remove faster changes than this (Hz), e.g. muscle noise.")
        notch_hz: float = Field(50.0, description="Mains frequency to remove (50 or 60 Hz; 0 = off).")
        order: int = Field(4, description="Filter steepness. Higher is sharper but rings more.")

    def setup(self, inputs: dict[str, StreamInfo]) -> list[StreamInfo]:
        from scipy.signal import butter, iirnotch, tf2sos
        if len(inputs) != 1:
            raise IncompatibleInput(f"expected one EEG stream, got {list(inputs)}")
        (self.src,) = inputs.values()
        c, fs = self.config, self.src.rate
        if not 0 < c.low_hz < c.high_hz < fs / 2:
            raise IncompatibleInput(f"band {c.low_hz}-{c.high_hz} Hz invalid at {fs} Hz")
        sos = butter(c.order, [c.low_hz, c.high_hz], btype="bandpass", fs=fs, output="sos")
        if c.notch_hz and c.notch_hz < fs / 2:
            b, a = iirnotch(c.notch_hz, 30.0, fs=fs)
            sos = np.vstack([sos, tf2sos(b, a)])
        self.sos = sos
        self.zi: np.ndarray | None = None
        return [self.src.replace(id="eeg", kind="eeg_filtered", producer="",
                                 meta={"filter": c.model_dump(), "from": self.src.id})]

    async def process(self, chunk: Chunk, gap: bool) -> None:
        from scipy.signal import sosfilt, sosfilt_zi
        x = chunk.data.astype(np.float64)
        if gap or self.zi is None:
            self.zi = sosfilt_zi(self.sos)[:, :, None] * x[0][None, None, :]
        y, self.zi = sosfilt(self.sos, x, axis=0, zi=self.zi)
        self.ctx.emit(Chunk("eeg", y, chunk.t, chunk.first_index))

    def views(self) -> list[ViewSpec]:
        c = self.config
        return [ViewSpec(id="eeg", title="EEG (filtered)", section="signal", type="timeseries", streams=["eeg"], window_s=5.0,
                         description=f"The EEG voltage of each electrode over the last 5 s, filtered to "
                                     f"{c.low_hz:g}-{c.high_hz:g} Hz" + (f" with a {c.notch_hz:g} Hz mains notch"
                                     if c.notch_hz else "") + " so it is easier to read. Filtering is for display "
                                     "only; the recording keeps the unfiltered signal. Large slow bumps on AF7/AF8 "
                                     "are usually blinks.",
                         y=Axis(label="EEG", unit="µV", min=-100, max=100))]
