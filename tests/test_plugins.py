import asyncio

import numpy as np
import pytest
from pydantic import BaseModel
from scipy.signal import welch

from muse_monitor.kernel import PluginRegistry
from muse_monitor.plugins.artifacts import Artifacts
from muse_monitor.plugins.bandpower import BandPower
from muse_monitor.plugins.baseline import Baseline
from muse_monitor.plugins.filter import Filter
from muse_monitor.plugins.quality import Quality
from muse_monitor.sdk import MASK_FLAT, MASK_OK, Analyzer, Chunk, Event, StreamInfo, StreamSelector, ViewSpec, Windower
from muse_monitor.sdk.testing import check_plugin, run_analyzer, run_pipeline, synthetic

from .conftest import CaptureContext, sine_chunks

EEG = StreamInfo("eeg", "eeg", ["TP9", "AF7", "AF8", "TP10"], ["uV"] * 4, 256.0)


@pytest.mark.parametrize("name", [m.name for m in PluginRegistry(paths=[]).manifests()])
def test_builtin_plugins_pass_check(name):
    check_plugin(PluginRegistry(paths=[])._classes[name])


@pytest.mark.parametrize("freq,band", [(6, "theta"), (10, "alpha"), (20, "beta")])
def test_bandpower_peak(freq, band):
    out = run_analyzer(BandPower, source=synthetic(sines={freq: 20}, noise_uv=2), seconds=10)
    mr = out["mean_rel"]
    assert mr.info.channels[int(np.nanmean(mr.data, axis=0).argmax())] == band


def test_quality_flags_flat_channel():
    ctx = CaptureContext()
    q = Quality(Quality.Config(), ctx)
    q.setup({"eeg": EEG})
    x = np.random.default_rng(0).normal(0, 10, (512, 4))
    x[:, 2] = 0.0
    asyncio.run(q.process(Chunk("eeg", x, 1e9 + np.arange(512) / 256.0, 0), False))
    mask = ctx.stacked("mask")[-1]
    assert mask.tolist() == [MASK_OK, MASK_OK, MASK_FLAT, MASK_OK]


def test_filter_removes_drift_keeps_alpha():
    ctx = CaptureContext()
    f = Filter(Filter.Config(), ctx)
    f.setup({"eeg": EEG})
    for c in sine_chunks(10, 8, amp=20):
        drift = Chunk("eeg", c.data + 200 * np.sin(2 * np.pi * 0.1 * (c.t - 1.7e9))[:, None], c.t, c.first_index)
        asyncio.run(f.process(drift, c.first_index == 0))
    y = ctx.stacked("eeg")[512:]                            # skip the filter warm-up
    assert abs(y.mean()) < 3 and 12 < y.std() < 16          # 20 µV sine has std 14.1


def test_artifacts_count_blinks():
    out = run_analyzer(Artifacts, source=synthetic(blink_every_s=2), seconds=10)
    assert 4 <= len(out.events_of("artifact.blink")) <= 6
    assert not out.events_of("artifact.motion")


def test_baseline_then_normalised_bandpower():
    ctx = CaptureContext()
    b = Baseline(Baseline.Config(settle_s=0), ctx)
    b.setup({"eeg": EEG})
    t0 = 1_700_000_000.0

    async def go():
        await b.on_event(Event("calibration.eyes_open", t0))
        for c in sine_chunks(10, 10, amp=10, t0=t0, noise=1):
            await b.process(c, False)
        await b.on_event(Event("calibration.eyes_closed", t0 + 10))
        for c in sine_chunks(10, 10, amp=30, t0=t0 + 10, noise=1):
            await b.process(c, False)
        await b.on_event(Event("calibration.end", t0 + 20))
    asyncio.run(go())
    ready = [e for e in ctx.events if e.type == "baseline.ready"]
    assert len(ready) == 1
    assert ready[0].data["alpha_reactivity"] == pytest.approx(9.0, rel=0.15)   # amplitude x3 -> power x9

    ctx2 = CaptureContext()
    bp = BandPower(BandPower.Config(), ctx2)
    bp.setup({"eeg": EEG})

    async def go2():
        await bp.on_event(ready[0])
        for c in sine_chunks(10, 4, amp=10, t0=t0 + 30, noise=1):
            await bp.process(c, False)
    asyncio.run(go2())
    norm = ctx2.stacked("norm")
    alpha = list(BandPower.Config().bands).index("alpha")
    assert norm[:, alpha].mean() == pytest.approx(1.0, rel=0.15)


class AlphaRatio(Analyzer):
    """The example plugin from spec/plugins.md §9, verbatim apart from imports."""
    name, version = "alpha-ratio", "0.1.0"
    inputs = [StreamSelector(kind="eeg")]

    class Config(BaseModel):
        window_s: float = 2.0

    def setup(self, inputs):
        (self.src,) = inputs.values()
        self.win = Windower(self.config.window_s, step_s=0.5, rate=self.src.rate)
        return [StreamInfo(id="ratio", kind="derived", channels=self.src.channels,
                           units=["1"] * len(self.src.channels), rate=2.0)]

    async def process(self, chunk, gap):
        if gap:
            self.win.reset()
        for w in self.win.push(chunk):
            f, p = welch(w.data, fs=self.src.rate, axis=0)
            alpha = p[(f >= 8) & (f < 13)].mean(0)
            beta = p[(f >= 13) & (f < 30)].mean(0)
            self.ctx.emit(Chunk.single("ratio", alpha / beta, t=w.t_end))

    def views(self):
        return [ViewSpec(id="ratio", title="Alpha/beta ratio", type="timeseries", streams=["ratio"])]


def test_spec_example_plugin():
    check_plugin(AlphaRatio)
    out = run_analyzer(AlphaRatio, {}, source=synthetic(sines={10: 20}), seconds=10)
    assert (out["ratio"].data > 1).all()
    assert out.session.views[0]["streams"] == ["alpha-ratio/ratio"]


def test_every_builtin_view_explains_itself():
    out = run_pipeline([(Quality, {}), (Filter, {}), (Baseline, {}), (BandPower, {}), (Artifacts, {})],
                       synthetic(), seconds=3)
    assert len(out.session.views) == 9
    missing = [v["id"] for v in out.session.views if len(v["description"]) < 40]
    assert not missing, f"views without a description: {missing}"


@pytest.mark.parametrize("name", [m.name for m in PluginRegistry(paths=[]).manifests()])
def test_every_builtin_option_is_described(name):
    m = PluginRegistry(paths=[]).manifest(name)
    props = m.config_schema.get("properties", {})
    assert not [k for k, v in props.items() if not v.get("description")]
    assert set(m.config_defaults) == {k for k, f in PluginRegistry(paths=[])._classes[name].Config.model_fields.items()
                                      if not f.is_required()}


def test_openmuse_handles_settle_backlog_before_new_packets(monkeypatch):
    """Packets queued during settle must be decoded before packets arriving after open() (found on real hardware:
    the backlog was decoded late and its wrapped subpacket indices looked like ~250-subpacket gaps)."""
    import OpenMuse

    from muse_monitor.plugins.openmuse import OpenMuseSource

    def eeg(sub):
        return {"EEG": [{"tag_byte": 0x12, "subpkt_index": sub, "data": np.zeros((2, 8))}]}
    monkeypatch.setattr(OpenMuse, "parse_message", lambda line: eeg(int(line.split("\t")[2], 16)))
    ctx = CaptureContext("live")
    src = OpenMuseSource(OpenMuseSource.Config(), ctx)
    cb = src._make_callback("273e0013")
    for i in range(3):                          # settle: queued, streams not declared yet
        cb(None, bytearray([i]))
    src._declared = {s.id: s for s in src._declare()}
    cb(None, bytearray([3]))                     # after open() returned, before run() drained the backlog
    src._stop.set()
    asyncio.run(src.run())
    cb(None, bytearray([4]))
    assert [c.first_index for c in ctx.chunks["eeg"]] == [0, 2, 4, 6, 8]
    assert src.health().counters["lost_subpackets_0x12"] == 0
