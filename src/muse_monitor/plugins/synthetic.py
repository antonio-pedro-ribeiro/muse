"""Synthetic EEG/IMU source: known sines + noise + optional blinks and gaps. For tests and demos."""

from __future__ import annotations

import asyncio
import time

import numpy as np
from pydantic import BaseModel, Field

from muse_monitor.sdk import MUSE_POSITIONS, Chunk, DeviceInfo, Health, Source, StreamInfo


class Synthetic(Source):
    name = "synthetic"
    version = "1.0.0"
    summary = "Sines + noise + fake blinks on N channels; no hardware needed"

    class Config(BaseModel):
        channels: list[str] = Field(default_factory=lambda: ["TP9", "AF7", "AF8", "TP10"],
                                    description="Simulated electrode names.")
        rate: float = Field(256.0, description="Samples per second.")
        sines: dict[float, float] = Field(default_factory=lambda: {10.0: 20.0},
                                          description="Rhythms to generate: frequency in Hz -> amplitude in µV. "
                                                      "10 Hz is in the alpha band.")
        noise_uv: float = Field(5.0, description="Random noise added to every channel (µV).")
        blink_every_s: float = Field(0.0, description="Add a fake blink on frontal channels every N seconds "
                                                      "(0 = none).")
        blink_uv: float = Field(300.0, description="Blink size (µV).")
        duration_s: float = Field(0.0, description="Stop after N seconds (0 = until stopped).")
        chunk_s: float = Field(0.0625, description="Seconds of data per packet.")
        realtime: bool = Field(True, description="Stream at real speed. Off: as fast as possible (tests).")
        imu: bool = Field(True, description="Also simulate the accelerometer.")
        gaps: list[tuple[float, int]] = Field(default_factory=list,
                                              description="Simulated data loss: [[at second, samples lost], ...].")
        seed: int = Field(0, description="Random seed, for repeatable signals.")
        start_time: float | None = Field(None, description="Start timestamp (UTC seconds); default now.")

    def __init__(self, config: Config, ctx) -> None:
        super().__init__(config, ctx)
        self._stop = asyncio.Event()
        self._emitted = 0.0

    async def open(self) -> list[StreamInfo]:
        c = self.config
        self.device = DeviceInfo(id="synthetic", name="Synthetic", model="synthetic")
        streams = [StreamInfo("eeg", "eeg", list(c.channels), ["uV"] * len(c.channels), c.rate,
                              positions={ch: MUSE_POSITIONS[ch] for ch in c.channels if ch in MUSE_POSITIONS},
                              meta={"synthetic": True}),
                   StreamInfo("battery", "battery", ["level"], ["%"], 0.0)]
        if c.imu:
            streams.append(StreamInfo("acc", "acc", ["x", "y", "z"], ["g"] * 3, 52.0))
        return streams

    async def run(self) -> None:
        c = self.config
        rng = np.random.default_rng(c.seed)
        t0 = c.start_time if c.start_time is not None else time.time()
        wall0 = time.monotonic()
        n_ch = len(c.channels)
        phases = rng.uniform(0, 2 * np.pi, size=(len(c.sines), n_ch))
        frontal = np.array([ch.startswith(("AF", "Fp", "FP")) for ch in c.channels])
        step = max(1, round(c.chunk_s * c.rate))
        total = round(c.duration_s * c.rate) if c.duration_s > 0 else None
        gaps = sorted((round(at * c.rate), n) for at, n in c.gaps)
        idx = 0           # sample index including lost samples
        acc_idx = 0
        next_battery = 0.0
        while not self._stop.is_set() and (total is None or idx < total):
            while gaps and idx >= gaps[0][0]:
                idx += gaps.pop(0)[1]
            n = step if total is None else min(step, total - idx)
            if n <= 0:
                break
            rel = (idx + np.arange(n)) / c.rate
            x = rng.normal(0.0, c.noise_uv, size=(n, n_ch))
            for (f, amp), ph in zip(c.sines.items(), phases):
                x += amp * np.sin(2 * np.pi * f * rel[:, None] + ph)
            if c.blink_every_s > 0:
                phase = (rel % c.blink_every_s) - c.blink_every_s / 2
                x[:, frontal] += (c.blink_uv * np.exp(-(phase / 0.08) ** 2))[:, None]
            self.ctx.emit(Chunk("eeg", x, t0 + rel, idx))
            idx += n
            if c.imu:
                acc_end = int(idx / c.rate * 52.0)
                if acc_end > acc_idx:
                    k = acc_end - acc_idx
                    acc = np.tile([0.0, 0.0, 1.0], (k, 1)) + rng.normal(0, 0.003, (k, 3))
                    self.ctx.emit(Chunk("acc", acc, t0 + (acc_idx + np.arange(k)) / 52.0, acc_idx))
                    acc_idx = acc_end
            if rel[-1] >= next_battery:
                self.ctx.emit(Chunk.single("battery", [max(0.0, 100.0 - rel[-1] / 360.0)], t0 + rel[-1]))
                next_battery += 5.0
            self._emitted = rel[-1]
            if c.realtime:
                delay = wall0 + idx / c.rate - time.monotonic()
                if delay > 0:
                    try:
                        await asyncio.wait_for(self._stop.wait(), delay)
                    except TimeoutError:
                        pass
            else:
                await self.ctx.pace()

    async def stop(self) -> None:
        self._stop.set()

    def health(self) -> Health:
        c = self.config
        progress = self._emitted / c.duration_s if c.duration_s > 0 else 0.0
        return Health(counters={"seconds": round(self._emitted, 2), "progress": min(1.0, progress)})
