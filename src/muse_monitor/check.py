"""Connectivity check for the Muse S Athena via OpenMuse (direct BLE, no phone).

Usage:
    muse-check scan
    muse-check test [--address MAC] [--duration 10] [--preset p1041] [--save raw.txt]
"""

from __future__ import annotations

import argparse
import asyncio
import re
import sys
import time
from dataclasses import dataclass, field

import bleak
import numpy as np
from OpenMuse import MuseS, parse_message
from OpenMuse.decode import SENSORS
from OpenMuse.utils import get_utc_timestamp

# Fraction of the nominal sample rate a stream must reach to count as healthy.
RATE_OK = 0.90
# Fraction of subpackets that may be missing (by sequence index) before a stream is degraded.
MAX_LOSS = 0.02
# EEG standard deviation bounds (µV) for a rough contact check.
EEG_FLAT_UV = 1.0
EEG_NOISY_UV = 100.0


@dataclass
class SensorStats:
    tag: int
    samples: int = 0
    subpackets: int = 0
    first: float | None = None
    last: float | None = None
    lost: int = 0
    _prev_idx: int | None = None
    chunks: list[np.ndarray] = field(default_factory=list)

    @property
    def cfg(self) -> dict:
        return SENSORS[self.tag]

    def add(self, sub: dict, now: float) -> None:
        data = np.atleast_2d(sub["data"])
        self.samples += data.shape[0]
        self.subpackets += 1
        self.first = self.first if self.first is not None else now
        self.last = now
        idx = sub.get("subpkt_index")
        # The first subpacket of each BLE packet carries no index of its own; it
        # occupies the next slot in the sequence, so it is not a loss.
        if idx is None and self._prev_idx is not None:
            idx = (self._prev_idx + 1) % 256
        if idx is not None:
            if self._prev_idx is not None:
                gap = (idx - self._prev_idx) % 256
                if gap > 1:
                    self.lost += gap - 1
            self._prev_idx = idx
        if self.cfg["type"] in ("EEG", "BATTERY"):
            self.chunks.append(data)


async def _scan(timeout: float) -> list[tuple[str, str, int | None]]:
    found = await bleak.BleakScanner.discover(timeout=timeout, return_adv=True)
    muses = []
    for addr, (dev, adv) in found.items():
        name = dev.name or adv.local_name or ""
        if "muse" in name.lower():
            muses.append((addr, name, adv.rssi))
    return sorted(muses, key=lambda m: -(m[2] or -999))


def cmd_scan(args: argparse.Namespace) -> int:
    print(f"Scanning for Muse headbands ({args.timeout:.0f}s)...")
    muses = asyncio.run(_scan(args.timeout))
    if not muses:
        print("No Muse found. Is it on, and not connected to the phone app?")
        return 1
    for addr, name, rssi in muses:
        print(f"  {name:<14} {addr}  RSSI {rssi} dBm")
    return 0


async def _test(args: argparse.Namespace) -> int:
    address = args.address
    if not address:
        print(f"Scanning for Muse headbands ({args.timeout:.0f}s)...")
        muses = await _scan(args.timeout)
        if not muses:
            print("FAIL: no Muse found. Is it on, and not connected to the phone app?")
            return 1
        address, name, rssi = muses[0]
        print(f"Using {name} {address} (RSSI {rssi} dBm)")

    stats: dict[int, SensorStats] = {}
    info: dict[str, str] = {}
    control_buf = ""
    raw = open(args.save, "w", encoding="utf-8") if args.save else None

    def on_control(_, data: bytearray) -> None:
        nonlocal control_buf
        control_buf = (control_buf + data.decode("utf-8", errors="ignore"))[-4096:]
        for key in ("fw", "hw", "sn"):
            m = re.search(rf'"{key}"\s*:\s*"([^"]+)"', control_buf)
            if m:
                info[key] = m.group(1)
        m = re.search(r'"bp"\s*:\s*([\d.]+)', control_buf)
        if m:
            info["battery"] = m.group(1)

    def make_cb(uuid: str):
        def cb(_, data: bytearray) -> None:
            now = time.monotonic()
            line = f"{get_utc_timestamp()}\t{uuid}\t{data.hex()}"
            if raw:
                raw.write(line + "\n")
            for subs in parse_message(line).values():
                for sub in subs:
                    tag = sub["tag_byte"]
                    stats.setdefault(tag, SensorStats(tag)).add(sub, now)

        return cb

    print(f"Connecting to {address} ...")
    t0 = time.monotonic()
    try:
        async with bleak.BleakClient(address, timeout=20.0) as client:
            print(f"Connected in {time.monotonic() - t0:.1f}s. Starting stream (preset {args.preset})...")
            # Control notifications only carry device info (firmware, battery); optional.
            try:
                await client.start_notify(MuseS.CONTROL_UUID, on_control)
            except Exception as exc:
                print(f"  note: no control notifications ({exc}); device info unavailable")
            subscribed = 0
            for uuid in MuseS.DATA_CHARACTERISTICS:
                try:
                    await client.start_notify(uuid, make_cb(uuid))
                    subscribed += 1
                except Exception as exc:
                    print(f"  warning: could not subscribe to {uuid[:8]}: {exc}")
            if not subscribed:
                print("FAIL: could not subscribe to any data characteristic.")
                return 1
            await MuseS.initialize_device(client, args.preset)
            print(f"Collecting data for {args.duration:.0f}s ...")
            await asyncio.sleep(args.duration)
            await MuseS.stop_streaming(client)
    except Exception as exc:  # BLE errors come in many types on WinRT
        print(f"FAIL: connection error: {type(exc).__name__}: {exc}")
        return 1
    finally:
        if raw:
            raw.close()

    return _report(stats, info, args)


def _report(stats: dict[int, SensorStats], info: dict[str, str], args) -> int:
    print()
    if info:
        print("Device: " + ", ".join(f"{k}={v}" for k, v in info.items()))
    if not stats:
        print("FAIL: connected but received no sensor data.")
        return 1

    print(f"{'stream':<10}{'tag':>6}{'ch':>4}{'samples':>9}{'rate Hz':>9}{'nominal':>9}{'lost':>6}  status")
    eeg_ok = False
    for tag, s in sorted(stats.items()):
        cfg = s.cfg
        span = (s.last - s.first) if s.first is not None and s.last != s.first else 0.0
        rate = s.samples / span if span else 0.0
        nominal = cfg["rate"]
        if cfg["type"] == "BATTERY":
            status = "ok"
        else:
            loss = s.lost / (s.subpackets + s.lost)
            status = "ok" if rate >= RATE_OK * nominal and loss <= MAX_LOSS else "DEGRADED"
        if cfg["type"] == "EEG" and status == "ok":
            eeg_ok = True
        print(
            f"{cfg['type']:<10}{tag:>#6x}{cfg['n_channels']:>4}{s.samples:>9}"
            f"{rate:>9.1f}{nominal:>9.1f}{s.lost:>6}  {status}"
        )

    for tag, s in stats.items():
        if s.cfg["type"] == "BATTERY" and s.chunks:
            print(f"\nBattery: {float(np.concatenate(s.chunks)[-1, 0]):.0f}%")

    eeg = [s for s in stats.values() if s.cfg["type"] == "EEG" and s.chunks]
    if eeg:
        from OpenMuse.decode import select_eeg_channels

        data = np.concatenate(eeg[0].chunks)
        names = select_eeg_channels(data.shape[1])
        print("\nEEG contact (std after removing drift below ~1 Hz; rough heuristic):")
        win = int(SENSORS[eeg[0].tag]["rate"])  # 1 s moving average as a crude high-pass
        kernel = np.ones(win) / win
        for name, col in zip(names, data.T):
            hp = col - np.convolve(col, kernel, mode="same") if len(col) > 2 * win else col - col.mean()
            sd = float(np.std(hp[win:-win] if len(hp) > 2 * win else hp))
            verdict = "flat?" if sd < EEG_FLAT_UV else "noisy / poor contact?" if sd > EEG_NOISY_UV else "ok"
            print(f"  {name:<9} std {sd:8.1f} uV  {verdict}")

    if args.save:
        print(f"\nRaw packets saved to {args.save}")
    print("\nRESULT:", "PASS" if eeg_ok else "FAIL (EEG not streaming at the expected rate)")
    return 0 if eeg_ok else 1


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="muse-check", description="Check Muse S Athena connectivity over Bluetooth LE.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("scan", help="list Muse headbands in range")
    sp.add_argument("--timeout", type=float, default=8.0)
    sp.set_defaults(func=cmd_scan)

    tp = sub.add_parser("test", help="connect, stream briefly and report data rates and signal quality")
    tp.add_argument("--address", help="MAC address (default: strongest Muse found by scanning)")
    tp.add_argument("--duration", type=float, default=10.0, help="seconds of data to collect (default 10)")
    tp.add_argument("--preset", default="p1041", help="device preset (default p1041 = all sensors)")
    tp.add_argument("--timeout", type=float, default=8.0, help="scan timeout in seconds")
    tp.add_argument("--save", metavar="FILE", help="also save raw packets in OpenMuse's text format")
    tp.set_defaults(func=lambda a: asyncio.run(_test(a)))

    args = p.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
