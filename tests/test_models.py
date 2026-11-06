import asyncio
import importlib.util
from pathlib import Path

import numpy as np
import pytest

from muse_monitor.kernel import KernelFacade, PluginRegistry
from muse_monitor.kernel.config import default_pipeline
from muse_monitor.probe import label_windows, segments
from muse_monitor.sdk import ModelAnalyzer, ProbeHead, Window, band_powers, welch_psd
from muse_monitor.sdk.testing import check_plugin, run_pipeline, synthetic

HAS_SKLEARN = importlib.util.find_spec("sklearn") is not None
EYES = [[0.0, "eyes open", {20.0: 6.0}], [30.0, "eyes closed", {10.0: 25.0}],
        [60.0, "eyes open", {20.0: 6.0}], [90.0, "eyes closed", {10.0: 25.0}]]


class FakeModel(ModelAnalyzer):
    """Stands in for a network: log alpha and beta power. Needs only a non-empty weights folder."""
    name, version = "fakemodel", "0"
    embedding_dim = 2

    def load(self, path: Path) -> None:
        self.loaded = path

    def preprocess(self, w: Window, rate: float) -> np.ndarray:
        f, p = welch_psd(w.data, rate)
        return band_powers(f, p, {"alpha": (8, 13), "beta": (13, 30)}).mean(axis=1)

    def infer(self, x: np.ndarray) -> np.ndarray:
        return np.log10(x)


@pytest.fixture
def weights(tmp_path: Path) -> str:
    d = tmp_path / "weights"
    d.mkdir()
    (d / "w.bin").write_bytes(b"0")
    return str(d)


def test_model_analyzer_contract(weights):
    check_plugin(FakeModel)
    out = run_pipeline([(FakeModel, {"weights": weights, "revision": "r1"})], synthetic(), seconds=10)
    emb = out["embedding"]
    assert emb.data.shape == (7, 2)                                   # 4 s windows every 1 s over 10 s
    assert emb.info.meta["revision"] == "r1" and emb.info.meta["channels"] == ["TP9", "AF7", "AF8", "TP10"]


def failure(cfg: dict) -> str | None:
    """Runs FakeModel next to bandpower; returns FakeModel's error (the session itself must keep going)."""
    from muse_monitor.plugins.bandpower import BandPower
    grab = {}
    out = run_pipeline([(FakeModel, cfg), (BandPower, {})], synthetic(), seconds=5,
                       inject=lambda s: grab.setdefault("rt", s.runtime("fakemodel")))
    assert out.session.status == "stopped" and len(out["mean_rel"].t) > 0
    return grab["rt"].error


def test_missing_weights_fail_with_pull_hint():
    assert "muse models pull fakemodel" in failure({"weights": "org/none", "revision": "r1"})


def test_probe_head_roundtrip_and_probabilities(tmp_path):
    head = ProbeHead(["a", "b"], np.zeros(2), np.ones(2), np.array([[1.0, -1.0]]), np.array([0.0]), {"x": 1})
    head.save(tmp_path / "h.npz")
    back = ProbeHead.load(tmp_path / "h.npz")
    assert back.classes == ["a", "b"] and back.meta == {"x": 1}
    p = back.predict_proba(np.array([[2.0, 0.0], [0.0, 0.0]]))
    assert p[0, 1] == pytest.approx(1 / (1 + np.exp(-2))) and p[1].tolist() == [0.5, 0.5]
    multi = ProbeHead(["a", "b", "c"], np.zeros(1), np.ones(1), np.array([[1.0], [0.0], [-1.0]]), np.zeros(3))
    assert multi.predict_proba(np.array([[3.0]])).argmax() == 0
    assert multi.predict_proba(np.array([[3.0]])).sum() == pytest.approx(1.0)


def test_segments_and_window_labels():
    evs = [{"type": "marker", "t": 10.0, "data": {"label": "open"}},
           {"type": "marker", "t": 20.0, "data": {"label": "-"}},
           {"type": "marker", "t": 25.0, "data": {"label": "closed"}},
           {"type": "calibration.eyes_open", "t": 0.0, "duration": 5.0}]
    segs = segments(evs, "markers", t_end=40.0)
    assert segs == [(10.0, 20.0, "open"), (25.0, 40.0, "closed")]
    labels = label_windows(np.array([13.0, 14.0, 22.0, 29.0, 31.0]), 4.0, segs)
    assert labels.tolist() == ["", "open", "", "closed", "closed"]       # window must fit inside a segment
    assert segments(evs, "calibration", 40.0) == [(0.0, 5.0, "eyes_open")]


def test_window_usable_averages_quality_inside_each_window():
    from muse_monitor.probe import window_usable
    tq = np.arange(1.0, 11.0)                                   # quality judged every second
    usable = np.where(tq <= 5, 1.0, 0.25)
    u = window_usable(np.array([4.0, 6.0, 10.0, 0.5]), 4.0, tq, usable)
    assert u[:3].tolist() == [1.0, 0.8125, 0.25] and np.isnan(u[3])


def test_probe_verdict_is_the_rule_describe_applies():
    from muse_monitor.probe import verdict
    from muse_monitor.sdk import probe_validated, probe_validation

    def cv(scheme, wins):                                      # model 0.80 vs band power 0.70 on average
        folds = [{"model": 0.9 if i < wins else 0.6, "baseline": 0.7} for i in range(3)]
        out = {"scheme": scheme, "folds": folds, "model_mean": 0.8, "baseline_mean": 0.7, "chance": 0.5}
        v = probe_validation(out)
        return {**out, "model_fold_wins": v["model_fold_wins"], "model_beats_baseline": probe_validated(v)}
    assert cv("session-held-out", 2)["model_beats_baseline"]
    assert not cv("session-held-out", 1)["model_beats_baseline"]          # better on average, not per session
    assert not cv("time-blocks (single session: optimistic)", 3)["model_beats_baseline"]
    assert "won 1 of 3" in verdict(cv("session-held-out", 1))
    assert "Only one session" in verdict(cv("time-blocks (single session: optimistic)", 3))
    close = probe_validation({"scheme": "session-held-out", "folds": [{"model": 0.72, "baseline": 0.7}] * 3,
                              "model_mean": 0.72, "baseline_mean": 0.7})
    assert not probe_validated(close)                                       # 0.02 < the 0.05 margin


def test_synthetic_script_adds_markers_and_changes_rhythm():
    out = run_pipeline([], synthetic(sines={20.0: 6.0}, script=EYES[:2], noise_uv=1.0), seconds=60)
    assert [e.data["label"] for e in out.events_of("marker")] == ["eyes open", "eyes closed"]
    x, t = out["eeg"].data[:, 0], out["eeg"].t - out["eeg"].t[0]
    f, p = welch_psd(x[t >= 30][:, None], 256.0)
    assert f[p[:, 0].argmax()] == pytest.approx(10.0, abs=0.5)


@pytest.mark.skipif(not HAS_SKLEARN, reason="needs the models extra")
def test_train_probe_end_to_end(data_dir, weights, tmp_path):
    reg = PluginRegistry()
    reg.add(FakeModel, origin="test")
    f = KernelFacade(data_dir, registry=reg)

    async def record(seed: int) -> str:
        pipe = default_pipeline("synthetic", realtime=False, duration_s=120, start_time=1_700_000_000.0 + seed * 1e4,
                                sines={20.0: 6.0}, script=EYES, seed=seed, noise_uv=8.0)
        info = await f.start_session(pipe)
        return (await f.wait_session(info["id"]))["id"]

    async def go():
        ids = [await record(s) for s in (1, 2)]
        return await f.train_probe(ids, plugin="fakemodel", out=str(tmp_path / "head.npz"),
                                   options={"weights": weights, "revision": "r1"})
    r = asyncio.run(go())
    cv = r["cv"]
    assert cv["scheme"] == "session-held-out" and len(cv["folds"]) == 2
    assert set(r["windows"]) == {"eyes open", "eyes closed"}
    assert cv["model_mean"] > 0.9 and cv["baseline_mean"] > 0.9               # easy, separable task
    assert r["low_quality_windows"] == 0                                     # clean synthetic contact
    from muse_monitor.sdk import probe_validated, probe_validation
    assert cv["model_beats_baseline"] == probe_validated(probe_validation(cv))
    head = ProbeHead.load(r["head"])
    assert head.classes == ["eyes closed", "eyes open"] and head.meta["model"] == "fakemodel"

    out = run_pipeline([(FakeModel, {"weights": weights, "revision": "r1", "head": r["head"]})],
                       synthetic(sines={10.0: 25.0}, noise_uv=8.0), seconds=10)
    assert out["probs"].data[:, 0].mean() > 0.9                            # alpha-heavy -> "eyes closed"
    assert "trained on weights r1" in failure({"weights": weights, "revision": "other", "head": r["head"]})


@pytest.mark.skipif(importlib.util.find_spec("braindecode") is None, reason="needs the models extra")
def test_cbramod_embeds_muse_windows():
    from muse_monitor.plugins.cbramod import CBraMod
    cfg = CBraMod.Config()
    if not CBraMod.pulled(cfg):
        pytest.skip("run `muse models pull cbramod` first")
    check_plugin(CBraMod)
    out = run_pipeline([(CBraMod, {})], synthetic(), seconds=8)
    emb = out["embedding"].data
    assert emb.shape == (5, 200) and np.isfinite(emb).all() and emb.std() > 0
