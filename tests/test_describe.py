import itertools

from muse_monitor.plugins.artifacts import Artifacts
from muse_monitor.plugins.bandpower import BandPower
from muse_monitor.plugins.describe import Describe, Facts, check_wording, compose
from muse_monitor.plugins.quality import Quality
from muse_monitor.sdk import StreamInfo
from muse_monitor.sdk.testing import check_plugin, run_pipeline, synthetic

from .conftest import CaptureContext

REL = {"delta": 0.30, "theta": 0.15, "alpha": 0.25, "beta": 0.25, "gamma": 0.05}
GOOD_PROBE = {"scheme": "session-held-out", "model_mean": 0.82, "baseline_mean": 0.70, "chance": 0.5,
              "model_fold_wins": 3, "folds": 3}


def test_low_quality_says_only_that():
    text = compose(Facts(seconds=30, usable=0.3, rel=REL, norm={"alpha": 2.0}))
    assert text.startswith("Signal quality too low") and "30%" in text and "alpha" not in text


def test_baseline_comparison_and_hedge():
    f = Facts(seconds=30, usable=0.95, rel=REL, norm={"delta": 1.0, "theta": 0.9, "alpha": 1.7, "beta": 1.1,
                                                      "gamma": 1.0}, blinks=6, context="eyes closed")
    text = compose(f)
    assert text.startswith("During “eyes closed”: Compared with your eyes-open baseline")
    assert "alpha 1.7×" in text and "commonly seen with closed eyes" in text
    assert "12 blinks/min, 0 jaw-clench episodes, little movement." in text


def test_no_baseline_compares_with_earlier_in_session():
    f = Facts(seconds=30, usable=0.9, rel={**REL, "alpha": 0.40}, earlier_rel={**REL, "alpha": 0.20})
    text = compose(f)
    assert "Share of power by band" in text and "alpha share was higher than earlier in the session (40% vs 20%)" in text


def test_artifacts_named_before_rhythms_are_trusted():
    f = Facts(seconds=60, usable=0.9, rel={**REL, "delta": 0.55, "gamma": 0.2}, blinks=40, motion_s=12)
    text = compose(f)
    assert "likely comes from blinks and movement" in text and "mostly reflects muscle activity" in text
    beta = compose(Facts(seconds=30, usable=0.9, rel=REL, norm={"beta": 1.6}, jaw=3))
    assert "can accompany mental effort, but also muscle tension" in beta and "Jaw clenching" in beta


def test_probe_sentence_needs_validation():
    base = Facts(seconds=30, usable=0.9, rel=REL, probe={"relax": 0.78, "arithmetic": 0.22})
    assert "probe" not in compose(base)
    text = compose(Facts(**{**base.__dict__, "probe_validation": GOOD_PROBE}))
    assert "closest label was “relax” in 78% of windows (held-out accuracy 0.82 vs 0.70" in text


def test_wording_never_states_or_diagnoses():
    assert check_wording("You are anxious today") and check_wording("signs of a sleep disorder")
    norms = [{}] + [{b: r for b in ("delta", "theta", "alpha", "beta", "gamma")} for r in (0.5, 1.0, 2.0)]
    for usable, norm, earlier, blinks, jaw, motion, ctx, probe in itertools.product(
            (None, 0.3, 0.6, 0.95), norms, ({}, {**REL, "alpha": 0.1}, {**REL, "beta": 0.5}), (0, 30), (0, 2),
            (0.0, 10.0), ("", "eyes closed"), (None, GOOD_PROBE)):
        f = Facts(seconds=30, usable=usable, rel=REL, norm=norm, earlier_rel=earlier, blinks=blinks, jaw=jaw,
                  motion_s=motion, context=ctx, probe={"a": 0.6, "b": 0.4} if probe else {}, probe_validation=probe)
        text = compose(f)
        assert check_wording(text) is None, text
    for view in Describe(Describe.Config(), CaptureContext()).views():
        assert check_wording(view.description) is None


def describe_with_probe(validation: dict) -> Describe:
    d = Describe(Describe.Config(), CaptureContext())
    rel = StreamInfo("bandpower/mean_rel", "derived", list(REL), ["1"] * 5, 2.0)
    probs = StreamInfo("cbramod/probs", "derived", ["relax", "arithmetic"], ["1", "1"], 1.0,
                       meta={"validation": validation})
    d.setup({rel.id: rel, probs.id: probs})
    return d


def test_unvalidated_probe_is_left_out_with_a_reason():
    assert describe_with_probe(GOOD_PROBE).validation is not None
    weak = describe_with_probe({**GOOD_PROBE, "model_mean": 0.72})
    assert weak.validation is None and "did not beat band power by 0.05" in weak.probe_note
    lucky = describe_with_probe({**GOOD_PROBE, "model_fold_wins": 1})          # wins on average, not per session
    assert lucky.validation is None
    blocks = describe_with_probe({**GOOD_PROBE, "scheme": "time-blocks (single session: optimistic)"})
    assert blocks.validation is None


def test_end_to_end_scripted_session():
    check_plugin(Describe)
    script = [[0.0, "eyes open", {20.0: 8.0, 10.0: 4.0}], [60.0, "eyes closed", {10.0: 25.0, 20.0: 4.0}]]
    out = run_pipeline([(Quality, {}), (BandPower, {}), (Artifacts, {}), (Describe, {})],
                       synthetic(sines={20.0: 8.0}, script=script, noise_uv=4.0), seconds=120)
    texts = {round(e.t - 1_700_000_000.0): e.data["text"] for e in out.events_of("description")}
    assert sorted(texts) == [2, 32, 62]       # 30 s periods from the first band-power estimate (2 s window)
    assert texts[2].startswith("During “eyes open”")
    assert "alpha share was higher than earlier in the session" in texts[62]
    assert "commonly seen with closed eyes" in texts[62]
    report = next(r for r in out.session.reports if r.title.endswith("Session description"))
    assert report.notes[0].startswith("Whole session (02:00)")
    assert any(n.startswith("“eyes closed” (01:00–02:00)") for n in report.notes)
    assert all(check_wording(n) is None for n in report.notes)
    facts = out.events_of("description")[2].data["facts"]
    assert facts["context"] == "eyes closed" and facts["rel"]["alpha"] > 0.3


def test_needs_bandpower():
    grab = {}
    out = run_pipeline([(Describe, {})], synthetic(), seconds=5,
                       inject=lambda s: grab.setdefault("rt", s.runtime("describe")))
    assert "list `bandpower` before `describe`" in grab["rt"].error
    assert out.session.status == "stopped"                 # only describe fails
