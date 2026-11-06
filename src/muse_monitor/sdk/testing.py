"""Testing kit for plugin authors: run a plugin through the real kernel against a synthetic source."""

from __future__ import annotations

import asyncio
import logging
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import numpy as np

from .plugin import Context, Plugin, Sink
from .types import Chunk, Event, SessionInfo, StreamInfo

_COLLECTED: dict[str, dict[str, Any]] = {}


class _Collector(Sink):
    name = "_collector"
    version = "0"

    async def open(self, streams: dict[str, StreamInfo], session: SessionInfo) -> None:
        _COLLECTED[self.ctx.session_id] = {"streams": streams, "chunks": {}, "events": []}

    async def write(self, item: Chunk | Event) -> None:
        box = _COLLECTED[self.ctx.session_id]
        if isinstance(item, Event):
            box["events"].append(item)
        else:
            box["chunks"].setdefault(item.stream, []).append(item)


@dataclass
class Collected:
    info: StreamInfo
    t: np.ndarray
    data: np.ndarray


@dataclass
class RunResult:
    session: SessionInfo
    streams: dict[str, Collected] = field(default_factory=dict)   # by full id and by id relative to the plugin
    events: list[Event] = field(default_factory=list)

    def __getitem__(self, stream: str) -> Collected:
        return self.streams[stream]

    def events_of(self, prefix: str) -> list[Event]:
        return [e for e in self.events if e.type.startswith(prefix)]


def synthetic(**options: Any) -> dict[str, Any]:
    """Config for the built-in synthetic source, e.g. synthetic(sines={10: 20}, blink_every_s=3)."""
    return {"plugin": "synthetic", **options}


def run_pipeline(analyzers: list[tuple[type[Plugin], dict[str, Any]]], source: dict[str, Any] | None = None,
                 seconds: float = 10.0, inject: Callable[[Any], None] | None = None) -> RunResult:
    """Runs source -> analyzers -> collector in offline mode (deterministic, nothing dropped)."""
    from ..kernel.config import Pipeline, PluginEntry, SourceEntry
    from ..kernel.registry import PluginRegistry
    from ..kernel.session import Session

    registry = PluginRegistry(paths=[])
    for cls, _ in analyzers:
        registry.add(cls, origin="test")
    registry.add(_Collector, origin="test")
    src = {"duration_s": seconds, "start_time": 1_700_000_000.0, **(source or synthetic()), "realtime": False}
    pipeline = Pipeline(source=SourceEntry(**src),
                        analyzer=[PluginEntry(plugin=cls.name, **cfg) for cls, cfg in analyzers],
                        sink=[PluginEntry(plugin="_collector")])

    async def go() -> RunResult:
        with tempfile.TemporaryDirectory() as tmp:
            session = Session(registry, pipeline, data_dir=Path(tmp), mode="offline")
            await session.start()
            if inject:
                inject(session)
            await session.done.wait()
        box = _COLLECTED.pop(session.info.id, {"streams": {}, "chunks": {}, "events": []})
        result = RunResult(session.info, events=box["events"])
        for sid, chunks in box["chunks"].items():
            c = Collected(box["streams"][sid], np.concatenate([x.t for x in chunks]),
                          np.concatenate([x.data for x in chunks]))
            result.streams[sid] = c
            if "/" in sid:
                result.streams.setdefault(sid.split("/", 1)[1], c)
        return result

    return asyncio.run(go())


def run_analyzer(cls: type[Plugin], config: dict[str, Any] | None = None, source: dict[str, Any] | None = None,
                 seconds: float = 10.0) -> RunResult:
    return run_pipeline([(cls, config or {})], source, seconds)


class _DummyContext(Context):
    session_id = "test"
    instance_id = "test"
    mode = "offline"
    recordings_dir = Path(tempfile.gettempdir())
    log = logging.getLogger("muse.test")

    def emit(self, item: Chunk | Event) -> None:
        pass

    def data_dir(self) -> Path:
        return Path(tempfile.mkdtemp())

    async def run_blocking(self, fn: Callable[..., Any], *args: Any) -> Any:
        return fn(*args)

    async def pace(self) -> None:
        pass


def check_plugin(cls: type[Plugin]) -> None:
    """Manifest, config schema, and stop() idempotence (also without start())."""
    for attr in ("name", "version", "kind"):
        assert getattr(cls, attr, None), f"{cls.__name__}.{attr} is required"
    assert cls.kind in ("source", "analyzer", "sink"), f"bad kind {cls.kind!r}"
    assert isinstance(cls.Config.model_json_schema(), dict)
    try:
        config = cls.Config()
    except Exception:            # required options: schema check is enough
        return
    plugin = cls(config, _DummyContext())

    async def go() -> None:
        await plugin.stop()
        await plugin.stop()
    asyncio.run(go())
    plugin.health()
