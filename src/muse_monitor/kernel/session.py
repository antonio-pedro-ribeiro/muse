"""SessionManager: builds a pipeline, runs it, isolates failures, shuts it down without losing data."""

from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import os
import threading
import time
import traceback
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, datetime
from functools import partial
from pathlib import Path
from typing import Any, Callable, Literal

from ..sdk.plugin import Analyzer, Context, IncompatibleInput, Plugin, Sink, Source, SourceDisconnected
from ..sdk.types import Chunk, Event, SessionInfo, StreamInfo, StreamSelector
from .bus import Overflow, Policy, StreamBus, Subscriber
from .config import Pipeline, PluginEntry, build_config
from .errors import ConfigError
from .ids import ulid
from .registry import PluginRegistry

log = logging.getLogger("muse.kernel")

LIVE_QUEUE = 256           # chunks per analyzer before dropping (live)
LIVE_CRITICAL_QUEUE = 20_000
OFFLINE_QUEUE = 32         # source waits beyond this (offline)
SNAPSHOT_MAX_RATE = 10.0   # derived streams up to this rate are kept for reports
SNAPSHOT_MAX_ROWS = 50_000


class _Ctx(Context):
    def __init__(self, session: Session, instance_id: str, cls: type[Plugin]) -> None:
        self.session_id = session.info.id
        self.instance_id = instance_id
        self.mode = session.mode
        self.recordings_dir = session.data_dir
        self.log = logging.getLogger(f"muse.plugin.{instance_id}")
        self._session = session
        self._cls = cls
        self._prefix = f"{instance_id}/" if cls.kind == "analyzer" else ""
        self._executor = ThreadPoolExecutor(1, thread_name_prefix=instance_id) if cls.cpu_bound else None

    def emit(self, item: Chunk | Event) -> None:
        s = self._session
        if s.loop is not None and threading.get_ident() != s.loop_thread:
            s.loop.call_soon_threadsafe(self.emit, item)
            return
        if isinstance(item, Chunk):
            if self._prefix and not item.stream.startswith(self._prefix):
                item = item.with_stream(self._prefix + item.stream)
            if item.stream not in s.streams:
                raise ValueError(f"{self.instance_id} emitted undeclared stream {item.stream!r}")
        elif not item.source:
            item = dataclasses.replace(item, source=self.instance_id)
        s.publish(item)

    def data_dir(self) -> Path:
        p = self._session.data_dir / ".muse" / "plugins" / self._cls.name
        p.mkdir(parents=True, exist_ok=True)
        return p

    async def run_blocking(self, fn: Callable[..., Any], *args: Any) -> Any:
        return await asyncio.get_running_loop().run_in_executor(self._executor, partial(fn, *args))

    async def pace(self) -> None:
        if self.mode == "offline":
            await self._session.bus.pace()
        else:
            await asyncio.sleep(0)

    def shutdown(self) -> None:
        if self._executor:
            self._executor.shutdown(wait=False, cancel_futures=True)


@dataclass
class Runtime:
    instance_id: str
    cls: type[Plugin]
    plugin: Plugin
    ctx: _Ctx
    entry: PluginEntry
    state: Literal["created", "started", "running", "failed", "stopped"] = "created"
    error: str | None = None
    inputs: set[str] = field(default_factory=set)
    outputs: list[str] = field(default_factory=list)
    sub: Subscriber | None = None
    task: asyncio.Task[None] | None = None

    @property
    def active(self) -> bool:
        return self.state in ("started", "running")


class Snapshot:
    """Keeps low-rate derived streams and events in memory for reports and late joiners."""

    def __init__(self) -> None:
        self.rows: dict[str, list[tuple[float, list[float]]]] = {}
        self.events: list[Event] = []

    def add(self, item: Chunk | Event) -> None:
        if isinstance(item, Event):
            if len(self.events) < SNAPSHOT_MAX_ROWS:
                self.events.append(item)
            return
        rows = self.rows.setdefault(item.stream, [])
        for t, row in zip(item.t, item.data):
            if len(rows) >= SNAPSHOT_MAX_ROWS:
                break
            rows.append((float(t), [float(v) for v in row]))


class Session:
    def __init__(self, registry: PluginRegistry, pipeline: Pipeline, *, data_dir: Path,
                 mode: Literal["live", "offline"] = "live", parent: str | None = None,
                 app_version: str = "") -> None:
        self.registry = registry
        self.pipeline = pipeline
        self.data_dir = data_dir
        self.mode = mode
        self.info = SessionInfo(id=ulid(), label=pipeline.session.label, mode=mode, parent=parent,
                                started_at=datetime.now(UTC), pipeline=pipeline.model_dump(mode="json"),
                                app_version=app_version)
        self.bus = StreamBus()
        self.bus.listeners.append(self._on_event)
        self.streams: dict[str, StreamInfo] = {}
        self.snapshot = Snapshot()
        self.recent_events: deque[Event] = deque(maxlen=2000)
        self.loop: asyncio.AbstractEventLoop | None = None
        self.loop_thread: int | None = None
        self.done = asyncio.Event()
        self._stopping = False
        self._stop_task: asyncio.Task[None] | None = None
        self._source_task: asyncio.Task[None] | None = None
        self._snapshot_task: asyncio.Task[None] | None = None
        self._snap_sub: Subscriber | None = None
        self._final_status: Literal["stopped", "failed"] = "stopped"
        self._build()

    # -- construction -----------------------------------------------------------------------------
    def _make(self, entry: PluginEntry, kind: str) -> Runtime:
        cls = self.registry.get(entry.plugin, kind)
        ctx = _Ctx(self, entry.instance_id, cls)
        plugin = cls(build_config(cls, entry), ctx)
        return Runtime(entry.instance_id, cls, plugin, ctx, entry)

    def _build(self) -> None:
        src, analyzers, sinks = self.pipeline.active()
        self.source = self._make(src, "source")
        self.analyzers = [self._make(a, "analyzer") for a in analyzers]
        self.sinks = [self._make(s, "sink") for s in sinks]
        ids = [rt.instance_id for rt in self.all]
        dupes = sorted({i for i in ids if ids.count(i) > 1})
        if dupes:
            raise ConfigError(f"duplicate plugin instance ids {dupes}; give each a distinct id = \"...\"")
        self.info.plugins = {rt.instance_id: f"{rt.cls.name}=={rt.cls.version}" for rt in self.all}

    @property
    def all(self) -> list[Runtime]:
        return [self.source, *self.analyzers, *self.sinks]

    def runtime(self, instance_id: str) -> Runtime | None:
        return next((rt for rt in self.all if rt.instance_id == instance_id), None)

    # -- publishing ---------------------------------------------------------------------------------
    def _on_event(self, event: Event) -> None:
        self.recent_events.append(event)
        if event.type == "baseline.ready":
            self.info.baseline = event.data

    def publish(self, item: Chunk | Event) -> None:
        try:
            self.bus.publish(item)
        except Overflow as exc:
            self._fail(f"lossless sink overflow: {exc}")

    def inject(self, event: Event) -> Event:
        """Events from the user/interfaces (markers, calibration)."""
        if not event.source:
            event = dataclasses.replace(event, source="user")
        self.publish(event)
        return event

    def _register(self, info: StreamInfo) -> None:
        if info.id in self.streams:
            raise ConfigError(f"stream {info.id!r} declared twice")
        self.streams[info.id] = info

    def _resolve(self, selectors: list[StreamSelector], owner: str) -> dict[str, StreamInfo]:
        found: dict[str, StreamInfo] = {}
        for sel in selectors:
            matches = {sid: s for sid, s in self.streams.items() if sel.matches(s) and s.producer != owner}
            if not matches and not sel.optional:
                raise IncompatibleInput(f"no stream matches {sel}")
            found.update(matches)
        return found

    def _policy(self, critical: bool) -> tuple[Policy, int]:
        if self.mode == "offline":
            return "block", OFFLINE_QUEUE
        return ("fail", LIVE_CRITICAL_QUEUE) if critical else ("drop", LIVE_QUEUE)

    # -- lifecycle ----------------------------------------------------------------------------------
    async def start(self) -> SessionInfo:
        self.loop = asyncio.get_running_loop()
        self.loop_thread = threading.get_ident()
        try:
            for rt in [*self.sinks, *self.analyzers, self.source]:
                await rt.plugin.start()
                rt.state = "started"
            src = self.source
            assert isinstance(src.plugin, Source)
            for s in await src.plugin.open():
                self._register(s.replace(producer=src.instance_id))
                src.outputs.append(s.id)
            self.info.device = self._private_device(src.plugin)

            for rt in self.analyzers:
                self._setup_analyzer(rt)
            for rt in self.sinks:
                assert isinstance(rt.plugin, Sink)
                inputs = self._resolve(list(rt.cls.inputs), rt.instance_id)  # type: ignore[attr-defined]
                rt.inputs = set(inputs)
                self.info.streams = [s.to_dict() for s in self.streams.values()]
                await rt.plugin.open(inputs, self.info)
                policy, size = self._policy(bool(getattr(rt.cls, "critical", False)))
                rt.sub = self.bus.subscribe(Subscriber(rt.instance_id, rt.inputs.__contains__, events=True,
                                                       policy=policy, maxsize=size))
                rt.task = asyncio.create_task(self._sink_worker(rt))
                rt.state = "running"

            self.info.streams = [s.to_dict() for s in self.streams.values()]
            self.info.views = [v.model_dump() for v in self.views()]
            snap_rate = lambda sid: self.streams[sid].producer != src.instance_id and \
                self.streams[sid].rate <= SNAPSHOT_MAX_RATE
            policy, size = ("block", OFFLINE_QUEUE) if self.mode == "offline" else ("latest", LIVE_CRITICAL_QUEUE)
            self._snap_sub = self.bus.subscribe(Subscriber("snapshot", snap_rate, events=True, policy=policy,
                                                           maxsize=size))
            self._snapshot_task = asyncio.create_task(self._snapshot_worker(self._snap_sub))
            self.info.status = "running"
            src.state = "running"
            self._source_task = asyncio.create_task(self._run_source())
        except BaseException as exc:
            self._final_status = "failed"
            self.info.error = f"{type(exc).__name__}: {exc}"
            await self._teardown()
            raise
        return self.info

    def _setup_analyzer(self, rt: Runtime) -> None:
        assert isinstance(rt.plugin, Analyzer)
        try:
            inputs = self._resolve(list(rt.cls.inputs), rt.instance_id)  # type: ignore[attr-defined]
            outputs = rt.plugin.setup(inputs)
        except Exception as exc:
            self._mark_failed(rt, exc)
            return
        rt.inputs = set(inputs)
        for o in outputs:
            info = o.replace(id=f"{rt.instance_id}/{o.id}", producer=rt.instance_id)
            self._register(info)
            rt.outputs.append(info.id)
        policy, size = self._policy(False)
        rt.sub = self.bus.subscribe(Subscriber(rt.instance_id, rt.inputs.__contains__, events=True,
                                               policy=policy, maxsize=size))
        rt.task = asyncio.create_task(self._analyzer_worker(rt))
        rt.state = "running"

    def _private_device(self, src: Source) -> Any:
        dev = src.device
        if dev is None or not dev.serial or os.environ.get("MUSE_KEEP_SERIALS"):
            return dev
        salt_file = self.data_dir / ".muse" / "salt"
        salt_file.parent.mkdir(parents=True, exist_ok=True)
        if not salt_file.exists():
            salt_file.write_bytes(os.urandom(16))
        digest = hashlib.sha256(salt_file.read_bytes() + dev.serial.encode()).hexdigest()[:16]
        return dev.model_copy(update={"serial": f"sha256:{digest}"})

    def views(self) -> list[Any]:
        out = []
        for rt in self.analyzers:
            if not rt.active:
                continue
            assert isinstance(rt.plugin, Analyzer)
            try:
                specs = rt.plugin.views()
            except Exception as exc:
                rt.ctx.log.warning("views() failed: %s", exc)
                continue
            for v in specs:
                streams = [s if s in self.streams else f"{rt.instance_id}/{s}" for s in v.streams]
                out.append(v.model_copy(update={"id": f"{rt.instance_id}/{v.id}", "streams": streams,
                                                "plugin": rt.instance_id}))
        return out

    async def _run_source(self) -> None:
        src = self.source
        assert isinstance(src.plugin, Source)
        reconnect = self.pipeline.source.reconnect
        try:
            while not self._stopping:
                try:
                    await src.plugin.run()
                    break
                except SourceDisconnected as exc:
                    if self._stopping:
                        break
                    self.publish(Event("source.disconnected", time.time(), source=src.instance_id,
                                       data={"error": str(exc)}))
                    await self._reconnect(reconnect.max_attempts, reconnect.backoff_s)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            self._mark_failed(src, exc)
            self._fail(f"source failed: {type(exc).__name__}: {exc}")
        finally:
            if not self._stopping:
                self._schedule_stop()

    async def _reconnect(self, attempts: int, backoff: float) -> None:
        src = self.source
        assert isinstance(src.plugin, Source)
        last: Exception | None = None
        for attempt in range(1, attempts + 1):
            await asyncio.sleep(backoff)
            if self._stopping:
                return
            try:
                await src.plugin.open()
                self.publish(Event("source.reconnected", time.time(), source=src.instance_id,
                                   data={"attempt": attempt}))
                return
            except Exception as exc:
                last = exc
                src.ctx.log.warning("reconnect attempt %d/%d failed: %s", attempt, attempts, exc)
        raise SourceDisconnected(f"gave up after {attempts} reconnect attempts: {last}")

    async def _analyzer_worker(self, rt: Runtime) -> None:
        assert rt.sub is not None and isinstance(rt.plugin, Analyzer)
        while True:
            item, gap = await rt.sub.queue.get()
            try:
                if isinstance(item, Chunk):
                    await rt.plugin.process(item, gap)
                elif isinstance(item, Event):
                    await rt.plugin.on_event(item)
            except Exception as exc:
                self._mark_failed(rt, exc)
            finally:
                rt.sub.done()
            if rt.state == "failed":
                return

    async def _sink_worker(self, rt: Runtime) -> None:
        assert rt.sub is not None and isinstance(rt.plugin, Sink)
        while True:
            item, _ = await rt.sub.queue.get()
            try:
                if item is not None:
                    await rt.plugin.write(item)
            except Exception as exc:
                self._mark_failed(rt, exc)
                if getattr(rt.cls, "critical", False):
                    self._fail(f"critical sink {rt.instance_id} failed: {exc}")
            finally:
                rt.sub.done()
            if rt.state == "failed":
                return

    async def _snapshot_worker(self, sub: Subscriber) -> None:
        while True:
            item, _ = await sub.queue.get()
            try:
                if item is not None:
                    self.snapshot.add(item)
            finally:
                sub.done()

    def _mark_failed(self, rt: Runtime, exc: BaseException) -> None:
        rt.state = "failed"
        rt.error = f"{type(exc).__name__}: {exc}"
        rt.ctx.log.error("plugin failed: %s", rt.error)
        if rt.sub is not None:
            self.bus.unsubscribe(rt.sub)
        tb = traceback.format_exception(type(exc), exc, exc.__traceback__)[-3:]
        self.publish(Event("plugin.failed", time.time(), source="kernel",
                           data={"plugin": rt.instance_id, "error": rt.error, "traceback": "".join(tb)}))

    def _fail(self, message: str) -> None:
        if self.info.error is None:
            self.info.error = message
        self._final_status = "failed"
        log.error("session %s failing: %s", self.info.id, message)
        self._schedule_stop()

    def _schedule_stop(self) -> None:
        if self._stop_task is None:
            self._stop_task = asyncio.get_running_loop().create_task(self.stop())

    async def stop(self) -> SessionInfo:
        if self._stopping:
            if self._stop_task is not None and asyncio.current_task() is not self._stop_task:
                await self.done.wait()
            return self.info
        self._stopping = True
        if self._stop_task is None:
            self._stop_task = asyncio.current_task()  # type: ignore[assignment]
        await self._teardown()
        return self.info

    async def _teardown(self) -> None:
        self._stopping = True
        src = self.source
        try:
            await src.plugin.stop()
        except Exception as exc:
            src.ctx.log.warning("stop() failed: %s", exc)
        if self._source_task is not None and self._source_task is not asyncio.current_task():
            try:
                await asyncio.wait_for(asyncio.shield(self._source_task), timeout=5.0)
            except (TimeoutError, asyncio.CancelledError):
                self._source_task.cancel()
        if src.state != "failed":
            src.state = "stopped"

        timeout = None if self.mode == "offline" else 10.0
        await self._drain(self.analyzers, timeout)
        for rt in self.analyzers:
            if rt.active and rt.sub is not None:     # set up (the source opened); else there is nothing to sum up
                assert isinstance(rt.plugin, Analyzer)
                try:
                    report = await rt.plugin.summarize()
                    if report is not None:
                        report = report.model_copy(update={"title": f"{rt.instance_id}: {report.title}"})
                        self.info.reports.append(report)
                except Exception as exc:
                    rt.ctx.log.warning("summarize() failed: %s", exc)
        await self._drain(self.analyzers, timeout)
        await self._drain(self.sinks, timeout)
        if self._snap_sub is not None:
            try:
                await asyncio.wait_for(self._snap_sub.queue.join(), timeout)
            except TimeoutError:
                pass

        self.info.ended_at = datetime.now(UTC)
        self.info.status = self._final_status
        self.refresh_stats()
        for rt in self.analyzers:
            await self._stop_plugin(rt)
        for rt in self.sinks:   # in config order, so later sinks (export) see finished files
            if rt.task:
                rt.task.cancel()
            if rt.state in ("running", "failed"):
                try:
                    await rt.plugin.close()  # type: ignore[attr-defined]
                except Exception as exc:
                    self._mark_failed(rt, exc)
            await self._stop_plugin(rt)
        if self._snapshot_task:
            self._snapshot_task.cancel()
        for rt in self.all:
            rt.ctx.shutdown()
        self.done.set()

    async def _drain(self, runtimes: list[Runtime], timeout: float | None) -> None:
        for rt in runtimes:
            if rt.sub is not None and not rt.sub.closed:
                try:
                    await asyncio.wait_for(rt.sub.queue.join(), timeout)
                except TimeoutError:
                    rt.ctx.log.warning("did not drain within %ss; dropping %d items", timeout,
                                       rt.sub.queue.qsize())

    async def _stop_plugin(self, rt: Runtime) -> None:
        if rt.task and not rt.task.done():
            rt.task.cancel()
        try:
            await rt.plugin.stop()
        except Exception as exc:
            rt.ctx.log.warning("stop() failed: %s", exc)
        if rt.state != "failed":
            rt.state = "stopped"

    # -- status ---------------------------------------------------------------------------------------
    def refresh_stats(self) -> None:
        self.info.stats = {sid: st.to_dict(self.streams[sid].rate) for sid, st in self.bus.stats.items()
                           if sid in self.streams}

    def health(self) -> dict[str, dict[str, Any]]:
        out = {}
        for rt in self.all:
            try:
                h = rt.plugin.health().to_dict()
            except Exception as exc:
                h = {"status": "degraded", "message": f"health() failed: {exc}", "counters": {}}
            if rt.state == "failed":
                h["status"], h["message"] = "failed", rt.error or ""
            h["state"] = rt.state
            h["dropped"] = rt.sub.dropped if rt.sub else 0
            out[rt.instance_id] = h
        return out
