from __future__ import annotations

import asyncio
import logging
import tempfile
from pathlib import Path
from typing import Any
from collections.abc import Callable

import numpy as np
import pytest

from muse_monitor.sdk import Chunk, Context, Event


class CaptureContext(Context):
    """Context for calling plugin methods directly in unit tests."""

    def __init__(self, mode: str = "offline") -> None:
        self.session_id = "test"
        self.instance_id = "test"
        self.mode = mode  # type: ignore[assignment]
        self.recordings_dir = Path(tempfile.mkdtemp())
        self.log = logging.getLogger("test")
        self.chunks: dict[str, list[Chunk]] = {}
        self.events: list[Event] = []

    def emit(self, item: Chunk | Event) -> None:
        if isinstance(item, Event):
            self.events.append(item)
        else:
            self.chunks.setdefault(item.stream, []).append(item)

    def stacked(self, stream: str) -> np.ndarray:
        return np.concatenate([c.data for c in self.chunks[stream]])

    def data_dir(self) -> Path:
        return Path(tempfile.mkdtemp())

    async def run_blocking(self, fn: Callable[..., Any], *args: Any) -> Any:
        return fn(*args)

    async def pace(self) -> None:
        await asyncio.sleep(0)


def sine_chunks(freq: float, seconds: float, rate: float = 256.0, channels: int = 4, amp: float = 20.0,
                t0: float = 1_700_000_000.0, step: int = 32, noise: float = 0.0) -> list[Chunk]:
    rng = np.random.default_rng(0)
    n = int(seconds * rate)
    t = np.arange(n) / rate
    x = amp * np.sin(2 * np.pi * freq * t)[:, None] + rng.normal(0, noise, (n, channels)) if noise else \
        np.tile(amp * np.sin(2 * np.pi * freq * t)[:, None], (1, channels))
    return [Chunk("eeg", x[a:a + step], t0 + t[a:a + step], a) for a in range(0, n, step)]


@pytest.fixture
def ctx() -> CaptureContext:
    return CaptureContext()


@pytest.fixture
def data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    d = tmp_path / "recordings"
    monkeypatch.setenv("MUSE_DATA_DIR", str(d))
    return d
