"""KernelFacade: the single async API used by the CLI, HTTP API and web UI.

Methods return plain JSON-able dicts so a remote client can offer the same interface.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import re
import shutil
import tempfile
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path
from typing import Any

import numpy as np

from ..sdk.types import Chunk, Event, SessionInfo
from .bus import Subscriber
from .config import DEFAULT_ANALYZERS, Pipeline, PluginEntry, SessionSection, SourceEntry, data_root, parse_pipeline
from .errors import Busy, ConfigError, KernelError, NotFound
from .ids import ulid
from .registry import PluginRegistry
from .session import Session

log = logging.getLogger("muse.facade")

try:
    APP_VERSION = version("muse-monitor")
except PackageNotFoundError:  # pragma: no cover
    APP_VERSION = "0.0.0"


def envelope(t: np.ndarray, data: np.ndarray, max_points: int) -> tuple[np.ndarray, np.ndarray]:
    """Min/max decimation: keeps peaks visible while bounding the number of points."""
    n = len(t)
    if max_points <= 0 or n <= max_points:
        return t, data
    bins = max(1, max_points // 2)
    edges = np.linspace(0, n, bins + 1).astype(int)
    ts, ds = [], []
    for a, b in zip(edges[:-1], edges[1:]):
        if b <= a:
            continue
        seg = data[a:b]
        ts += [t[a], t[b - 1]]
        ds += [np.nanmin(seg, axis=0), np.nanmax(seg, axis=0)]
    return np.array(ts), np.array(ds)


def _num(x: float) -> float | None:
    return None if x is None or not math.isfinite(x) else float(x)


def chunk_message(stream: str, t: np.ndarray, data: np.ndarray, first_index: int = -1) -> dict[str, Any]:
    return {"type": "chunk", "stream": stream, "first_index": first_index,
            "t": [round(float(x), 4) for x in t],
            "data": [[_num(float(v)) for v in row] for row in data]}


def event_message(e: Event) -> dict[str, Any]:
    return {"type": "event", "event": e.to_dict()}


class _Decimator:
    """Batches chunks per stream; flushes at most max_hz times per second with <= 200 points/s/channel."""

    def __init__(self, max_hz: float) -> None:
        self.max_hz = max_hz
        self.buf: dict[str, list[Chunk]] = {}
        self.last: dict[str, float] = {}

    def add(self, c: Chunk) -> None:
        self.buf.setdefault(c.stream, []).append(c)

    def flush(self, now: float, force: bool = False) -> list[dict[str, Any]]:
        out = []
        for stream, chunks in self.buf.items():
            if not chunks:
                continue
            if not force and self.max_hz > 0 and now - self.last.get(stream, 0.0) < 1.0 / self.max_hz:
                continue
            t = np.concatenate([c.t for c in chunks])
            d = np.concatenate([c.data for c in chunks])
            if self.max_hz > 0:
                t, d = envelope(t, d, max(2, int(400 / self.max_hz)))
            out.append(chunk_message(stream, t, d, chunks[0].first_index))
            self.buf[stream] = []
            self.last[stream] = now
        return out


@dataclass
class Job:
    id: str
    kind: str
    parent: str
    session: Session | None = None
    status: str = "running"
    error: str | None = None
    result: dict[str, Any] = field(default_factory=dict)
    task: asyncio.Task[None] | None = None

    def to_dict(self) -> dict[str, Any]:
        progress = 1.0 if self.status == "done" else 0.0
        if self.session is not None and self.status == "running":
            try:
                progress = float(self.session.source.plugin.health().counters.get("progress", 0.0))
            except Exception:
                pass
        return {"id": self.id, "kind": self.kind, "parent": self.parent, "status": self.status,
                "progress": round(progress, 3), "error": self.error,
                "session_id": self.session.info.id if self.session else None, "result": self.result}


class KernelFacade:
    def __init__(self, data_dir: Path | None = None, registry: PluginRegistry | None = None) -> None:
        self.data_dir = (data_dir or data_root()).resolve()
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.registry = registry or PluginRegistry()
        self.live: Session | None = None
        self.active: dict[str, Session] = {}
        self.jobs: dict[str, Job] = {}

    # -- plugins & devices ------------------------------------------------------------------------
    def list_plugins(self, kind: str | None = None) -> list[dict[str, Any]]:
        return [m.to_dict() for m in self.registry.manifests(kind)]

    def plugin_info(self, name: str) -> dict[str, Any]:
        return self.registry.manifest(name).to_dict()

    async def discover_devices(self, source: str = "openmuse", config: dict[str, Any] | None = None
                               ) -> list[dict[str, Any]]:
        from .config import build_config
        cls = self.registry.get(source, "source")
        cfg = build_config(cls, SourceEntry(plugin=source, **(config or {})))
        return [d.model_dump() for d in await cls.discover(cfg)]  # type: ignore[attr-defined]

    # -- sessions ---------------------------------------------------------------------------------
    async def start_session(self, pipeline: dict[str, Any] | Pipeline) -> dict[str, Any]:
        if self.live is not None and not self.live.done.is_set():
            raise Busy(f"session {self.live.info.id} is already running; stop it first")
        session = Session(self.registry, parse_pipeline(pipeline), data_dir=self.data_dir,
                          app_version=APP_VERSION)
        await session.start()
        self.live = session
        self.active[session.info.id] = session
        return self._info(session)

    async def replay_session(self, session_id: str, speed: float = 1.0,
                             analyzers: list[str] | None = None) -> dict[str, Any]:
        """Plays a recording back as a live session, nothing recorded: watch the analyzers (and `describe`) work in
        real time on old data. Default analyzers: the ones it was recorded with, plus `describe` if missing."""
        session_id = self._check_id(session_id)
        d = self.session_dir(session_id)
        parent = SessionInfo.model_validate_json((d / "session.json").read_text(encoding="utf-8"))
        if parent.mode != "live":
            raise ConfigError("only recorded sessions can be replayed")
        if session_id in self.active and not self.active[session_id].done.is_set():
            raise Busy(f"session {session_id} is still recording")
        if not 0 < speed <= 100:
            raise ConfigError("speed must be above 0 and at most 100 (1 = real time)")
        if analyzers is not None:
            entries = [PluginEntry(plugin=a) for a in analyzers]
        else:
            recorded = parse_pipeline(parent.pipeline).analyzer if parent.pipeline else []
            entries = list(recorded) or [PluginEntry(plugin=a) for a in DEFAULT_ANALYZERS]
            describe_ok = any(m.name == "describe" and m.status == "ok" for m in self.registry.manifests("analyzer"))
            if describe_ok and not any(e.plugin == "describe" for e in entries):
                entries.append(PluginEntry(plugin="describe"))
        pipe = Pipeline(session=SessionSection(label=f"Replay of {parent.label or parent.id} ({speed:g}×)"),
                        source=SourceEntry(plugin="replay", path=str(d), speed=speed), analyzer=entries, sink=[])
        return await self.start_session(pipe)

    async def stop_session(self, session_id: str) -> dict[str, Any]:
        s = self._session(session_id)
        await s.stop()
        return self._info(s)

    async def wait_session(self, session_id: str, timeout: float | None = None) -> dict[str, Any]:
        s = self._session(session_id)
        try:
            await asyncio.wait_for(s.done.wait(), timeout)
        except TimeoutError:
            pass
        return self._info(s)

    def _session(self, session_id: str) -> Session:
        if session_id in self.active:
            return self.active[session_id]
        if session_id == "current" and self.live is not None:
            return self.live
        raise NotFound(f"no active session {session_id!r}")

    def _info(self, s: Session) -> dict[str, Any]:
        if not s.done.is_set():
            s.refresh_stats()
        d = s.info.model_dump(mode="json")
        d["active"] = not s.done.is_set()
        d["health"] = s.health()
        return d

    @staticmethod
    def _check_id(session_id: str) -> str:
        """Session ids are single path components; this also blocks path traversal from the API."""
        sid = session_id.strip().rstrip("/\\")
        if not re.fullmatch(r"[0-9A-Za-z_-]{1,64}", sid):
            raise NotFound(f"invalid session id {session_id!r}")
        return sid

    def session_dir(self, session_id: str) -> Path:
        d = self.data_dir / self._check_id(session_id)
        if not (d / "session.json").is_file():
            raise NotFound(f"no recorded session {session_id!r}")
        return d

    def get_session(self, session_id: str) -> dict[str, Any]:
        if session_id in self.active or session_id == "current":
            return self._info(self._session(session_id))
        session_id = self._check_id(session_id)
        path = self.data_dir / session_id / "session.json"
        if not path.is_file():
            hits = list(self.data_dir.glob(f"*/analyses/{session_id}.json"))
            if not hits:
                raise NotFound(f"unknown session {session_id!r}")
            path = hits[0]
        d = self._from_disk(path)
        if d["status"] == "incomplete":            # killed: its saved stats may be stale or missing
            from ..sdk.storage import stats_from_files
            try:
                d["stats"] = {**d.get("stats", {}), **stats_from_files(path.parent)}
            except Exception as exc:
                log.warning("could not rebuild stats for %s: %s", d["id"], exc)
        return d

    def _from_disk(self, path: Path) -> dict[str, Any]:
        d = SessionInfo.model_validate_json(path.read_text(encoding="utf-8")).model_dump(mode="json")
        d["active"] = d["id"] in self.active and not self.active[d["id"]].done.is_set()
        if d["status"] in ("starting", "running") and not d["active"]:
            d["status"] = "incomplete"
        return d

    def list_sessions(self, since: datetime | None = None, mode: str | None = "live",
                      label: str | None = None) -> list[dict[str, Any]]:
        files = list(self.data_dir.glob("*/session.json")) if mode in (None, "live") else []
        if mode in (None, "offline"):
            files += list(self.data_dir.glob("*/analyses/*.json"))
        out = []
        for f in files:
            try:
                d = self._from_disk(f)
            except Exception:
                continue
            if since and datetime.fromisoformat(d["started_at"]) < since:
                continue
            if label and label.lower() not in (d.get("label") or "").lower():
                continue
            out.append(d)
        return sorted(out, key=lambda d: d["started_at"], reverse=True)

    def views(self, session_id: str) -> list[dict[str, Any]]:
        if session_id in self.active or session_id == "current":
            return [v.model_dump() for v in self._session(session_id).views()]
        return self.get_session(session_id)["views"]

    def health(self, session_id: str) -> dict[str, Any]:
        return self._session(session_id).health()

    async def add_marker(self, session_id: str, label: str) -> dict[str, Any]:
        s = self._session(session_id)
        return s.inject(Event("marker", time.time(), data={"label": label})).to_dict()

    async def calibrate(self, session_id: str, eyes_open_s: float = 60.0, eyes_closed_s: float = 60.0
                        ) -> dict[str, Any]:
        """Runs the eyes-open / eyes-closed baseline protocol by emitting calibration events."""
        s = self._session(session_id)

        async def protocol() -> None:
            s.inject(Event("calibration.eyes_open", time.time(), duration=eyes_open_s))
            await asyncio.sleep(eyes_open_s)
            s.inject(Event("calibration.eyes_closed", time.time(), duration=eyes_closed_s))
            await asyncio.sleep(eyes_closed_s)
            s.inject(Event("calibration.end", time.time()))

        asyncio.create_task(protocol())
        return {"eyes_open_s": eyes_open_s, "eyes_closed_s": eyes_closed_s}

    async def subscribe(self, session_id: str, streams: list[str] | None = None, max_hz: float = 20.0
                        ) -> AsyncIterator[dict[str, Any]]:
        s = self._session(session_id)
        unknown = [x for x in streams or [] if x not in s.streams]
        if unknown:
            raise NotFound(f"unknown stream(s) {unknown}")
        wanted = set(streams or s.streams)
        sub = s.bus.subscribe(Subscriber(f"viewer-{ulid()}", wanted.__contains__, events=True,
                                         policy="latest", maxsize=5000))
        decim = _Decimator(max_hz)
        try:
            yield {"type": "hello", "session": self._info(s), "views": self.views(session_id),
                   "streams": [s.streams[x].to_dict() for x in wanted]}
            last_health = 0.0
            while not s.done.is_set():
                try:
                    item, _ = await asyncio.wait_for(sub.queue.get(), 0.1)
                    sub.done()
                except TimeoutError:
                    item = None
                if isinstance(item, Event):
                    yield event_message(item)
                elif isinstance(item, Chunk):
                    decim.add(item)
                now = time.monotonic()
                for msg in decim.flush(now):
                    yield msg
                if now - last_health >= 1.0:
                    last_health = now
                    yield {"type": "health", "plugins": s.health()}
            for msg in decim.flush(time.monotonic(), force=True):
                yield msg
            yield {"type": "end", "session": self._info(s)}
        finally:
            s.bus.unsubscribe(sub)

    # -- offline analysis -------------------------------------------------------------------------
    async def run_offline(self, session_id: str, pipeline: dict[str, Any] | Pipeline | None = None,
                          report: bool = True, speed: float = 0.0) -> dict[str, Any]:
        session_id = self._check_id(session_id)
        parent = SessionInfo.model_validate_json((self.session_dir(session_id) / "session.json")
                                                 .read_text(encoding="utf-8"))
        if parent.mode != "live":
            raise ConfigError("offline analysis needs a recorded (live) session")
        if session_id in self.active and not self.active[session_id].done.is_set():
            raise Busy(f"session {session_id} is still recording")
        if pipeline is None:
            base = parse_pipeline(parent.pipeline)
            pipe = Pipeline(session=base.session, source=base.source, analyzer=base.analyzer, sink=[])
        else:
            pipe = parse_pipeline(pipeline) if isinstance(pipeline, (dict, Pipeline)) else pipeline
        pipe = pipe.model_copy(update={
            "source": SourceEntry(plugin="replay", path=str(self.session_dir(session_id)), speed=speed),
            "sink": [x for x in pipe.sink if x.plugin != "recorder"] + [PluginEntry(plugin="recorder")],
        })
        session = Session(self.registry, pipe, data_dir=self.data_dir, mode="offline", parent=session_id,
                          app_version=APP_VERSION)
        job = Job(id=ulid(), kind="analysis", parent=session_id, session=session)
        self.jobs[job.id] = job
        job.task = asyncio.create_task(self._run_job(job, session, report))
        await asyncio.sleep(0)
        return job.to_dict()

    async def _run_job(self, job: Job, session: Session, report: bool) -> None:
        try:
            self.active[session.info.id] = session
            await session.start()
            await session.done.wait()
            if session.info.status == "failed":
                raise KernelError(session.info.error or "analysis failed")
            if report:
                from ..report import write_report
                path = write_report(self.data_dir / job.parent, session)
                job.result["report"] = path.name
                job.result["report_path"] = str(path)
            job.result["reports"] = [r.model_dump() for r in session.info.reports]
            job.status = "done"
        except Exception as exc:
            job.status, job.error = "failed", f"{type(exc).__name__}: {exc}"
        finally:
            self.active.pop(session.info.id, None)

    def get_job(self, job_id: str) -> dict[str, Any]:
        if job_id not in self.jobs:
            raise NotFound(f"unknown job {job_id!r}")
        return self.jobs[job_id].to_dict()

    async def wait_job(self, job_id: str) -> dict[str, Any]:
        job = self.jobs[job_id]
        if job.task:
            await job.task
        return job.to_dict()

    # -- models -----------------------------------------------------------------------------------
    async def pull_model(self, plugin: str, revision: str | None = None) -> dict[str, Any]:
        """Download a model plugin's pinned weights (the only place weights are downloaded)."""
        cls = self.registry.get(plugin, "analyzer")
        if not hasattr(cls, "pull"):
            raise ConfigError(f"plugin {plugin!r} has no model weights")
        cfg = cls.Config(**({"revision": revision} if revision else {}))
        path = await asyncio.to_thread(cls.pull, cfg)
        return {"plugin": plugin, "weights": cfg.weights, "revision": cfg.revision, "path": str(path)}

    async def train_probe(self, sessions: list[str], plugin: str = "cbramod", label_from: str = "markers",
                          out: str = "head.npz", options: dict[str, Any] | None = None) -> dict[str, Any]:
        from ..probe import train_probe
        return await train_probe(self, [self._check_id(s) for s in sessions], plugin, label_from, out, options)

    # -- stored data ------------------------------------------------------------------------------
    def _recorder(self) -> Any:
        return self.registry.get("recorder", "sink")

    def streams(self, session_id: str) -> list[dict[str, Any]]:
        return [s.to_dict() for s in self._recorder().list_streams(self.session_dir(session_id))]

    def read_stream(self, session_id: str, stream: str, t0: float | None = None, t1: float | None = None,
                    max_points: int = 0) -> dict[str, Any]:
        try:
            info, t, _, data = self._recorder().read_stream(self.session_dir(session_id), stream, t0, t1)
        except FileNotFoundError as exc:
            raise NotFound(str(exc)) from exc
        t, data = envelope(t, data, max_points)
        return {"info": info.to_dict(), **chunk_message(stream, t, data)}

    def events(self, session_id: str, type_prefix: str | None = None, t0: float | None = None,
               t1: float | None = None) -> list[dict[str, Any]]:
        if session_id in self.active and not (self.data_dir / session_id / "events.jsonl").exists():
            evs = [e.to_dict() for e in self.active[session_id].recent_events]
        else:
            evs = self._recorder().read_events(self.session_dir(session_id))
        return [e for e in evs if (not type_prefix or e["type"].startswith(type_prefix))
                and (t0 is None or e["t"] >= t0) and (t1 is None or e["t"] <= t1)]

    def reports(self, session_id: str) -> list[str]:
        d = self.session_dir(session_id) / "reports"
        return sorted((p.name for p in d.glob("*.html")), reverse=True) if d.is_dir() else []

    def report_path(self, session_id: str, name: str) -> Path:
        p = self.session_dir(session_id) / "reports" / Path(name).name
        if not p.is_file():
            raise NotFound(f"no report {name!r}")
        return p

    def export(self, session_id: str, fmt: str, out: Path | None = None) -> list[str]:
        exporter = self.registry.get("export", "sink")
        files = exporter.export_session(self.session_dir(session_id), fmt, out)  # type: ignore[attr-defined]
        return [str(f) for f in files]

    # -- session management -------------------------------------------------------------------------
    def rename_session(self, session_id: str, label: str | None) -> dict[str, Any]:
        """Sets (or clears, with an empty label) a session's label, live or recorded."""
        session_id = self._check_id(session_id)
        label = (label or "").strip() or None
        if label is not None and len(label) > 200:
            raise ConfigError("labels are limited to 200 characters")
        live = self.active.get(session_id)
        if live is not None:
            live.info.label = label                       # the recorder writes it with the session metadata
        path = self.data_dir / session_id / "session.json"
        if path.is_file():
            info = json.loads(path.read_text(encoding="utf-8"))
            info["label"] = label
            tmp = path.with_name("session.json.tmp")
            tmp.write_text(json.dumps(info, indent=2), encoding="utf-8")
            tmp.replace(path)
        elif live is None:
            raise NotFound(f"unknown session {session_id!r}")
        return self.get_session(session_id)

    def delete_session(self, session_id: str) -> dict[str, Any]:
        """Deletes a recording and everything derived from it. Refuses a session that is still running."""
        session_id = self._check_id(session_id)
        live = self.active.get(session_id)
        if live is not None and not live.done.is_set():
            raise Busy(f"session {session_id} is still running; stop it first")
        shutil.rmtree(self.session_dir(session_id))
        self.active.pop(session_id, None)
        return {"deleted": session_id}

    def export_sessions(self, session_ids: list[str], fmt: str, out: Path) -> Path:
        """One file for one or more sessions: a session archive (.zip, importable), or CSV / EDF+ (one session:
        its file or zip; several: a zip with a folder per session)."""
        from ..archive import bundle, export_archive
        ids = list(dict.fromkeys(self._check_id(s) for s in session_ids))
        if not ids:
            raise ConfigError("select at least one session")
        dirs = [self.session_dir(s) for s in ids]
        for s in ids:
            if s in self.active and not self.active[s].done.is_set():
                raise Busy(f"session {s} is still recording")
        out.parent.mkdir(parents=True, exist_ok=True)
        if fmt == "session":
            return export_archive(dirs, out)
        if fmt not in ("csv", "edf"):
            raise ConfigError(f"unknown export format {fmt!r}; use session, csv or edf")
        with tempfile.TemporaryDirectory() as tmp:
            return bundle({s: [Path(f) for f in self.export(s, fmt, Path(tmp) / s / (f"{s}.edf" if fmt == "edf" else ""))]
                           for s in ids}, out)

    def import_sessions(self, path: Path) -> dict[str, Any]:
        """Imports the sessions in a session archive. Existing sessions are skipped, never overwritten."""
        from ..archive import ArchiveError, import_archive
        try:
            return import_archive(Path(path), self.data_dir, self._check_id)
        except ArchiveError as exc:
            raise ConfigError(str(exc)) from exc

    def repair(self, session_id: str) -> dict[str, Any]:
        if session_id in self.active and not self.active[session_id].done.is_set():
            raise Busy("cannot repair a session that is still recording")
        fixed = self._recorder().compact(self.session_dir(session_id))
        info_path = self.session_dir(session_id) / "session.json"
        info = json.loads(info_path.read_text(encoding="utf-8"))
        if info.get("status") in ("starting", "running"):
            info["status"] = "stopped"
            info["error"] = (info.get("error") or "") + " [repaired after abnormal termination]"
            info["ended_at"] = info.get("ended_at") or datetime.now(UTC).isoformat()
            info_path.write_text(json.dumps(info, indent=2), encoding="utf-8")
        return {"compacted_streams": fixed}
