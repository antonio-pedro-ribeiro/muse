"""Export recorded sessions to CSV or EDF+ (with markers and artifacts as annotations)."""

from __future__ import annotations

import csv
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, Field

from muse_monitor.sdk import Chunk, Event, SessionInfo, Sink, StreamInfo, storage

Format = Literal["csv", "edf"]


class Export(Sink):
    name = "export"
    version = "1.0.0"
    summary = "CSV / EDF+ export at session close"
    inputs = []          # works from the recorded files, not the live stream
    requires = ["pyarrow"]

    class Config(BaseModel):
        formats: list[Format] = Field(default_factory=lambda: ["csv"],
                                      description="Formats written when the session ends: csv and/or edf.")
        out: str | None = Field(None, description="Output folder; default: export/ inside the session folder.")

    async def open(self, streams: dict[str, StreamInfo], session: SessionInfo) -> None:
        self.session = session

    async def write(self, item: Chunk | Event) -> None:
        pass

    async def close(self) -> None:
        if self.session.mode != "live":
            return
        d = self.ctx.recordings_dir / self.session.id
        for fmt in self.config.formats:
            out = Path(self.config.out) / f"{self.session.id}.{fmt}" if self.config.out else None
            files = await self.ctx.run_blocking(self.export_session, d, fmt, out)
            self.ctx.log.info("exported %s", [str(f) for f in files])

    @classmethod
    def export_session(cls, session_dir: Path, fmt: str, out: Path | None = None) -> list[Path]:
        if fmt == "csv":
            return export_csv(session_dir, out or session_dir / "export" / "csv")
        if fmt == "edf":
            if out is None or out.is_dir() or out.suffix.lower() != ".edf":   # a folder: name the file
                out = (out or session_dir / "export") / f"{session_dir.name}.edf"
            return [export_edf(session_dir, out)]
        raise ValueError(f"unknown export format {fmt!r}; use csv or edf")


def export_csv(session_dir: Path, out_dir: Path) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    files = []
    for info in storage.list_streams(session_dir):
        _, t, idx, data = storage.read_stream(session_dir, info.id)
        path = out_dir / f"{info.id.replace('/', '__')}.csv"
        with open(path, "w", newline="", encoding="utf-8") as f:
            w = csv.writer(f)
            w.writerow(["t", "index", *info.channels])
            for i in range(len(t)):
                w.writerow([f"{t[i]:.6f}", int(idx[i]), *(f"{v:.6g}" for v in data[i])])
        files.append(path)
    path = out_dir / "events.csv"
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["t", "duration", "type", "source", "data"])
        for e in storage.read_events(session_dir):
            w.writerow([f"{e['t']:.6f}", e.get("duration", 0), e["type"], e.get("source", ""),
                        json.dumps(e.get("data", {}))])
    files.append(path)
    return files


# -- EDF+ ----------------------------------------------------------------------------------------------
def _field(value: Any, width: int) -> bytes:
    s = str(value).encode("ascii", "replace")[:width]
    return s.ljust(width, b" ")


def _ascii_unit(unit: str) -> str:
    """EDF headers are ASCII: µV -> uV (which MNE and EDFbrowser recognise), ° -> deg."""
    return unit.replace("µ", "u").replace("μ", "u").replace("°", "deg")


def _num(x: float, width: int = 8) -> str:
    for prec in range(8, 0, -1):
        s = f"{x:.{prec}g}"
        if len(s) <= width:
            return s
    raise ValueError(f"cannot fit {x} in {width} chars")


def export_edf(session_dir: Path, out: Path) -> Path:
    """EDF+C writer: 1 s data records, gaps zero-filled and annotated, events as annotations.

    EDF needs an exact, fixed rate, but the headband's clocks run slightly off nominal (e.g. EEG ~256.7 Hz).
    Placing samples by counter would drift seconds from the annotations over a long session, so each
    continuous run is linearly resampled onto the nominal grid by its timestamps. Raw files stay exact."""
    order = {"eeg": 0, "optics": 1, "acc": 2, "gyro": 3}
    raw = sorted((s for s in storage.list_streams(session_dir)
                  if "/" not in s.id and s.rate > 0 and float(s.rate).is_integer()),
                 key=lambda s: (order.get(s.kind, 9), s.id))
    if not raw:
        raise ValueError("no regular-rate raw streams to export")
    signals: list[tuple[str, str, int, np.ndarray]] = []       # label, unit, rate, values (padded later)
    start: float | None = None
    loaded = []
    for info in raw:
        _, t, idx, data = storage.read_stream(session_dir, info.id)
        if len(t):
            loaded.append((info, t, idx, data))
            start = float(t[0]) if start is None else min(start, float(t[0]))
    if start is None:
        raise ValueError("session has no samples")
    annotations: list[tuple[float, float, str]] = []
    duration = 0.0
    for info, t, idx, data in loaded:
        rate = int(info.rate)
        rel = t - start
        n = int(np.floor(rel[-1] * rate + 1e-6)) + 1
        filled = np.zeros((n, data.shape[1]), dtype=np.float64)
        for run in np.split(np.arange(len(t)), np.flatnonzero(np.diff(idx) != 1) + 1):
            k0 = int(np.ceil(rel[run[0]] * rate - 1e-6))
            k1 = int(np.floor(rel[run[-1]] * rate + 1e-6))
            if k1 < k0:                                       # a lone sample: nearest grid slot
                filled[min(n - 1, int(round(rel[run[0]] * rate)))] = data[run[0]]
                continue
            grid = np.arange(k0, k1 + 1) / rate
            for c in range(data.shape[1]):
                filled[k0:k1 + 1, c] = np.interp(grid, rel[run], data[run, c])
        for a in np.flatnonzero(np.diff(idx) > 1):          # gaps -> annotation
            annotations.append(((t[a] - start) + 1 / rate, (idx[a + 1] - idx[a] - 1) / rate, f"gap {info.id}"))
        duration = max(duration, n / rate)
        for c, ch in enumerate(info.channels):
            label = f"{info.kind.upper()} {ch}" if info.kind != "eeg" else f"EEG {ch}"
            signals.append((label, _ascii_unit(info.units[c]), rate, filled[:, c]))
    for e in storage.read_events(session_dir):
        if e["type"].startswith(("stream.gap", "kernel.")):
            continue
        text = e["type"] + (f" {e['data']['label']}" if e.get("data", {}).get("label") else "")
        annotations.append((float(e["t"]) - start, float(e.get("duration", 0.0)), text))

    n_records = max(1, math.ceil(duration))
    per_record: list[list[bytes]] = [[] for _ in range(n_records)]
    for onset, dur, text in sorted(annotations):
        if onset < 0:
            continue
        r = min(n_records - 1, int(onset))
        clean = text.replace("\x14", " ").replace("\x15", " ")
        dur_s = f"\x15{dur:.3f}" if dur > 0 else ""
        per_record[r].append(f"+{onset:.4f}{dur_s}\x14{clean}\x14\x00".encode())
    tal_bytes = [f"+{r}\x14\x14\x00".encode() + b"".join(a) for r, a in enumerate(per_record)]
    ann_samples = max(30, math.ceil(max(len(b) for b in tal_bytes) / 2))

    ns = len(signals) + 1
    dt = datetime.fromtimestamp(start, UTC)
    header = b"".join([
        _field("0", 8), _field("X X X X", 80),
        _field(f"Startdate {dt.strftime('%d-%b-%Y').upper()} X X muse-monitor", 80),
        _field(dt.strftime("%d.%m.%y"), 8), _field(dt.strftime("%H.%M.%S"), 8),
        _field(256 * (ns + 1), 8), _field("EDF+C", 44), _field(n_records, 8), _field(1, 8), _field(ns, 4)])
    phys = []
    for _, _, _, v in signals:
        lo, hi = float(np.min(v)), float(np.max(v))
        if hi - lo < 1e-6:
            lo, hi = lo - 1.0, hi + 1.0
        phys.append((float(_num(lo)), float(_num(hi))))
    labels = [s[0] for s in signals] + ["EDF Annotations"]
    cols: list[list[bytes]] = [
        [_field(x, 16) for x in labels],
        [_field("", 80)] * ns,
        [_field(s[1], 8) for s in signals] + [_field("", 8)],
        [_field(_num(p[0]), 8) for p in phys] + [_field(-1, 8)],
        [_field(_num(p[1]), 8) for p in phys] + [_field(1, 8)],
        [_field(-32768, 8)] * ns,
        [_field(32767, 8)] * ns,
        [_field("", 80)] * ns,
        [_field(s[2], 8) for s in signals] + [_field(ann_samples, 8)],
        [_field("", 32)] * ns,
    ]
    header += b"".join(b"".join(c) for c in cols)

    digital = []
    for (_, _, rate, v), (lo, hi) in zip(signals, phys):
        padded = np.zeros(n_records * rate)
        padded[: len(v)] = v
        d = np.round((padded - lo) / (hi - lo) * 65535 - 32768)
        digital.append(np.clip(d, -32768, 32767).astype("<i2").reshape(n_records, rate))
    out.parent.mkdir(parents=True, exist_ok=True)
    with open(out, "wb") as f:
        f.write(header)
        for r in range(n_records):
            for d in digital:
                f.write(d[r].tobytes())
            f.write(tal_bytes[r].ljust(ann_samples * 2, b"\x00"))
    return out
