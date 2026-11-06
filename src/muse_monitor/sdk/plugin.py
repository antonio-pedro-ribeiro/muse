"""Plugin base classes. Third-party plugins import only from ``muse_monitor.sdk``."""

from __future__ import annotations

import logging
from abc import ABC, abstractmethod
from pathlib import Path
from typing import Any, ClassVar, Literal
from collections.abc import Callable

from pydantic import BaseModel

from .types import Chunk, DeviceInfo, Event, Health, Report, SessionInfo, StreamInfo, StreamSelector
from .views import ViewSpec

API_VERSION = 1


class SourceDisconnected(Exception):
    """Raised by Source.run() when the device link drops; the kernel applies the reconnect policy."""


class IncompatibleInput(Exception):
    """Raised by Analyzer.setup() to refuse its input streams."""


class DeviceNotFound(Exception):
    """Raised by Source.open() when no device could be found or reached."""


class Context(ABC):
    """The plugin's only handle on the kernel."""

    session_id: str
    instance_id: str
    mode: Literal["live", "offline"]
    recordings_dir: Path
    log: logging.Logger

    @abstractmethod
    def emit(self, item: Chunk | Event) -> None:
        """Publish a chunk or event. Analyzer stream ids are namespaced by instance automatically."""

    @abstractmethod
    def data_dir(self) -> Path:
        """Per-plugin writable directory (caches, model weights)."""

    @abstractmethod
    async def run_blocking(self, fn: Callable[..., Any], *args: Any) -> Any:
        """Run slow code off the event loop (dedicated thread for cpu_bound plugins)."""

    @abstractmethod
    async def pace(self) -> None:
        """Sources call this after emitting. Offline: waits until consumers have room. Live: yields."""


class _EmptyConfig(BaseModel):
    pass


class Plugin:
    name: ClassVar[str]
    version: ClassVar[str] = "0.0.0"
    kind: ClassVar[str]
    api_version: ClassVar[int] = API_VERSION
    summary: ClassVar[str] = ""
    Config: ClassVar[type[BaseModel]] = _EmptyConfig
    requires: ClassVar[list[str]] = []      # importable modules; missing -> "unavailable"
    install_hint: ClassVar[str] = ""
    cpu_bound: ClassVar[bool] = False

    def __init__(self, config: Any, ctx: Context) -> None:
        self.config = config
        self.ctx = ctx

    async def start(self) -> None:
        """Acquire resources."""

    async def stop(self) -> None:
        """Release resources. Must be idempotent and safe without start()."""

    def health(self) -> Health:
        return Health()


class Source(Plugin):
    kind = "source"
    device: DeviceInfo | None = None        # set by open() when known

    @classmethod
    async def discover(cls, config: Any) -> list[DeviceInfo]:
        return []

    @abstractmethod
    async def open(self) -> list[StreamInfo]:
        """Connect and declare the streams this source will produce."""

    @abstractmethod
    async def run(self) -> None:
        """Emit chunks until stop() is called or the data ends. Raise SourceDisconnected on link loss."""


class Analyzer(Plugin):
    kind = "analyzer"
    inputs: ClassVar[list[StreamSelector]] = []
    # Stream kinds setup() may declare. The kernel sets up producers before the analyzers whose inputs select
    # those kinds, whatever their order in the pipeline (an analyzer only sees streams declared before it).
    produces: ClassVar[list[str]] = []

    @abstractmethod
    def setup(self, inputs: dict[str, StreamInfo]) -> list[StreamInfo]:
        """Validate inputs and declare outputs (ids relative to this instance)."""

    @abstractmethod
    async def process(self, chunk: Chunk, gap: bool) -> None:
        """Handle one input chunk. gap=True: samples were lost before it; reset state first."""

    async def on_event(self, event: Event) -> None:
        """Optional: react to events (markers, calibration, other plugins' results)."""

    def views(self) -> list[ViewSpec]:
        return []

    async def summarize(self) -> Report | None:
        return None


class Sink(Plugin):
    kind = "sink"
    inputs: ClassVar[list[StreamSelector]] = [StreamSelector(any=True)]
    critical: ClassVar[bool] = False        # True: failure or full queue stops the session

    async def open(self, streams: dict[str, StreamInfo], session: SessionInfo) -> None: ...

    @abstractmethod
    async def write(self, item: Chunk | Event) -> None: ...

    async def close(self) -> None: ...
