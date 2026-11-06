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


def test_analyzers_are_set_up_after_the_producers_they_read():
    """`bandpower` listed before `quality` used to miss quality/mask silently (its mask input is optional)."""
    from muse_monitor.plugins.bandpower import BandPower
    from muse_monitor.plugins.quality import Quality
    grab = {}
    run_pipeline([(BandPower, {}), (Quality, {})], synthetic(), seconds=3,
                 inject=lambda s: grab.setdefault("s", s))
    s = grab["s"]
    assert [rt.instance_id for rt in s.analyzers] == ["quality", "bandpower"]
    assert "quality/mask" in s.runtime("bandpower").inputs


def test_setup_order_is_stable_and_reports_cycles():
    from types import SimpleNamespace

    from muse_monitor.kernel.session import setup_order

    def rt(name, inputs, produces):
        return SimpleNamespace(instance_id=name, cls=SimpleNamespace(inputs=inputs, produces=produces))
    a = rt("a", [StreamSelector(kind="derived", optional=True)], [])
    b = rt("b", [StreamSelector(kind="eeg")], ["derived"])
    c = rt("c", [StreamSelector(kind="eeg")], [])
    d = rt("d", [StreamSelector(id="b/x")], [])
    order, cyclic = setup_order([a, c, d, b])
    assert [x.instance_id for x in order] == ["c", "b", "a", "d"] and cyclic == []
    x = rt("x", [StreamSelector(kind="k2")], ["k1"])
    y = rt("y", [StreamSelector(kind="k1")], ["k2"])
    order, cyclic = setup_order([y, x])
    assert [r.instance_id for r in order] == ["y", "x"] and cyclic == ["y"]


def test_unseen_optional_input_is_logged(caplog):
    """A plugin without `produces` still can't be reordered: the analyzer that missed its stream says so."""
    from muse_monitor.plugins.bandpower import BandPower
    from muse_monitor.plugins.quality import Quality

    class Undeclared(Quality):
        name = "quality2"
        produces = []
    with caplog.at_level("WARNING"):
        run_pipeline([(BandPower, {}), (Undeclared, {})], synthetic(), seconds=2)
    assert "does not read ['quality2/mask']" in caplog.text


def test_offline_run_seeds_the_recorded_baseline_and_releases_the_job(data_dir: Path):
    """`bandpower/norm` exists from the first window of a re-analysis when the recording has a baseline."""
    import json

    from muse_monitor.kernel import KernelFacade
    from muse_monitor.kernel.config import default_pipeline
    from muse_monitor.sdk import storage
    f = KernelFacade(data_dir)

    async def go():
        info = await f.start_session(default_pipeline("synthetic", realtime=False, duration_s=20,
                                                      start_time=1_700_000_000.0))
        sid = (await f.wait_session(info["id"]))["id"]
        path = f.session_dir(sid) / "session.json"
        meta = json.loads(path.read_text(encoding="utf-8"))
        bands = ["delta", "theta", "alpha", "beta", "gamma"]
        meta["baseline"] = {"eyes_open": {c: {b: 10.0 for b in bands} for c in ["TP9", "AF7", "AF8", "TP10"]}}
        path.write_text(json.dumps(meta), encoding="utf-8")
        pipe = {"source": {"plugin": "replay"}, "analyzer": [{"plugin": "bandpower"}]}
        job = await f.wait_job((await f.run_offline(sid, pipe, report=False))["id"])
        return sid, job
    sid, job = asyncio.run(go())
    assert job["status"] == "done" and job["session_id"]
    assert f.jobs[job["id"]].session is None                          # snapshot released
    _, t_norm, _, norm = storage.read_stream(f.session_dir(sid), "bandpower/norm")
    _, t_rel, _, _ = storage.read_stream(f.session_dir(sid), "bandpower/mean_rel")
    assert len(t_norm) == len(t_rel) > 0 and np.isfinite(norm).all()


def test_finished_jobs_are_capped():
    from muse_monitor.kernel.facade import MAX_JOBS, Job, KernelFacade
    f = KernelFacade.__new__(KernelFacade)
    f.jobs = {}
    running = Job(id="run", kind="analysis", parent="p")
    f.jobs["run"] = running
    for i in range(MAX_JOBS + 20):
        f._forget_old_jobs()
        f.jobs[f"j{i:03d}"] = Job(id=f"j{i:03d}", kind="analysis", parent="p", status="done")
    assert len(f.jobs) == MAX_JOBS and "run" in f.jobs and f"j{MAX_JOBS + 19:03d}" in f.jobs
