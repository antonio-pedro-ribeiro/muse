"""Pipeline configuration (TOML) and app settings."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from ..sdk.plugin import Plugin
from .errors import ConfigError


class Reconnect(BaseModel):
    max_attempts: int = 10
    backoff_s: float = 2.0


class PluginEntry(BaseModel):
    model_config = ConfigDict(extra="allow")
    plugin: str
    id: str | None = None
    enabled: bool = True

    @property
    def instance_id(self) -> str:
        return self.id or self.plugin

    def options(self) -> dict[str, Any]:
        return dict(self.model_extra or {})


class SourceEntry(PluginEntry):
    reconnect: Reconnect = Field(default_factory=Reconnect)


class SessionSection(BaseModel):
    label: str | None = None


class Pipeline(BaseModel):
    model_config = ConfigDict(extra="forbid")
    session: SessionSection = Field(default_factory=SessionSection)
    source: SourceEntry
    analyzer: list[PluginEntry] = Field(default_factory=list)
    sink: list[PluginEntry] = Field(default_factory=list)

    def active(self) -> tuple[SourceEntry, list[PluginEntry], list[PluginEntry]]:
        return (self.source, [a for a in self.analyzer if a.enabled], [s for s in self.sink if s.enabled])


DEFAULT_ANALYZERS = ["quality", "filter", "baseline", "bandpower", "artifacts", "describe"]   # describe last


def default_pipeline(source: str = "openmuse", record: bool = True, **source_options: Any) -> Pipeline:
    return Pipeline(
        source=SourceEntry(plugin=source, **source_options),
        analyzer=[PluginEntry(plugin=a) for a in DEFAULT_ANALYZERS],
        sink=[PluginEntry(plugin="recorder")] if record else [],
    )


def parse_pipeline(data: dict[str, Any] | Pipeline) -> Pipeline:
    if isinstance(data, Pipeline):
        return data
    try:
        return Pipeline.model_validate(data)
    except ValidationError as exc:
        raise ConfigError(f"invalid pipeline: {exc}") from exc


def load_pipeline(path: str | Path) -> Pipeline:
    try:
        with open(path, "rb") as f:
            return parse_pipeline(tomllib.load(f))
    except FileNotFoundError as exc:
        raise ConfigError(f"pipeline file not found: {path}") from exc
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def build_config(cls: type[Plugin], entry: PluginEntry) -> BaseModel:
    opts = entry.options()
    known = set(cls.Config.model_fields)
    unknown = sorted(set(opts) - known - {"reconnect"})
    if unknown:
        raise ConfigError(f"{entry.instance_id}: unknown option(s) {unknown}; known: {sorted(known)}",
                          plugin=entry.instance_id)
    try:
        return cls.Config.model_validate({k: v for k, v in opts.items() if k in known})
    except ValidationError as exc:
        raise ConfigError(f"{entry.instance_id}: {exc}", plugin=entry.instance_id) from exc


def data_root() -> Path:
    return Path(os.environ.get("MUSE_DATA_DIR", "recordings")).resolve()
