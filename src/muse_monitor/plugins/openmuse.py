"""Muse S Athena over BLE via OpenMuse (direct connection, no phone)."""

from __future__ import annotations

import asyncio
import re
import time
from collections import deque
from typing import Any

import numpy as np
from pydantic import BaseModel, Field

from muse_monitor.sdk import (MUSE_POSITIONS, Chunk, DeviceInfo, DeviceNotFound, Health, Source, SourceDisconnected,
                              StreamInfo)

# preset -> (EEG channels, optics channels); from AGENTS.md (provisional)
PRESETS: dict[str, tuple[int, int]] = {
    **{p: (4, 0) for p in ("p20", "p21", "p50", "p51", "p60", "p61")},
    "p1035": (4, 4), "p1034": (8, 8), "p1043": (8, 8),
    "p1041": (8, 16), "p1042": (8, 16), "p1045": (8, 16), "p1046": (8, 4), "p4129": (8, 4),
}
EEG_TAGS = {4: 0x11, 8: 0x12}
OPTICS_TAGS = {4: 0x34, 8: 0x35, 16: 0x36}
ACCGYRO_TAG = 0x47
BATTERY_TAGS = (0x88, 0x98)
EEG_SCALE = 0.0885                     # µV per LSB (Athena), applied by OpenMuse


async def scan(timeout: float) -> list[DeviceInfo]:
    import bleak
    found = await bleak.BleakScanner.discover(timeout=timeout, return_adv=True)
    out = []
    for addr, (dev, adv) in found.items():
        name = dev.name or adv.local_name or ""
        if "muse" in name.lower():
            out.append(DeviceInfo(id=addr, name=name, model="Muse", rssi=adv.rssi))
    return sorted(out, key=lambda d: -(d.rssi if d.rssi is not None else -999))


class _Clock:
    """Reconstructs sample times: t = offset + n / rate, offset = min(arrival - n/rate) over 60 s.
    The minimum tracks the lowest-latency packets, which removes jitter; the sliding window follows drift."""

    def __init__(self, rate: float) -> None:
        self.rate = rate
        self.window: deque[tuple[float, float]] = deque()
        self.last_t = -np.inf

    def times(self, n_first: int, count: int, arrival: float) -> np.ndarray:
        off = arrival - (n_first + count) / self.rate
        self.window.append((arrival, off))
        while self.window and arrival - self.window[0][0] > 60.0:
            self.window.popleft()
        offset = min(o for _, o in self.window)
        t = offset + (n_first + np.arange(count)) / self.rate
        if t[0] <= self.last_t:
            t += self.last_t + 0.5 / self.rate - t[0]
        self.last_t = float(t[-1])
        return t


class _Stream:
    def __init__(self, tag: int, n_per_sub: int, rate: float) -> None:
        self.tag = tag
        self.n_per_sub = n_per_sub
        self.index = 0
        self.prev_sub: int | None = None
        self.lost_subpackets = 0
        self.subpackets = 0
        self.clock = _Clock(rate) if rate > 0 else None

    def advance(self, sub_idx: int | None) -> int:
        """Updates the sequence and returns how many samples were lost before this subpacket."""
        self.subpackets += 1
        if sub_idx is None and self.prev_sub is not None:
            sub_idx = (self.prev_sub + 1) % 256   # first subpacket of a packet: next slot
        missing = 0
        if sub_idx is not None:
            if self.prev_sub is not None:
                gap = (sub_idx - self.prev_sub) % 256
                if gap > 1:
                    self.lost_subpackets += gap - 1
                    missing = (gap - 1) * self.n_per_sub
            self.prev_sub = sub_idx
        return missing


class OpenMuseSource(Source):
    name = "openmuse"
    version = "1.0.0"
    summary = "Muse S Athena over Bluetooth LE (OpenMuse)"
    requires = ["OpenMuse", "bleak"]
    install_hint = "OpenMuse is a core dependency: run `uv sync`"

    class Config(BaseModel):
        address: str = Field("", description="Bluetooth address of the headband. Leave empty to use the "
                                             "strongest Muse in range (see `muse devices`).")
        preset: str = Field("p1041", description="Which sensors the headband streams. p1041 = 8 EEG channels + "
                                                 "optics + motion; p20/p21 = 4 EEG channels only.")
        scan_timeout: float = Field(8.0, description="Seconds to search for headbands.")
        connect_timeout: float = Field(20.0, description="Seconds to wait for the Bluetooth connection.")
        settle_s: float = Field(2.0, description="Seconds to watch which sensors stream before the session "
                                                 "starts.")

    @classmethod
    async def discover(cls, config: Config) -> list[DeviceInfo]:
        return await scan(config.scan_timeout)

    def __init__(self, config: Config, ctx: Any) -> None:
        super().__init__(config, ctx)
        self._client: Any = None
        self._stop = asyncio.Event()
        self._disconnected = asyncio.Event()
        self._states: dict[int, _Stream] = {}
        self._declared: dict[str, StreamInfo] | None = None
        self._pending: list[tuple[dict[str, list[dict]], float]] = []
        self._seen: set[int] = set()
        self._control = ""
        self._errors = 0
        self._mismatched = 0
        self._battery: float | None = None
        self._last_disconnect: float | None = None

    # -- connection ---------------------------------------------------------------------------------
    async def open(self) -> list[StreamInfo]:
        import bleak
        from OpenMuse import MuseS
        cfg = self.config
        address, name = cfg.address, ""
        if not address:
            found = await scan(cfg.scan_timeout)
            if not found:
                raise DeviceNotFound("no Muse found; is it on and not connected to the phone app?")
            address, name = found[0].id, found[0].name
        loop = asyncio.get_running_loop()
        self._stop.clear()
        self._disconnected.clear()
        self._client = bleak.BleakClient(
            address, timeout=cfg.connect_timeout,
            disconnected_callback=lambda _c: loop.call_soon_threadsafe(self._disconnected.set))
        try:
            await self._client.connect()
        except Exception as exc:
            raise DeviceNotFound(f"could not connect to {address}: {type(exc).__name__}: {exc}") from exc
        try:
            await self._client.start_notify(MuseS.CONTROL_UUID, self._on_control)
        except Exception as exc:     # control notifications only carry device info
            self.ctx.log.info("no control notifications: %s", exc)
        subscribed = 0
        for uuid in MuseS.DATA_CHARACTERISTICS:
            try:
                await self._client.start_notify(uuid, self._make_callback(uuid))
                subscribed += 1
            except Exception as exc:
                self.ctx.log.warning("could not subscribe to %s: %s", uuid[:8], exc)
        if not subscribed:
            raise DeviceNotFound("connected but could not subscribe to any data characteristic")
        await MuseS.initialize_device(self._client, cfg.preset)

        if self._declared is not None:                 # reconnect: keep streams, account for the outage
            outage = time.time() - (self._last_disconnect or time.time())
            for st in self._states.values():
                if st.clock is not None:
                    st.index += int(outage * st.clock.rate)
                st.prev_sub = None
            return list(self._declared.values())

        await asyncio.sleep(cfg.settle_s)
        self.device = DeviceInfo(id=address, name=name,
                                 model="Muse S Athena", preset=cfg.preset,
                                 serial=self._ctrl("sn"), firmware=self._ctrl("fw"),
                                 extra={"hw": self._ctrl("hw")})
        self._declared = {s.id: s for s in self._declare()}
        return list(self._declared.values())

    def _declare(self) -> list[StreamInfo]:
        from OpenMuse.decode import SENSORS, select_eeg_channels
        n_eeg, n_opt = PRESETS.get(self.config.preset, (8, 16))
        eeg_tag = next((t for t in self._seen if t in EEG_TAGS.values()), EEG_TAGS[n_eeg])
        n_eeg = SENSORS[eeg_tag]["n_channels"]
        names = [c.removeprefix("EEG_") for c in select_eeg_channels(n_eeg)]
        meta = {"preset": self.config.preset, "scale_uv_per_lsb": EEG_SCALE, "raw_dtype": "uint14"}
        streams = [StreamInfo("eeg", "eeg", names, ["uV"] * n_eeg, 256.0,
                              positions={c: MUSE_POSITIONS[c] for c in names if c in MUSE_POSITIONS}, meta=meta)]
        opt_tag = next((t for t in self._seen if t in OPTICS_TAGS.values()), OPTICS_TAGS.get(n_opt))
        if opt_tag is not None:
            n = SENSORS[opt_tag]["n_channels"]
            streams.append(StreamInfo("optics", "optics", [f"opt{i + 1}" for i in range(n)], ["a.u."] * n, 64.0,
                                      meta={"preset": self.config.preset}))
        streams.append(StreamInfo("acc", "acc", ["x", "y", "z"], ["g"] * 3, 52.0))
        streams.append(StreamInfo("gyro", "gyro", ["x", "y", "z"], ["deg/s"] * 3, 52.0))
        streams.append(StreamInfo("battery", "battery", ["level"], ["%"], 0.0))
        return streams

    def _ctrl(self, key: str) -> str:
        m = re.search(rf'"{key}"\s*:\s*"([^"]+)"', self._control)
        return m.group(1) if m else ""

    def _on_control(self, _: Any, data: bytearray) -> None:
        self._control = (self._control + data.decode("utf-8", errors="ignore"))[-4096:]

    def _make_callback(self, uuid: str):
        from OpenMuse import parse_message
        from OpenMuse.utils import get_utc_timestamp

        def cb(_: Any, data: bytearray) -> None:
            arrival = time.time()
            try:
                parsed = parse_message(f"{get_utc_timestamp()}\t{uuid}\t{data.hex()}")
                if self._declared is None:
                    for subs in parsed.values():
                        self._seen.update(s["tag_byte"] for s in subs)
                    self._pending.append((parsed, arrival))
                else:
                    self._handle(parsed, arrival)
            except Exception as exc:   # never let a bad packet kill the BLE callback
                self._errors += 1
                if self._errors <= 5:
                    self.ctx.log.warning("packet error: %s: %s", type(exc).__name__, exc)
        return cb

    # -- decoding -----------------------------------------------------------------------------------
    def _handle(self, parsed: dict[str, list[dict]], arrival: float) -> None:
        from OpenMuse.decode import SENSORS
        assert self._declared is not None
        by_tag: dict[int, list[dict]] = {}
        for subs in parsed.values():
            for sub in subs:
                by_tag.setdefault(sub["tag_byte"], []).append(sub)
        for tag, subs in by_tag.items():
            cfg = SENSORS.get(tag)
            if cfg is None or cfg["type"] not in ("EEG", "OPTICS", "ACCGYRO", "BATTERY"):
                continue
            st = self._states.setdefault(tag, _Stream(tag, cfg["n_samples"], cfg["rate"] if cfg["type"] != "BATTERY"
                                                      else 0.0))
            for sub in subs:
                data = np.atleast_2d(np.asarray(sub["data"], dtype=np.float64))
                st.index += st.advance(sub.get("subpkt_index"))
                if cfg["type"] == "BATTERY":
                    self._battery = float(data[-1, 0])
                    self.ctx.emit(Chunk.single("battery", [self._battery], arrival))
                    continue
                t = st.clock.times(st.index, data.shape[0], arrival) if st.clock else np.full(data.shape[0], arrival)
                if cfg["type"] == "ACCGYRO":
                    self._emit("acc", data[:, :3], t, st.index)
                    self._emit("gyro", data[:, 3:6], t, st.index)
                else:
                    self._emit("eeg" if cfg["type"] == "EEG" else "optics", data, t, st.index)
                st.index += data.shape[0]

    def _emit(self, stream: str, data: np.ndarray, t: np.ndarray, index: int) -> None:
        info = self._declared.get(stream) if self._declared else None
        if info is None or data.shape[1] != len(info.channels):
            self._mismatched += 1
            return
        self.ctx.emit(Chunk(stream, data, t, index))

    async def run(self) -> None:
        pending, self._pending = self._pending, []
        for parsed, arrival in pending:
            self._handle(parsed, arrival)
        stop = asyncio.create_task(self._stop.wait())
        gone = asyncio.create_task(self._disconnected.wait())
        await asyncio.wait({stop, gone}, return_when=asyncio.FIRST_COMPLETED)
        stop.cancel()
        gone.cancel()
        if self._disconnected.is_set() and not self._stop.is_set():
            self._last_disconnect = time.time()
            raise SourceDisconnected("BLE link lost")

    async def stop(self) -> None:
        self._stop.set()
        client, self._client = self._client, None
        if client is None:
            return
        try:
            from OpenMuse import MuseS
            if client.is_connected:
                await MuseS.stop_streaming(client)
        except Exception:
            pass
        try:
            await client.disconnect()
        except Exception:
            pass

    def health(self) -> Health:
        counters: dict[str, float] = {"packet_errors": self._errors, "mismatched_chunks": self._mismatched}
        worst = 0.0
        for st in self._states.values():
            counters[f"lost_subpackets_{st.tag:#04x}"] = st.lost_subpackets
            if st.subpackets:
                worst = max(worst, st.lost_subpackets / (st.subpackets + st.lost_subpackets))
        if self._battery is not None:
            counters["battery"] = self._battery
        degraded = worst > 0.02 or self._errors > 0
        return Health("degraded" if degraded else "ok", f"worst subpacket loss {worst:.1%}" if degraded else "",
                      counters)
