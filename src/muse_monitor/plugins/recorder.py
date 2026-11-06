"""Lossless Parquet recorder. Raw data is write-once; offline runs write only derived/ and analyses/."""

from __future__ import annotations

import json
import os
import shutil
import time
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from muse_monitor.sdk import Chunk, Event, Health, SessionInfo, Sink, StreamInfo, storage


class Recorder(Sink):
    name = "recorder"
    version = "1.0.0"
    summary = "Lossless Parquet recording + session metadata (crash-safe row groups)"
    critical = True
    requires = ["pyarrow"]

    class Config(BaseModel):
        flush_s: float = Field(5.0, description="Seconds of data buffered before writing; at most this much "
                                                "is lost if the app crashes.")
        derived: bool | None = Field(None, description="Also save analyzer results. Default: only for offline "
                                                       "analysis, since they can be regenerated.")
        info_every_s: float = Field(30.0, description="How often session metadata is saved (seconds).")

    def __init__(self, config: Config, ctx: Any) -> None:
        super().__init__(config, ctx)
        self.writers: dict[str, storage.StreamWriter] = {}
        self.session: SessionInfo | None = None
        self.dir: Path | None = None
        self._events: Any = None
        self._info_path: Path | None = None
        self._last_info = 0.0
        self._written = 0

    async def open(self, streams: dict[str, StreamInfo], session: SessionInfo) -> None:
        self.session = session
        offline = session.mode == "offline"
        derived = self.config.derived if self.config.derived is not None else offline
        src_id = session.pipeline["source"].get("id") or session.pipeline["source"]["plugin"]
        if offline:
            assert session.parent, "offline sessions need a parent recording"
            self.dir = self.ctx.recordings_dir / session.parent
            self._info_path = self.dir / "analyses" / f"{session.id}.json"
            for inst in {s.producer for s in streams.values() if s.producer != src_id}:
                shutil.rmtree(self.dir / "derived" / inst, ignore_errors=True)   # re-analysis replaces
            events_path = self.dir / "derived" / "events.jsonl"
        else:
            self.dir = self.ctx.recordings_dir / session.id
            if self.dir.exists():
                raise FileExistsError(f"{self.dir} already exists; raw data is write-once")
            self._info_path = self.dir / "session.json"
            events_path = self.dir / "events.jsonl"
        for sid, info in streams.items():
            is_raw = info.producer == src_id
            if (is_raw and not offline) or (not is_raw and derived):
                self.writers[sid] = storage.StreamWriter(storage.stream_base(self.dir, sid), info)
        events_path.parent.mkdir(parents=True, exist_ok=True)
        self._info_path.parent.mkdir(parents=True, exist_ok=True)
        self._events = open(events_path, "w", encoding="utf-8")
        self._write_info()

    async def write(self, item: Chunk | Event) -> None:
        if isinstance(item, Event):
            if item.data.get("_replayed"):
                return                       # replayed markers are already in the raw events.jsonl
            self._events.write(json.dumps(item.to_dict(), default=str) + "\n")
            self._events.flush()
        else:
            w = self.writers.get(item.stream)
            if w is None:
                return
            w.append(item)
            self._written += item.n
            if w.buffered_span() >= self.config.flush_s:
                await self.ctx.run_blocking(w.flush)
        if time.monotonic() - self._last_info >= self.config.info_every_s:
            self._write_info()

    def _write_info(self) -> None:
        if self.session is None or self._info_path is None:
            return
        tmp = self._info_path.with_suffix(".tmp")
        tmp.write_text(self.session.model_dump_json(indent=2), encoding="utf-8")
        os.replace(tmp, self._info_path)
        self._last_info = time.monotonic()

    async def close(self) -> None:
        for w in self.writers.values():
            await self.ctx.run_blocking(w.flush)
            await self.ctx.run_blocking(storage.compact, w.base)
        self.writers.clear()
        if self._events:
            self._events.close()
            self._events = None
        self._write_info()

    async def stop(self) -> None:
        if self._events:
            self._events.close()
            self._events = None

    def health(self) -> Health:
        return Health(counters={"samples_written": self._written,
                                "buffered": sum(w.buffered for w in self.writers.values())})

    # -- reading (used by the facade and tools) ---------------------------------------------------
    @staticmethod
    def list_streams(session_dir: Path) -> list[StreamInfo]:
        return storage.list_streams(session_dir)

    @staticmethod
    def read_stream(session_dir: Path, stream: str, t0: float | None = None, t1: float | None = None):
        return storage.read_stream(session_dir, stream, t0, t1)

    @staticmethod
    def read_events(session_dir: Path) -> list[dict[str, Any]]:
        return storage.read_events(session_dir)

    @staticmethod
    def compact(session_dir: Path) -> list[str]:
        return storage.compact_session(session_dir)
