"""The on-disk session format (spec/data.md §6). Public so sources, sinks and tools share one reader.

    <session>/session.json
    <session>/raw/<stream>.parquet            compacted stream
    <session>/raw/<stream>.parts/NNNNNN.parquet  row groups written while recording (crash-safe)
    <session>/events.jsonl
    <session>/derived/<instance>/<stream>.parquet
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Any

import numpy as np

from .types import Chunk, StreamInfo

META_KEY = b"muse.stream_info"


def stream_base(session_dir: Path, stream_id: str) -> Path:
    """Path without suffix; raw streams have no '/', derived ones are '<instance>/<stream>'."""
    if "/" in stream_id:
        inst, rest = stream_id.split("/", 1)
        return session_dir / "derived" / inst / rest.replace("/", "__")
    return session_dir / "raw" / stream_id


def _parts_dir(base: Path) -> Path:
    return base.with_name(base.name + ".parts")


def _schema(info: StreamInfo) -> Any:
    import pyarrow as pa
    reserved = {"t", "index"} & set(info.channels)
    if reserved:
        raise ValueError(f"stream {info.id!r}: channel names {reserved} are reserved")
    fields = [pa.field("t", pa.float64()), pa.field("index", pa.int64())]
    fields += [pa.field(ch, pa.float32()) for ch in info.channels]
    return pa.schema(fields, metadata={META_KEY: json.dumps(info.to_dict()).encode()})


class StreamWriter:
    """Buffers chunks and writes each flush as a complete Parquet file, so a crash loses at most one buffer."""

    def __init__(self, base: Path, info: StreamInfo) -> None:
        self.base, self.info = base, info
        self.parts = _parts_dir(base)
        self.parts.mkdir(parents=True, exist_ok=True)
        self.schema = _schema(info)
        self._n = len(list(self.parts.glob("*.parquet")))
        self._buf: list[Chunk] = []

    @property
    def buffered(self) -> int:
        return sum(c.n for c in self._buf)

    def buffered_span(self) -> float:
        return float(self._buf[-1].t[-1] - self._buf[0].t[0]) if self._buf else 0.0

    def append(self, chunk: Chunk) -> None:
        self._buf.append(chunk)

    def flush(self) -> None:
        if not self._buf:
            return
        import pyarrow as pa
        import pyarrow.parquet as pq
        t = np.concatenate([c.t for c in self._buf])
        idx = np.concatenate([np.arange(c.first_index, c.first_index + c.n, dtype=np.int64) for c in self._buf])
        data = np.concatenate([c.data for c in self._buf])
        cols = [pa.array(t), pa.array(idx)] + [pa.array(data[:, i]) for i in range(data.shape[1])]
        table = pa.Table.from_arrays(cols, schema=self.schema)
        tmp = self.parts / f"{self._n:06d}.tmp"
        pq.write_table(table, tmp)
        os.replace(tmp, self.parts / f"{self._n:06d}.parquet")
        self._n += 1
        self._buf = []


def compact(base: Path) -> bool:
    """Merge parts (and an existing compacted file) into <base>.parquet. Returns True if anything changed."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    parts = _parts_dir(base)
    files = sorted(parts.glob("*.parquet")) if parts.is_dir() else []
    if not files:
        if parts.is_dir():
            shutil.rmtree(parts)
        return False
    target = base.with_suffix(".parquet")
    tables = ([pq.read_table(target)] if target.exists() else []) + [pq.read_table(f) for f in files]
    table = pa.concat_tables(tables)
    tmp = base.with_suffix(".parquet.tmp")
    pq.write_table(table, tmp, row_group_size=256 * 60)
    os.replace(tmp, target)
    shutil.rmtree(parts)
    return True


def _files(base: Path) -> list[Path]:
    target = base.with_suffix(".parquet")
    parts = _parts_dir(base)
    return ([target] if target.exists() else []) + (sorted(parts.glob("*.parquet")) if parts.is_dir() else [])


def read_stream(session_dir: Path, stream_id: str, t0: float | None = None, t1: float | None = None
                ) -> tuple[StreamInfo, np.ndarray, np.ndarray, np.ndarray]:
    """Returns (info, t, index, data[n, channels])."""
    import pyarrow as pa
    import pyarrow.parquet as pq
    files = _files(stream_base(session_dir, stream_id))
    if not files:
        raise FileNotFoundError(f"no data for stream {stream_id!r} in {session_dir}")
    table = pa.concat_tables([pq.read_table(f) for f in files])
    info = StreamInfo.from_dict(json.loads(table.schema.metadata[META_KEY]))
    t = table.column("t").to_numpy()
    mask = np.ones(len(t), dtype=bool)
    if t0 is not None:
        mask &= t >= t0
    if t1 is not None:
        mask &= t <= t1
    idx = table.column("index").to_numpy()[mask]
    data = (np.stack([table.column(ch).to_numpy() for ch in info.channels], axis=1)
            if info.channels else np.empty((len(t), 0), np.float32))[mask]
    return info, t[mask], idx, data


def list_streams(session_dir: Path) -> list[StreamInfo]:
    import pyarrow.parquet as pq
    out: dict[str, StreamInfo] = {}
    for root in (session_dir / "raw", session_dir / "derived"):
        if not root.is_dir():
            continue
        for f in sorted(root.rglob("*.parquet")):
            try:
                meta = pq.read_schema(f).metadata or {}
                info = StreamInfo.from_dict(json.loads(meta[META_KEY]))
            except Exception:
                continue
            out.setdefault(info.id, info)
    return list(out.values())


def _jsonl(path: Path) -> list[dict[str, Any]]:
    out = []
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except json.JSONDecodeError:   # truncated last line after a crash
                continue
    return out


def read_events(session_dir: Path, derived: bool = True) -> list[dict[str, Any]]:
    """Raw events plus the latest offline analysis' events. Events from plugins that the offline run
    re-produced replace the live ones, so re-analysis never shows duplicates."""
    events = _jsonl(session_dir / "events.jsonl")
    if derived:
        offline = _jsonl(session_dir / "derived" / "events.jsonl")
        redone = {e.get("source") for e in offline} | {
            p.name for p in (session_dir / "derived").iterdir() if p.is_dir()} if (session_dir / "derived").is_dir() else set()
        redone -= {"", "user", "kernel"}
        events = [e for e in events if e.get("source") not in redone] + offline
    return sorted(events, key=lambda e: e.get("t", 0.0))


def compact_session(session_dir: Path) -> list[str]:
    changed = []
    for parts in sorted(session_dir.rglob("*.parts")):
        if parts.is_dir() and compact(parts.with_name(parts.name[: -len(".parts")])):
            changed.append(str(parts.relative_to(session_dir)))
    return changed
