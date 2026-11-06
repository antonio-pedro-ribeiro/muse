"""Signal helpers for analyzers: windowing, multi-rate alignment, quality masks."""

from __future__ import annotations

from collections.abc import Iterator

import numpy as np

from .types import Chunk, Window

# Values of the standard quality/mask stream.
MASK_OK, MASK_POOR_CONTACT, MASK_NOISY, MASK_FLAT = 0, 1, 2, 3
MASK_LABELS = {MASK_OK: "ok", MASK_POOR_CONTACT: "poor contact", MASK_NOISY: "noisy", MASK_FLAT: "flat"}


class Windower:
    """Accumulates chunks and yields overlapping fixed-size windows."""

    def __init__(self, size_s: float, step_s: float, rate: float) -> None:
        if rate <= 0:
            raise ValueError("Windower needs a regular stream (rate > 0)")
        self.size = max(1, round(size_s * rate))
        self.step = max(1, round(step_s * rate))
        self.reset()

    def reset(self) -> None:
        self._data: np.ndarray | None = None
        self._t: np.ndarray | None = None

    def push(self, chunk: Chunk) -> Iterator[Window]:
        if self._data is None or self._t is None:
            self._data, self._t = chunk.data, chunk.t
        else:
            self._data = np.concatenate([self._data, chunk.data])
            self._t = np.concatenate([self._t, chunk.t])
        while self._data.shape[0] >= self.size:
            yield Window(self._data[: self.size].copy(), self._t[: self.size].copy())
            self._data, self._t = self._data[self.step:], self._t[self.step:]


class Aligner:
    """Keeps a short history of several streams so they can be joined by time."""

    def __init__(self, max_age_s: float = 10.0) -> None:
        self.max_age_s = max_age_s
        self._t: dict[str, np.ndarray] = {}
        self._d: dict[str, np.ndarray] = {}

    def push(self, chunk: Chunk) -> None:
        s = chunk.stream
        if s in self._t:
            t, d = np.concatenate([self._t[s], chunk.t]), np.concatenate([self._d[s], chunk.data])
        else:
            t, d = chunk.t, chunk.data
        keep = t >= t[-1] - self.max_age_s
        self._t[s], self._d[s] = t[keep], d[keep]

    def reset(self, stream: str | None = None) -> None:
        for s in [stream] if stream else list(self._t):
            self._t.pop(s, None)
            self._d.pop(s, None)

    def slice(self, stream: str, t0: float, t1: float) -> np.ndarray:
        """Samples of ``stream`` with t0 <= t <= t1, shape (n, channels); empty if none."""
        if stream not in self._t:
            return np.empty((0, 0), dtype=np.float32)
        m = (self._t[stream] >= t0) & (self._t[stream] <= t1)
        return self._d[stream][m]

    def at(self, stream: str, t: np.ndarray) -> np.ndarray | None:
        """``stream`` linearly interpolated at times ``t``; None if no data."""
        if stream not in self._t or len(self._t[stream]) == 0:
            return None
        st, sd = self._t[stream], self._d[stream]
        return np.stack([np.interp(t, st, sd[:, c]) for c in range(sd.shape[1])], axis=1)


class QualityMask:
    """Reads the standard ``quality/mask`` stream (one value per EEG channel, 1 Hz)."""

    def __init__(self, channels: list[str], max_age_s: float = 3.0) -> None:
        self.channels = channels
        self.max_age_s = max_age_s
        self._t: float | None = None
        self._mask: dict[str, int] = {}

    def push(self, chunk: Chunk, mask_channels: list[str]) -> None:
        row = chunk.data[-1]
        self._t = float(chunk.t[-1])
        self._mask = {ch: int(v) for ch, v in zip(mask_channels, row)}

    @property
    def available(self) -> bool:
        return self._t is not None

    def ok(self, t: float) -> np.ndarray:
        """Boolean per channel: True if the latest mask (not older than max_age_s) says ok.
        Without any mask data every channel counts as ok."""
        if self._t is None:
            return np.ones(len(self.channels), dtype=bool)
        if t - self._t > self.max_age_s:
            return np.zeros(len(self.channels), dtype=bool)
        return np.array([self._mask.get(ch, MASK_OK) == MASK_OK for ch in self.channels])


DEFAULT_BANDS: dict[str, tuple[float, float]] = {
    "delta": (1.0, 4.0), "theta": (4.0, 8.0), "alpha": (8.0, 13.0), "beta": (13.0, 30.0), "gamma": (30.0, 45.0),
}


def welch_psd(data: np.ndarray, rate: float, segment_s: float = 1.0) -> tuple[np.ndarray, np.ndarray]:
    """Welch PSD per channel: returns (freqs, psd[freqs, channels]) in unit^2/Hz."""
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


def eeg_channels(channels: list[str], wanted: list[str] | None = None) -> list[int]:
    """Indices of the channels to analyse: ``wanted`` if given, else everything except AUX inputs."""
    if wanted:
        missing = [c for c in wanted if c not in channels]
        if missing:
            raise ValueError(f"channels {missing} not in stream {channels}")
        return [channels.index(c) for c in wanted]
    return [i for i, c in enumerate(channels) if not c.upper().startswith("AUX")]
