import asyncio
from pathlib import Path

import numpy as np
import pytest
from pydantic import BaseModel

from muse_monitor.kernel import ConfigError, PluginRegistry, Session
from muse_monitor.kernel.config import Pipeline, PluginEntry, SourceEntry
from muse_monitor.sdk import Analyzer, Chunk, Sink, StreamInfo, StreamSelector
from muse_monitor.sdk.testing import run_pipeline, synthetic

CALLS: list[str] = []


class Recorder(Analyzer):
    """Counts chunks and remembers gap flags."""
    name, version = "probe", "0"
    inputs = [StreamSelector(kind="eeg")]

    class Config(BaseModel):
        delay_s: float = 0.0

    def setup(self, inputs):
        self.samples, self.gaps = 0, []
        return [StreamInfo("count", "derived", ["n"], ["1"], 0.0)]

    async def start(self):
        CALLS.append("probe.start")

    async def stop(self):
        CALLS.append("probe.stop")

    async def process(self, chunk, gap):
        if self.config.delay_s:
            await asyncio.sleep(self.config.delay_s)
        self.samples += chunk.n
        self.gaps.append(gap)
        self.ctx.emit(Chunk.single("count", [self.samples], chunk.t[-1]))


class Boom(Analyzer):
    name, version = "boom", "0"
    inputs = [StreamSelector(kind="eeg")]

    def setup(self, inputs):
        self.n = 0
        return []

    async def process(self, chunk, gap):
        self.n += 1
        if self.n == 5:
            raise RuntimeError("kaboom")


class Order(Sink):
    name, version = "order", "0"

    async def start(self):
        CALLS.append("sink.start")

    async def write(self, item):
        pass

    async def close(self):
        CALLS.append("sink.close")


def test_failing_analyzer_is_isolated():
    out = run_pipeline([(Boom, {}), (Recorder, {})], synthetic(), seconds=5)
    failed = out.events_of("plugin.failed")
    assert len(failed) == 1 and failed[0].data["plugin"] == "boom"
    assert out.session.status == "stopped"
    assert out["count"].data[-1, 0] == 5 * 256          # the other analyzer saw everything


def test_gap_reported_and_flagged():
    probe = {}

    def grab(session):
        probe["rt"] = session.runtime("probe")
    out = run_pipeline([(Recorder, {})], synthetic(gaps=[(2.0, 100)]), seconds=5, inject=grab)
    gaps = out.events_of("stream.gap")
    assert [g.data["missing_samples"] for g in gaps if g.data["stream"] == "eeg"] == [100]
    assert sum(probe["rt"].plugin.gaps) == 1
    assert out.session.stats["eeg"]["missing_samples"] == 100


def test_offline_never_drops():
    probe = {}
    out = run_pipeline([(Recorder, {"delay_s": 0.002})], synthetic(), seconds=10,
                       inject=lambda s: probe.setdefault("rt", s.runtime("probe")))
    assert out["count"].data[-1, 0] == 10 * 256
    assert probe["rt"].sub.dropped == 0


def test_live_overflow_drops_oldest_and_flags_gap(tmp_path: Path):
    reg = PluginRegistry(paths=[])
    reg.add(Recorder)
    pipe = Pipeline(source=SourceEntry(plugin="synthetic", realtime=False, duration_s=20, chunk_s=0.01),
                    analyzer=[PluginEntry(plugin="probe", delay_s=0.01)])

    async def go():
        s = Session(reg, pipe, data_dir=tmp_path, mode="live")
        await s.start()
        await s.done.wait()
        return s
    s = asyncio.run(go())
    rt = s.runtime("probe")
    assert rt.sub.dropped > 0
    assert any(rt.plugin.gaps)
    assert any(e.type == "kernel.overflow" for e in s.recent_events)


def test_start_and_stop_order(tmp_path: Path):
    CALLS.clear()
    reg = PluginRegistry(paths=[])
    reg.add(Recorder)
    reg.add(Order)
    pipe = Pipeline(source=SourceEntry(plugin="synthetic", realtime=False, duration_s=1),
                    analyzer=[PluginEntry(plugin="probe")], sink=[PluginEntry(plugin="order")])

    async def go():
        s = Session(reg, pipe, data_dir=tmp_path, mode="offline")
        await s.start()
        await s.done.wait()
    asyncio.run(go())
    assert CALLS == ["sink.start", "probe.start", "probe.stop", "sink.close"]


def test_config_errors():
    reg = PluginRegistry(paths=[])
    with pytest.raises(ConfigError, match="unknown option"):
        Session(reg, Pipeline(source=SourceEntry(plugin="synthetic", bogus=1)), data_dir=Path("."))
    with pytest.raises(ConfigError, match="duplicate"):
        Session(reg, Pipeline(source=SourceEntry(plugin="synthetic"),
                              analyzer=[PluginEntry(plugin="quality"), PluginEntry(plugin="quality")]),
                data_dir=Path("."))
    with pytest.raises(ConfigError):
        Session(reg, Pipeline(source=SourceEntry(plugin="quality")), data_dir=Path("."))


def test_newer_api_version_is_incompatible():
    class Future(Recorder):
        name, api_version = "future", 99
    reg = PluginRegistry(paths=[])
    reg.add(Future)
    assert reg.manifest("future").status == "incompatible"
    with pytest.raises(ConfigError):
        reg.get("future")


def test_local_plugin_directory(tmp_path: Path):
    (tmp_path / "mine.py").write_text(
        "from muse_monitor.sdk import Analyzer\n"
        "class Mine(Analyzer):\n    name = 'mine'\n    def setup(self, i): return []\n"
        "    async def process(self, c, g): pass\n", encoding="utf-8")
    (tmp_path / "broken.py").write_text("raise ImportError('nope')\n", encoding="utf-8")
    reg = PluginRegistry(paths=[tmp_path])
    assert reg.manifest("mine").kind == "analyzer"
    assert any("broken.py" in k for k in reg.broken)


def test_source_open_failure_skips_summaries(tmp_path: Path, caplog):
    from muse_monitor.plugins.bandpower import BandPower
    from muse_monitor.plugins.synthetic import Synthetic
    from muse_monitor.sdk import DeviceNotFound

    class Absent(Synthetic):
        name = "absent"

        async def open(self):
            raise DeviceNotFound("no headband")
    reg = PluginRegistry(paths=[])
    reg.add(Absent)
    reg.add(BandPower)
    pipe = Pipeline(source=SourceEntry(plugin="absent"), analyzer=[PluginEntry(plugin="bandpower")])

    async def go():
        s = Session(reg, pipe, data_dir=tmp_path, mode="live")
        with pytest.raises(DeviceNotFound):
            await s.start()
        return s
    s = asyncio.run(go())
    assert s.info.status == "failed" and "no headband" in s.info.error
    assert "summarize() failed" not in caplog.text
