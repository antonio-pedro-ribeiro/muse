"""Mind Monitor OSC source: timing reconstruction and a real UDP round trip on localhost."""

import asyncio
import socket

import numpy as np
import pytest

from muse_monitor.plugins.mindmonitor import MindMonitor, _Timeline
from muse_monitor.sdk import DeviceNotFound
from muse_monitor.sdk.testing import check_plugin

from .conftest import CaptureContext

RATE = 256.0


def feed(tl: _Timeline, arrivals: list[float], per_batch: int = 16) -> list[int]:
    return [tl.place(per_batch, a)[0] for a in arrivals]


def steady(t0: float, seconds: float, per_batch: int = 16, rate: float = RATE) -> list[float]:
    step = per_batch / rate
    return [t0 + (k + 1) * step for k in range(int(seconds / step))]


def test_steady_stream_has_no_gaps_and_even_times():
    tl = _Timeline(RATE, 0.5, 1.0)
    jitter = np.random.default_rng(0).uniform(0, 0.03, 400)       # Wi-Fi jitter up to 30 ms
    firsts = feed(tl, [a + j for a, j in zip(steady(1000.0, 25), jitter)])
    assert tl.gaps == 0 and firsts == list(range(0, 16 * len(firsts), 16))
    _, t = tl.place(16, 1000.0 + 25.1)
    assert np.allclose(np.diff(t), 1 / RATE)


def test_delayed_burst_is_not_a_gap():
    tl = _Timeline(RATE, 0.5, 1.0)
    arrivals = steady(0.0, 10)
    held = [a for a in arrivals if 5.0 <= a < 6.0]                  # phone buffers 1 s, then sends it all at once
    arrivals = [a for a in arrivals if not 5.0 <= a < 6.0]
    i = next(k for k, a in enumerate(arrivals) if a >= 6.0)
    arrivals[i:i] = [6.0 + 0.001 * k for k in range(len(held))]
    feed(tl, arrivals)
    assert tl.gaps == 0


def test_lost_second_becomes_one_gap():
    tl = _Timeline(RATE, 0.5, 1.0)
    arrivals = [a for a in steady(0.0, 10) if not 5.0 <= a < 6.0]   # 1 s of packets never arrive
    feed(tl, arrivals)
    assert tl.gaps == 1 and abs(tl.inferred_missing - 256) <= 16


def test_slow_device_clock_is_not_mistaken_for_loss():
    tl = _Timeline(RATE, 0.5, 1.0)
    feed(tl, steady(0.0, 300, rate=RATE * 0.995))                  # device 0.5% slow, for 5 minutes
    assert tl.gaps == 0


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def send_like_mind_monitor(port: int, seconds: float, lose: tuple[float, float] | None = None) -> None:
    from pythonosc.udp_client import SimpleUDPClient
    client = SimpleUDPClient("127.0.0.1", port)
    loop = asyncio.get_running_loop()
    start = loop.time()
    n = 0
    for k in range(int(seconds * 16)):                              # 16 messages every 62.5 ms = 256 Hz
        await asyncio.sleep(max(0.0, start + k / 16 - loop.time()))  # absolute schedule: no drift from sleep
        now = k / 16
        for _ in range(16):
            x = 800.0 + 20.0 * np.sin(2 * np.pi * 10 * n / RATE)    # Mind Monitor style: not centred on zero
            if not (lose and lose[0] <= now < lose[1]):
                client.send_message("/muse/eeg", [x, x, x, x, 800.0])
            n += 1
        for _ in range(3 + (k % 4 == 0)):                           # 52 Hz: 3.25 per 62.5 ms on average
            client.send_message("/muse/acc", [0.0, 0.0, 1.0])
        if k == 16:
            client.send_message("/Marker/1", 1)
            client.send_message("/muse/batt", [7300, 4000, 4000, 30])


def test_udp_round_trip_with_marker_battery_and_loss():
    check_plugin(MindMonitor)
    port = free_port()
    ctx = CaptureContext("live")
    src = MindMonitor(MindMonitor.Config(host="127.0.0.1", port=port, settle_s=0.5, wait_s=5,
                                         eeg_offset_uv=800.0), ctx)

    async def go():
        sender = asyncio.create_task(send_like_mind_monitor(port, 6.0, lose=(3.0, 4.0)))
        streams = await src.open()
        runner = asyncio.create_task(src.run())
        await sender
        await asyncio.sleep(0.3)
        await src.stop()
        await runner
        return {s.id: s for s in streams}
    streams = asyncio.run(go())
    assert streams["eeg"].channels == ["TP9", "AF7", "AF8", "TP10", "AUX_1"]
    assert streams["eeg"].meta["offset_uv"] == -800.0 and streams["acc"].meta["gaps"] == "not inferred"
    assert 45 <= streams["acc"].rate <= 60
    eeg = ctx.chunks["eeg"]
    x = np.concatenate([c.data for c in eeg])
    assert abs(np.median(x[:, 0])) < 5 and np.abs(x[:, 0]).max() <= 20.5     # offset removed, µV kept
    received = sum(c.n for c in eeg)
    assert 1100 <= received <= 1300                                          # ~5 of 6 s arrived (UDP on localhost)
    idx = [(c.first_index, c.n) for c in eeg]
    jumps = [b - (a + n) for (a, n), (b, _) in zip(idx, idx[1:]) if b != a + n]
    # the lost second (256 samples), inferred; a sender stalled by a busy test machine looks like extra loss
    assert len(jumps) == 1 and 180 <= jumps[0] <= 450
    assert [e.data["label"] for e in ctx.events if e.type == "marker"] == ["Mind Monitor marker 1"]
    assert ctx.stacked("battery")[-1, 0] == pytest.approx(73.0)
    h = src.health().counters
    assert h["inferred_gaps_eeg"] == 1 and h["malformed"] == 0


def test_no_data_explains_what_to_set_in_the_app():
    port = free_port()
    src = MindMonitor(MindMonitor.Config(host="127.0.0.1", port=port, wait_s=0.5), CaptureContext("live"))

    async def go():
        try:
            await src.open()
        finally:
            await src.stop()
    with pytest.raises(DeviceNotFound, match=f"OSC target IP .* port {port}"):
        asyncio.run(go())


def test_unexpected_rate_is_measured_not_turned_into_gaps():
    port = free_port()
    ctx = CaptureContext("live")
    src = MindMonitor(MindMonitor.Config(host="127.0.0.1", port=port, settle_s=1.0, wait_s=5), ctx)

    async def send():
        from pythonosc.udp_client import SimpleUDPClient
        client, loop = SimpleUDPClient("127.0.0.1", port), asyncio.get_running_loop()
        start = loop.time()
        for k in range(48):                                      # 3 s; acc at only 16 Hz
            await asyncio.sleep(max(0.0, start + k / 16 - loop.time()))
            for _ in range(16):
                client.send_message("/muse/eeg", [0.0, 0.0, 0.0, 0.0])
            client.send_message("/muse/acc", [0.0, 0.0, 1.0])

    async def go():
        sender = asyncio.create_task(send())
        streams = {s.id: s for s in await src.open()}
        runner = asyncio.create_task(src.run())
        await sender
        await asyncio.sleep(0.2)
        await src.stop()
        await runner
        return streams
    streams = asyncio.run(go())
    assert 14 <= streams["acc"].rate <= 18
    idx = [(c.first_index, c.n) for c in ctx.chunks["acc"]]
    assert all(b == a + n for (a, n), (b, _) in zip(idx, idx[1:]))     # no invented gaps
