"""StreamBus: topic pub/sub of chunks and events with per-subscriber flow control."""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Literal

from ..sdk.types import Chunk, Event

# drop:   live analyzers  — drop oldest, flag a gap on that stream for the subscriber
# block:  offline         — never drop; sources wait in pace()
# fail:   critical sinks in live mode — overflow fails the session
# latest: live viewers    — drop oldest silently
Policy = Literal["drop", "block", "fail", "latest"]


class Overflow(Exception):
    pass


class Subscriber:
    def __init__(self, name: str, wants: Callable[[str], bool], *, events: bool, policy: Policy,
                 maxsize: int) -> None:
        self.name = name
        self.wants = wants
        self.events = events
        self.policy = policy
        self.maxsize = maxsize
        self.queue: asyncio.Queue[tuple[Chunk | Event | None, bool]] = asyncio.Queue()
        self.dropped = 0
        self.closed = False
        self._gaps: set[str] = set()
        self._space = asyncio.Event()
        self._space.set()

    def deliver(self, item: Chunk | Event, gap: bool) -> bool:
        """Enqueue; returns True if something had to be dropped."""
        if self.closed:
            return False
        dropped = False
        if self.queue.qsize() >= self.maxsize:
            if self.policy == "fail":
                raise Overflow(f"{self.name}: queue full ({self.maxsize})")
            if self.policy in ("drop", "latest"):
                old, _ = self.queue.get_nowait()
                self.queue.task_done()
                self.dropped += 1
                dropped = True
                if self.policy == "drop" and isinstance(old, Chunk):
                    self._gaps.add(old.stream)
        if isinstance(item, Chunk) and item.stream in self._gaps:
            self._gaps.discard(item.stream)
            gap = True
        self.queue.put_nowait((item, gap))
        if self.queue.qsize() >= self.maxsize:
            self._space.clear()
        return dropped

    def done(self) -> None:
        """Call after each get()."""
        self.queue.task_done()
        if self.queue.qsize() < self.maxsize:
            self._space.set()

    async def wait_space(self) -> None:
        while not self.closed and self.queue.qsize() >= self.maxsize:
            self._space.clear()
            await self._space.wait()

    def close(self) -> None:
        """Stop receiving and discard anything queued (keeps queue.join() consistent)."""
        self.closed = True
        while not self.queue.empty():
            self.queue.get_nowait()
            self.queue.task_done()
        self._space.set()


@dataclass
class StreamStats:
    samples: int = 0
    gaps: int = 0
    missing_samples: int = 0
    duplicates: int = 0
    t_first: float | None = None
    t_last: float | None = None

    def to_dict(self, rate: float) -> dict[str, int | float]:
        span = (self.t_last - self.t_first) if self.t_first is not None and self.t_last is not None else 0.0
        eff = (self.samples - 1) / span if span > 0 and self.samples > 1 else 0.0
        return {"samples": self.samples, "gaps": self.gaps, "missing_samples": self.missing_samples,
                "duplicates": self.duplicates, "effective_rate": round(eff, 3), "nominal_rate": rate}


class StreamBus:
    def __init__(self) -> None:
        self.subs: list[Subscriber] = []
        self.stats: dict[str, StreamStats] = {}
        self._expected: dict[str, int] = {}
        self._last_overflow: dict[str, float] = {}
        self.listeners: list[Callable[[Event], None]] = []   # see every event, including the bus's own

    def subscribe(self, sub: Subscriber) -> Subscriber:
        self.subs.append(sub)
        return sub

    def unsubscribe(self, sub: Subscriber) -> None:
        if sub in self.subs:
            self.subs.remove(sub)
        sub.close()

    def publish(self, item: Chunk | Event) -> None:
        if isinstance(item, Event):
            self._fanout(item, False)
            return
        stream, n = item.stream, item.n
        if n == 0:
            return
        st = self.stats.setdefault(stream, StreamStats())
        expected = self._expected.get(stream)
        if item.first_index < 0:
            item = replace(item, first_index=expected or 0)
        gap = False
        if expected is not None:
            if item.first_index < expected:
                st.duplicates += 1
                return
            if item.first_index > expected:
                missing = item.first_index - expected
                gap = True
                st.gaps += 1
                st.missing_samples += missing
                self._fanout(Event("stream.gap", float(item.t[0]), source="kernel",
                                   data={"stream": stream, "missing_samples": missing}), False)
        self._expected[stream] = item.first_index + n
        st.samples += n
        st.t_first = float(item.t[0]) if st.t_first is None else st.t_first
        st.t_last = float(item.t[-1])
        self._fanout(item, gap)

    def _fanout(self, item: Chunk | Event, gap: bool) -> None:
        if isinstance(item, Event):
            for listener in self.listeners:
                listener(item)
        overflowed: list[Subscriber] = []
        for sub in list(self.subs):
            if isinstance(item, Chunk):
                if not sub.wants(item.stream):
                    continue
            elif not sub.events:
                continue
            if sub.deliver(item, gap) and sub.policy == "drop":
                overflowed.append(sub)
        now = time.monotonic()
        for sub in overflowed:   # at most one overflow event per subscriber per second
            if now - self._last_overflow.get(sub.name, 0.0) >= 1.0:
                self._last_overflow[sub.name] = now
                self._fanout(Event("kernel.overflow", time.time(), source="kernel",
                                   data={"subscriber": sub.name, "dropped_total": sub.dropped}), False)

    async def pace(self) -> None:
        for sub in list(self.subs):
            if sub.policy == "block":
                await sub.wait_space()
        await asyncio.sleep(0)
