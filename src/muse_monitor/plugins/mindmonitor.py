"""Muse data relayed by the Mind Monitor phone app over OSC (UDP), instead of a direct Bluetooth link.

In Mind Monitor, set "OSC Stream Target IP" to this computer's local network address and the port (default 5000),
leave the OSC path prefix empty, then tap the stream button. This source listens on that UDP port.

OSC carries one sample per message with no packet counter or device timestamp, so:
- sample times are rebuilt from the nominal rate, anchored to arrival time (minimum offset over 60 s, which removes
  network jitter and follows clock drift);
- lost samples can only be inferred from timing: when data keeps arriving later than the rate allows for longer
  than `gap_confirm_s`, the shortfall is recorded as a gap. Short losses (< `gap_tolerance_s`) go unnoticed.
Mind Monitor's raw EEG is in µV but not centred on zero; the source subtracts the converter midpoint: 725 µV for
a Muse S Athena (0-1450 µV, like OpenMuse), 841 µV for classic Muse headbands (0-1682.8 µV), or `eeg_offset_uv`."""

from __future__ import annotations

import asyncio
import math
import socket
import time
from collections import deque
from typing import Any

import numpy as np
from pydantic import BaseModel, Field

from muse_monitor.sdk import (MUSE_POSITIONS, Chunk, DeviceInfo, DeviceNotFound, Event, Health, Source,
                              SourceDisconnected, StreamInfo)

EEG_NAMES = ["TP9", "AF7", "AF8", "TP10"]
ATHENA_MIDPOINT_UV = 8192 * 1450.0 / 16383.0      # 14-bit, 0-1450 µV (same scale OpenMuse returns)
CLASSIC_MIDPOINT_UV = 1682.815 / 2                 # classic Muse via Mind Monitor: 0-1682.815 µV
# OSC address (after the prefix) -> stream id, kind, channel names (None: from the first message), units
STREAMS: dict[str, tuple[str, str, list[str] | None, str]] = {
    "/muse/eeg": ("eeg", "eeg", None, "uV"),
    "/muse/acc": ("acc", "acc", ["x", "y", "z"], "g"),
    "/muse/gyro": ("gyro", "gyro", ["x", "y", "z"], "deg/s"),
    "/muse/ppg": ("ppg", "ppg", None, "a.u."),
    "/muse/optics": ("optics", "optics", None, "a.u."),
}


def local_addresses() -> list[str]:
    """This computer's IPv4 addresses, to tell the user what to type into Mind Monitor."""
    out: set[str] = set()
    try:
        out.update(a[4][0] for a in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET))
    except OSError:
        pass
    try:                                       # the address used for the default route (no packet is sent)
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("10.255.255.255", 1))
            out.add(s.getsockname()[0])
    except OSError:
        pass
    return sorted(a for a in out if not a.startswith("127."))


class _Timeline:
    """Indices and times for a counter-less stream (see module docstring)."""

    def __init__(self, rate: float, tolerance_s: float, confirm_s: float) -> None:
        self.rate, self.tolerance, self.confirm = rate, tolerance_s * rate, confirm_s
        self.index = 0
        self.window: deque[tuple[float, float]] = deque()      # (arrival, offset)
        self.late_since: float | None = None
        self.last_t = -math.inf
        self.gaps = 0
        self.inferred_missing = 0

    def place(self, n: int, arrival: float) -> tuple[int, np.ndarray]:
        """n samples, the last of which arrived at `arrival` -> (first index, sample times)."""
        off = arrival - (self.index + n) / self.rate
        while self.window and arrival - self.window[0][0] > 60.0:
            self.window.popleft()
        best = min([o for _, o in self.window] + [off])
        deficit = (off - best) * self.rate       # samples this batch is "late" by
        if deficit > self.tolerance:
            if self.late_since is None:
                self.late_since = arrival
            elif arrival - self.late_since >= self.confirm:      # still late: data was lost, not delayed
                missing = int(round(deficit))
                self.index += missing
                self.gaps += 1
                self.inferred_missing += missing
                self.late_since = None
                off = arrival - (self.index + n) / self.rate
        else:
            self.late_since = None
        self.window.append((arrival, off))
        first = self.index
        t = best + (first + np.arange(n)) / self.rate
        if t[0] <= self.last_t:
            t = t + (self.last_t + 0.5 / self.rate - t[0])
        self.last_t = float(t[-1])
        self.index += n
        return first, t


class MindMonitor(Source):
    name = "mindmonitor"
    version = "1.1.0"            # 1.1: EEG centred automatically (Athena 725 µV, classic Muse 841 µV)
    summary = "Muse via the Mind Monitor phone app (OSC over UDP)"
    requires = ["pythonosc"]

    class Config(BaseModel):
        host: str = Field("0.0.0.0", description="Network address to listen on. 0.0.0.0 = all of this computer's "
                                                 "addresses; set Mind Monitor's target IP to one of them.")
        port: int = Field(5000, description="UDP port; must match Mind Monitor's OSC stream port (default 5000).")
        prefix: str = Field("", description="OSC path prefix, if one is set in Mind Monitor (normally empty).")
        eeg_rate: float = Field(256.0, description="EEG samples per second sent by Mind Monitor.")
        acc_rate: float = Field(52.0, description="Accelerometer and gyroscope samples per second.")
        ppg_rate: float = Field(64.0, description="PPG / optics samples per second.")
        eeg_offset_uv: float | None = Field(None, description="Subtracted from every EEG value to centre it on "
                                                             "zero. Empty: automatic (725 µV for a Muse S Athena, "
                                                             "841 µV for classic Muse headbands).")
        wait_s: float = Field(30.0, description="Seconds to wait for the first EEG message before giving up.")
        settle_s: float = Field(2.0, description="Seconds to watch which streams arrive before the session starts.")
        silence_s: float = Field(10.0, description="No EEG for this long counts as a disconnect.")
        chunk_s: float = Field(0.0625, description="Seconds of samples per chunk passed to the analyzers.")
        gap_tolerance_s: float = Field(0.5, description="Data arriving up to this late is treated as network delay; "
                                                        "beyond it (for gap_confirm_s), as lost samples.")
        gap_confirm_s: float = Field(1.0, description="How long data must stay late before a gap is recorded.")

    @classmethod
    async def discover(cls, config: Config) -> list[DeviceInfo]:
        """Listens briefly: reports Mind Monitor if any OSC data arrives on the port."""
        src = cls(config, None)
        try:
            await src._listen()
            deadline = time.monotonic() + min(config.wait_s, 8.0)
            while not src._seen and time.monotonic() < deadline:
                await asyncio.sleep(0.1)
        finally:
            await src.stop()
        if not src._seen:
            return []
        return [DeviceInfo(id=f"udp:{config.port}", name="Mind Monitor (OSC)", model="Muse via Mind Monitor",
                           extra={"addresses": sorted(src._seen)[:20], "sender": src._sender or ""})]

    def __init__(self, config: Config, ctx: Any) -> None:
        super().__init__(config, ctx)
        self._transport: Any = None
        self._stop = asyncio.Event()
        self._seen: set[str] = set()
        self._sender: str | None = None
        self._buffers: dict[str, list[tuple[float, list[float]]]] = {}
        self._timelines: dict[str, _Timeline] = {}
        self._declared: dict[str, StreamInfo] | None = None
        self._last_eeg: float | None = None
        self._battery: float | None = None
        self._messages = 0
        self._bad = 0
        self._eeg_offset = 0.0

    # -- receiving --------------------------------------------------------------------------------
    async def _listen(self) -> None:
        if self._transport is not None:
            return
        from pythonosc.dispatcher import Dispatcher
        from pythonosc.osc_server import AsyncIOOSCUDPServer
        d = Dispatcher()
        d.set_default_handler(self._on_message, needs_reply_address=True)
        server = AsyncIOOSCUDPServer((self.config.host, self.config.port), d, asyncio.get_running_loop())
        try:
            self._transport, _ = await server.create_serve_endpoint()
        except OSError as exc:
            raise DeviceNotFound(f"cannot listen on UDP {self.config.host}:{self.config.port}: {exc} "
                                 "(is another program using the port?)") from exc

    def _on_message(self, sender: tuple[str, int], address: str, *args: Any) -> None:
        arrival = time.time()
        self._messages += 1
        self._sender = sender[0] if sender else self._sender
        prefix = self.config.prefix.rstrip("/")
        if prefix and address.startswith(prefix):
            address = address[len(prefix):]
        self._seen.add(address)
        try:
            if address in STREAMS:
                values = [float(a) for a in args]
                if address == "/muse/eeg":
                    self._last_eeg = arrival
                self._buffers.setdefault(address, []).append((arrival, values))
            elif address == "/muse/batt" and args:
                level = float(args[0])
                self._battery = level / 100.0 if level > 100 else level      # some firmware report % × 100
            elif address.startswith("/Marker/") and self.ctx is not None and self._declared is not None:
                self.ctx.emit(Event("marker", arrival, source=self.ctx.instance_id,
                                    data={"label": f"Mind Monitor marker {address.rsplit('/', 1)[-1]}"}))
        except (TypeError, ValueError):
            self._bad += 1

    # -- source -----------------------------------------------------------------------------------
    async def open(self) -> list[StreamInfo]:
        c = self.config
        self._stop.clear()
        await self._listen()
        deadline = time.monotonic() + c.wait_s
        while not self._buffers.get("/muse/eeg") and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        if not self._buffers.get("/muse/eeg"):
            where = ", ".join(local_addresses()) or "this computer's address"
            heard = f" Received other OSC addresses: {sorted(self._seen)[:5]}." if self._seen else ""
            raise DeviceNotFound(f"no EEG from Mind Monitor on UDP port {c.port} within {c.wait_s:g} s. In Mind "
                                 f"Monitor set the OSC target IP to {where}, port {c.port}, an empty path prefix, "
                                 "and start streaming; allow UDP port "
                                 f"{c.port} through the Windows firewall.{heard}")
        if self._declared is not None:              # reconnect: same streams, the timelines record the gap
            return list(self._declared.values())
        await asyncio.sleep(c.settle_s)
        self.device = DeviceInfo(id=f"udp:{c.port}", name="Mind Monitor (OSC)", model="Muse via Mind Monitor",
                                 extra={"sender": self._sender or "", "addresses": sorted(self._seen)[:30]})
        self._declared = {s.id: s for s in self._declare()}
        return list(self._declared.values())

    def _declare(self) -> list[StreamInfo]:
        c = self.config
        out = []
        for address, (sid, kind, names, unit) in STREAMS.items():
            rows = self._buffers.get(address)
            if not rows:
                continue
            n = len(rows[-1][1])
            if names is None:
                names = (EEG_NAMES + [f"AUX_{i + 1}" for i in range(n - 4)])[:n] if sid == "eeg" else \
                    [f"{sid}{i + 1}" for i in range(n)]
            rate = c.eeg_rate if sid == "eeg" else c.acc_rate if kind in ("acc", "gyro") else c.ppg_rate
            span = rows[-1][0] - rows[0][0]
            measured = (len(rows) - 1) / span if span >= 0.5 else None
            if measured and abs(measured / rate - 1) > 0.1:       # the app streams at another rate: trust data
                self.ctx.log.warning("%s arrives at about %.1f Hz, not %g Hz; using the measured rate",
                                     sid, measured, rate)
                rate = float(round(measured)) if measured >= 10 else round(measured, 1)
            infer = sid == "eeg"                     # only EEG's rate is known well enough to infer lost samples
            meta: dict[str, Any] = {"via": "Mind Monitor OSC", "timing": "rebuilt from nominal rate + arrival",
                                    "gaps": "inferred from timing (no packet counter)" if infer else "not inferred",
                                    "measured_rate": round(measured, 2) if measured else None}
            if sid == "eeg":
                athena = bool(self._buffers.get("/muse/optics")) or n > 4
                if c.eeg_offset_uv is not None:
                    self._eeg_offset, how = c.eeg_offset_uv, "configured"
                else:
                    self._eeg_offset = ATHENA_MIDPOINT_UV if athena else CLASSIC_MIDPOINT_UV
                    how = "auto: Muse S Athena (optics or 8 EEG channels)" if athena else "auto: classic Muse"
                meta.update(offset_uv=-self._eeg_offset, offset_source=how)
            out.append(StreamInfo(sid, kind, list(names), [unit] * len(names), rate, meta=meta,
                                  positions={ch: MUSE_POSITIONS[ch] for ch in names if ch in MUSE_POSITIONS}
                                  if sid == "eeg" else None))
            self._timelines[address] = _Timeline(rate, c.gap_tolerance_s if infer else math.inf, c.gap_confirm_s)
        out.append(StreamInfo("battery", "battery", ["level"], ["%"], 0.0))
        return out

    def _flush(self) -> None:
        assert self._declared is not None
        for address, rows in self._buffers.items():
            if not rows or address not in self._timelines:
                continue
            self._buffers[address] = []
            sid = STREAMS[address][0]
            width = len(self._declared[sid].channels)
            good = [r for _, r in rows if len(r) == width]
            self._bad += len(rows) - len(good)
            if not good:
                continue
            first, t = self._timelines[address].place(len(good), rows[-1][0])
            data = np.asarray(good, dtype=np.float64)
            if sid == "eeg":
                data -= self._eeg_offset
            self.ctx.emit(Chunk(sid, data, t, first))

    async def run(self) -> None:
        c = self.config
        next_battery = 0.0
        while not self._stop.is_set():
            self._flush()
            now = time.time()
            if self._battery is not None and now >= next_battery:
                self.ctx.emit(Chunk.single("battery", [self._battery], now))
                next_battery = now + 5.0
            if self._last_eeg is not None and now - self._last_eeg > c.silence_s:
                raise SourceDisconnected(f"no EEG from Mind Monitor for {c.silence_s:g} s")
            try:
                await asyncio.wait_for(self._stop.wait(), c.chunk_s)
            except TimeoutError:
                pass
            await self.ctx.pace()

    async def stop(self) -> None:
        self._stop.set()
        transport, self._transport = self._transport, None
        if transport is not None:
            transport.close()

    def health(self) -> Health:
        counters: dict[str, float] = {"messages": self._messages, "malformed": self._bad}
        for address, tl in self._timelines.items():
            sid = STREAMS[address][0]
            counters[f"inferred_gaps_{sid}"] = tl.gaps
            counters[f"inferred_missing_{sid}"] = tl.inferred_missing
        if self._battery is not None:
            counters["battery"] = self._battery
        silent = self._last_eeg is not None and time.time() - self._last_eeg > 2.0
        return Health("degraded" if silent or self._bad else "ok",
                      "no EEG for over 2 s" if silent else ("malformed messages" if self._bad else ""), counters)
