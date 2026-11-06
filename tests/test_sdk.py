import numpy as np
import pytest

from muse_monitor.sdk import (MASK_NOISY, MASK_OK, Aligner, Chunk, QualityMask, StreamInfo, StreamSelector, Windower,
                              band_powers, eeg_channels, welch_psd)


def test_chunk_validates_shapes():
    c = Chunk("eeg", np.zeros(4), np.arange(4.0))
    assert c.data.shape == (4, 1) and c.data.dtype == np.float32
    with pytest.raises(ValueError):
        Chunk("eeg", np.zeros((3, 2)), np.arange(4.0))


def test_streaminfo_roundtrip_and_units():
    s = StreamInfo("eeg", "eeg", ["TP9"], ["uV"], 256.0, positions={"TP9": (1.0, 2.0, 3.0)})
    assert StreamInfo.from_dict(s.to_dict()) == s
    with pytest.raises(ValueError):
        StreamInfo("eeg", "eeg", ["TP9", "AF7"], ["uV"], 256.0)


def test_selector_matching():
    s = StreamInfo("eeg", "eeg", ["a"], ["uV"], 256.0)
    assert StreamSelector(kind="eeg").matches(s)
    assert not StreamSelector(kind="acc").matches(s)
    assert StreamSelector(any=True).matches(s)
    assert not StreamSelector().matches(s)


def test_windower_overlap_and_timestamps():
    w = Windower(size_s=1.0, step_s=0.5, rate=10.0)
    t = np.arange(25) / 10.0
    windows = list(w.push(Chunk("x", np.arange(25.0), t, 0)))
    assert len(windows) == 4                              # starts at 0, 0.5, 1.0, 1.5 s
    assert windows[1].t_start == pytest.approx(0.5) and windows[1].t_end == pytest.approx(1.4)
    assert windows[0].data.shape == (10, 1)
    w.reset()
    assert list(w.push(Chunk("x", np.arange(5.0), t[:5], 0))) == []


def test_aligner_slices_and_interpolates():
    a = Aligner()
    a.push(Chunk("acc", np.arange(10.0), np.arange(10.0), 0))
    assert a.slice("acc", 2.0, 4.0)[:, 0].tolist() == [2.0, 3.0, 4.0]
    assert a.at("acc", np.array([2.5]))[0, 0] == pytest.approx(2.5)
    assert a.at("missing", np.array([1.0])) is None


def test_quality_mask():
    m = QualityMask(["TP9", "AF7"])
    assert m.ok(0.0).all()                                  # no mask yet: everything usable
    m.push(Chunk.single("quality/mask", [MASK_OK, MASK_NOISY], t=10.0), ["TP9", "AF7"])
    assert m.ok(10.5).tolist() == [True, False]
    assert not m.ok(20.0).any()                             # stale mask: nothing trusted


def test_band_powers_find_alpha():
    rate = 256.0
    t = np.arange(512) / rate
    x = np.sin(2 * np.pi * 10 * t)[:, None]
    f, p = welch_psd(x, rate)
    bp = band_powers(f, p, {"theta": (4, 8), "alpha": (8, 13), "beta": (13, 30)})
    assert bp[:, 0].argmax() == 1


def test_eeg_channels_skips_aux():
    assert eeg_channels(["TP9", "AF7", "AUX_1"]) == [0, 1]
    assert eeg_channels(["TP9", "AF7"], ["AF7"]) == [1]
    with pytest.raises(ValueError):
        eeg_channels(["TP9"], ["Cz"])
