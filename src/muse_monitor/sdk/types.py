"""Core data types shared by the kernel, plugins and interfaces."""

from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Literal

import numpy as np
from pydantic import BaseModel, Field


@dataclass(frozen=True)
class StreamInfo:
    id: str                       # "eeg", "bandpower/rel"
    kind: str                     # eeg | eeg_filtered | optics | acc | gyro | battery | derived | mask | ...
    channels: list[str]
    units: list[str]              # one per channel
    rate: float                   # nominal Hz; 0.0 = irregular
    positions: dict[str, tuple[float, float, float]] | None = None  # metres, MNE head frame
    meta: dict[str, Any] = field(default_factory=dict)
    producer: str = ""            # plugin instance id (set by the kernel)

    def __post_init__(self) -> None:
        if len(self.units) != len(self.channels):
            raise ValueError(f"stream {self.id!r}: {len(self.channels)} channels but {len(self.units)} units")

    def replace(self, **changes: Any) -> StreamInfo:
        return dataclasses.replace(self, **changes)

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        if self.positions is not None:
            d["positions"] = {k: list(v) for k, v in self.positions.items()}
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> StreamInfo:
        d = dict(d)
        if d.get("positions") is not None:
            d["positions"] = {k: tuple(v) for k, v in d["positions"].items()}
        names = {f.name for f in dataclasses.fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in names})


@dataclass(frozen=True)
class Chunk:
    stream: str
    data: np.ndarray              # (n_samples, n_channels) float32
    t: np.ndarray                 # (n_samples,) float64 UTC seconds
    first_index: int = -1         # sample counter since stream start; -1 = assigned by the kernel

    def __post_init__(self) -> None:
        data = np.asarray(self.data, dtype=np.float32)
        if data.ndim == 1:
            data = data.reshape(-1, 1)
        t = np.asarray(self.t, dtype=np.float64).reshape(-1)
        if data.shape[0] != t.shape[0]:
            raise ValueError(f"chunk {self.stream!r}: {data.shape[0]} samples but {t.shape[0]} timestamps")
        object.__setattr__(self, "data", data)
        object.__setattr__(self, "t", t)

    @property
    def n(self) -> int:
        return int(self.data.shape[0])

    @classmethod
    def single(cls, stream: str, values: Any, t: float, first_index: int = -1) -> Chunk:
        """One sample (one row) — the usual shape for derived, windowed outputs."""
        return cls(stream, np.asarray(values, dtype=np.float32).reshape(1, -1), np.array([t]), first_index)

    def with_stream(self, stream: str, first_index: int | None = None) -> Chunk:
        return dataclasses.replace(self, stream=stream,
                                   first_index=self.first_index if first_index is None else first_index)


@dataclass(frozen=True)
class Event:
    type: str                     # "marker", "artifact.blink", "stream.gap", "plugin.failed", ...
    t: float                      # UTC seconds
    duration: float = 0.0
    source: str = ""
    data: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Event:
        return cls(type=d["type"], t=float(d["t"]), duration=float(d.get("duration", 0.0)),
                   source=d.get("source", ""), data=d.get("data") or {})


@dataclass(frozen=True)
class StreamSelector:
    """Which streams a plugin consumes. All non-None criteria must match."""

    kind: str | None = None
    id: str | None = None
    any: bool = False
    optional: bool = False

    def matches(self, info: StreamInfo) -> bool:
        if self.any:
            return True
        if self.kind is not None and info.kind != self.kind:
            return False
        if self.id is not None and info.id != self.id:
            return False
        return self.kind is not None or self.id is not None


@dataclass
class Health:
    status: Literal["ok", "degraded", "failed"] = "ok"
    message: str = ""
    counters: dict[str, float] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


@dataclass(frozen=True)
class Window:
    data: np.ndarray              # (n_samples, n_channels)
    t: np.ndarray                 # (n_samples,)

    @property
    def t_start(self) -> float:
        return float(self.t[0])

    @property
    def t_end(self) -> float:
        return float(self.t[-1])


class DeviceInfo(BaseModel):
    id: str                       # address or other connect handle
    name: str = ""
    model: str = ""
    serial: str = ""
    firmware: str = ""
    preset: str = ""
    rssi: int | None = None
    extra: dict[str, Any] = Field(default_factory=dict)


class Report(BaseModel):
    """A plugin's end-of-session summary section."""

    title: str
    metrics: dict[str, float | int | str | None] = Field(default_factory=dict)
    notes: list[str] = Field(default_factory=list)


class SessionInfo(BaseModel):
    id: str
    label: str | None = None
    mode: Literal["live", "offline"] = "live"
    parent: str | None = None
    status: Literal["starting", "running", "stopped", "failed"] = "starting"
    error: str | None = None
    started_at: datetime
    ended_at: datetime | None = None
    device: DeviceInfo | None = None
    streams: list[dict[str, Any]] = Field(default_factory=list)   # StreamInfo.to_dict()
    views: list[dict[str, Any]] = Field(default_factory=list)     # ViewSpec.model_dump()
    pipeline: dict[str, Any] = Field(default_factory=dict)
    plugins: dict[str, str] = Field(default_factory=dict)         # instance -> "name==version"
    baseline: dict[str, Any] | None = None
    stats: dict[str, dict[str, int | float]] = Field(default_factory=dict)
    reports: list[Report] = Field(default_factory=list)
    app_version: str = ""

    def stream_infos(self) -> list[StreamInfo]:
        return [StreamInfo.from_dict(s) for s in self.streams]
