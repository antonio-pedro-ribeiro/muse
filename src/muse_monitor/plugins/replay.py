"""Replays a recorded session as a source. Powers offline analysis (same pipeline as live)."""

from __future__ import annotations

import asyncio
import heapq
import json
import time
from pathlib import Path

import numpy as np
from pydantic import BaseModel, Field

from muse_monitor.sdk import Chunk, DeviceInfo, Event, Health, Source, StreamInfo, storage

REPLAYED_EVENTS = ("marker", "calibration.")


class Replay(Source):
    name = "replay"
    version = "1.0.0"
    summary = "Recorded session as a source (offline analysis)"
    requires = ["pyarrow"]

    class Config(BaseModel):
        path: str = Field(description="Folder of the recorded session to replay.")
        speed: float = Field(0.0, description="Playback speed: 1 = real time, 0 = as fast as possible.")
        chunk_s: float = Field(0.25, description="Seconds of data per replayed packet.")
        streams: list[str] | None = Field(None, description="Streams to replay; empty = all raw streams.")
        events: bool = Field(True, description="Also replay markers and calibration events.")

    def __init__(self, config: Config, ctx) -> None:
        super().__init__(config, ctx)
        self._stop = asyncio.Event()
        self._data: dict[str, tuple[StreamInfo, np.ndarray, np.ndarray, np.ndarray]] = {}
        self._events: list[Event] = []
        self._span = (0.0, 0.0)
        self._t = 0.0

    async def open(self) -> list[StreamInfo]:
        d = Path(self.config.path)
        if not (d / "session.json").is_file():
            raise FileNotFoundError(f"not a session directory: {d}")
        info = json.loads((d / "session.json").read_text(encoding="utf-8"))
        if info.get("device"):
            self.device = DeviceInfo.model_validate(info["device"])
        wanted = self.config.streams
        for s in storage.list_streams(d):
            if "/" in s.id or (wanted and s.id not in wanted):
                continue                           # raw streams only
            self._data[s.id] = await self.ctx.run_blocking(storage.read_stream, d, s.id)
        if not self._data:
            raise FileNotFoundError(f"no raw streams in {d}")
        if self.config.events:
            self._events = [Event.from_dict({**e, "data": {**(e.get("data") or {}), "_replayed": True}})
                            for e in storage.read_events(d, derived=False) if e["type"].startswith(REPLAYED_EVENTS)]
        ts = [v[1] for v in self._data.values() if len(v[1])]
        self._span = (min(float(t[0]) for t in ts), max(float(t[-1]) for t in ts))
        return [v[0].replace(producer="", meta={**v[0].meta, "replayed_from": str(d)})
                for v in self._data.values()]

    def _chunks(self):
        """Yields (t_start, kind, payload) for every chunk and event, merged in time order."""
        def stream_iter(sid: str):
            info, t, idx, data = self._data[sid]
            if len(t) == 0:
                return
            step = max(1, round(self.config.chunk_s * info.rate)) if info.rate > 0 else 16
            breaks = np.flatnonzero(np.diff(idx) != 1) + 1         # never let a chunk span a gap
            for seg_a, seg_b in zip(np.r_[0, breaks], np.r_[breaks, len(t)]):
                for a in range(seg_a, seg_b, step):
                    b = min(a + step, seg_b)
                    yield float(t[a]), 1, Chunk(sid, data[a:b], t[a:b], int(idx[a]))
        iters = [stream_iter(sid) for sid in self._data]
        iters.append((e.t, 0, e) for e in self._events)
        return heapq.merge(*iters, key=lambda x: (x[0], x[1]))

    async def run(self) -> None:
        wall0, t_first = time.monotonic(), self._span[0]
        for t, _, item in self._chunks():
            if self._stop.is_set():
                return
            if self.config.speed > 0:
                delay = wall0 + (t - t_first) / self.config.speed - time.monotonic()
                if delay > 0:
                    try:
                        await asyncio.wait_for(self._stop.wait(), delay)
                        return
                    except TimeoutError:
                        pass
            self.ctx.emit(item)
            self._t = t
            await self.ctx.pace()
        self._t = self._span[1]

    async def stop(self) -> None:
        self._stop.set()

    def health(self) -> Health:
        a, b = self._span
        progress = (self._t - a) / (b - a) if b > a else 0.0
        return Health(counters={"progress": round(min(1.0, max(0.0, progress)), 4)})
