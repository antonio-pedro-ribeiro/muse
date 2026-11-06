"""Recording, crash safety, replay/offline analysis, reproducibility and export."""

import asyncio
import importlib.util
import json
from pathlib import Path

import numpy as np
import pytest

from muse_monitor.kernel import KernelFacade, NotFound
from muse_monitor.kernel.config import default_pipeline
from muse_monitor.sdk import Chunk, StreamInfo, storage


def record(data_dir: Path, seconds: float = 10, **source) -> tuple[KernelFacade, str]:
    f = KernelFacade(data_dir)
    pipe = default_pipeline("synthetic", realtime=False, duration_s=seconds, chunk_s=0.25,
                            start_time=1_700_000_000.0, **source)

    async def go():
        info = await f.start_session(pipe)
        await f.add_marker(info["id"], "eyes closed")
        return (await f.wait_session(info["id"]))["id"]
    return f, asyncio.run(go())


def analyze(f: KernelFacade, sid: str) -> dict:
    async def go():
        job = await f.run_offline(sid)
        return await f.wait_job(job["id"])
    return asyncio.run(go())


def test_record_replay_reproducible(data_dir: Path):
    f, sid = record(data_dir, gaps=[(4.0, 64)])
    info = f.get_session(sid)
    assert info["status"] == "stopped"
    assert info["stats"]["eeg"]["missing_samples"] == 64
    raw = data_dir / sid / "raw"
    assert sorted(p.name for p in raw.iterdir()) == ["acc.parquet", "battery.parquet", "eeg.parquet"]
    _, t, idx, data = storage.read_stream(data_dir / sid, "eeg")
    assert len(t) == 10 * 256 - 64 and idx[-1] == 10 * 256 - 1

    job1 = analyze(f, sid)
    assert job1["status"] == "done", job1["error"]
    first = storage.read_stream(data_dir / sid, "bandpower/mean_rel")[3]
    job2 = analyze(f, sid)
    second = storage.read_stream(data_dir / sid, "bandpower/mean_rel")[3]
    np.testing.assert_allclose(first, second, atol=1e-6)
    assert len(first) > 0

    offline = f.get_session(job2["session_id"])
    assert offline["mode"] == "offline" and offline["parent"] == sid
    assert offline["stats"]["eeg"]["missing_samples"] == 64        # gap seen again through replay
    assert all(h["dropped"] == 0 for h in offline["health"].values()) if "health" in offline else True
    assert (data_dir / sid / "reports" / job2["result"]["report"]).is_file()
    raw_events = [json.loads(x) for x in (data_dir / sid / "events.jsonl").read_text().splitlines()]
    assert sum(e["type"] == "marker" for e in raw_events) == 1      # replay did not duplicate the marker


def test_parts_are_readable_and_repair_compacts(tmp_path: Path):
    info = StreamInfo("eeg", "eeg", ["a", "b"], ["uV", "uV"], 256.0)
    w = storage.StreamWriter(storage.stream_base(tmp_path, "eeg"), info)
    for k in range(3):
        w.append(Chunk("eeg", np.ones((256, 2)) * k, 1e9 + k + np.arange(256) / 256, k * 256))
        w.flush()
    # simulate a crash: no compaction yet, data must still be readable
    _, t, idx, data = storage.read_stream(tmp_path, "eeg")
    assert len(t) == 768 and data[-1, 0] == 2
    assert storage.compact_session(tmp_path) == [str(Path("raw") / "eeg.parts")]
    assert (tmp_path / "raw" / "eeg.parquet").is_file() and not (tmp_path / "raw" / "eeg.parts").exists()
    assert len(storage.read_stream(tmp_path, "eeg")[1]) == 768


def test_stats_rebuilt_from_parts_after_crash(tmp_path: Path):
    info = StreamInfo("eeg", "eeg", ["a"], ["uV"], 256.0)
    w = storage.StreamWriter(storage.stream_base(tmp_path, "eeg"), info)
    w.append(Chunk("eeg", np.zeros(256), 1e9 + np.arange(256) / 256, 0))
    w.append(Chunk("eeg", np.zeros(256), 1e9 + (np.arange(256) + 266) / 256, 266))     # 10 samples lost
    w.flush()
    st = storage.stats_from_files(tmp_path)["eeg"]
    assert (st["samples"], st["gaps"], st["missing_samples"], st["duplicates"]) == (512, 1, 10, 0)
    assert st["effective_rate"] == pytest.approx(511 / (521 / 256), abs=1e-3)


def test_export_csv_and_edf(data_dir: Path):
    f, sid = record(data_dir, seconds=4)
    files = f.export(sid, "csv")
    eeg_csv = next(Path(p) for p in files if p.endswith("eeg.csv") and "filter" not in p)
    assert len(eeg_csv.read_text().splitlines()) == 4 * 256 + 1
    edf = Path(f.export(sid, "edf")[0])
    raw = edf.read_bytes()
    hdr = raw[:256].decode("ascii")
    assert hdr[:8].strip() == "0" and hdr[192:197] == "EDF+C"
    n_records, ns = int(hdr[236:244]), int(hdr[252:256])
    assert n_records == 4 and ns == 4 + 3 + 1                    # 4 EEG + 3 acc + annotations
    labels = [raw[256 + i * 16: 256 + (i + 1) * 16].decode().strip() for i in range(ns)]
    assert labels[0] == "EEG TP9" and labels[-1] == "EDF Annotations"
    assert len(raw) == int(hdr[184:192]) + n_records * (2 * (4 * 256 + 3 * 52) + 2 * int(
        raw[256 + ns * 216 + (ns - 1) * 8: 256 + ns * 216 + ns * 8].decode()))
    assert b"marker eyes closed" in raw


@pytest.mark.skipif(importlib.util.find_spec("mne") is None, reason="needs mne (dev dependency)")
def test_edf_opens_in_mne_aligned_with_annotations(tmp_path: Path):
    """The headband's clock runs off nominal (EEG ~256.7 Hz on the device we tested). Samples must land at their
    real times in the EDF, or they drift seconds from the annotations over a long session."""
    import mne

    from muse_monitor.plugins.export import export_edf
    rate, true_rate, t0 = 256, 256 * 1.003, 1e9
    info = StreamInfo("eeg", "eeg", ["TP9", "AF7"], ["µV", "µV"], float(rate))
    n = 600 * rate                                                     # 10 minutes
    idx = np.arange(n)
    idx = idx[(idx < 1000) | (idx >= 1100)]                            # 100 samples lost
    t = t0 + idx / true_rate
    x = np.stack([50 * np.sin(2 * np.pi * 0.37 * (t - t0)), np.full(len(t), -20.0)], axis=1)
    w = storage.StreamWriter(storage.stream_base(tmp_path, "eeg"), info)
    w.append(Chunk("eeg", x[:1000], t[:1000], 0))
    w.append(Chunk("eeg", x[1000:], t[1000:], 1100))
    w.flush()
    with open(tmp_path / "events.jsonl", "w", encoding="utf-8") as fh:
        fh.write(json.dumps({"type": "marker", "t": t0 + 500.0, "data": {"label": "late"}}) + "\n")
    raw = mne.io.read_raw_edf(export_edf(tmp_path, tmp_path / "x.edf"), preload=True, verbose="error")
    assert raw.ch_names == ["EEG TP9", "EEG AF7"] and raw.info["sfreq"] == rate
    data = raw.get_data() * 1e6                                        # MNE gives volts: units must parse as uV
    assert np.allclose(data[1, 2000:3000], -20.0, atol=0.01)
    k = 500 * rate                                                     # at the marker, 500 s in
    assert data[0, k] == pytest.approx(50 * np.sin(2 * np.pi * 0.37 * 500.0), abs=0.05)
    ann = {d: o for o, d in zip(raw.annotations.onset, raw.annotations.description)}
    assert ann["marker late"] == pytest.approx(500.0, abs=1e-3)
    assert ann["gap eeg"] == pytest.approx(1000 / true_rate, abs=0.01)
    assert raw.n_times == rate * 599            # 1 s records covering 598.2 s of real time (counter-based: 600)


def test_session_id_validation(data_dir: Path):
    f = KernelFacade(data_dir)
    for bad in ("../etc", "..", "a/b", "x" * 100):
        with pytest.raises(NotFound):
            f.session_dir(bad)


def test_reanalysis_replaces_live_events(data_dir: Path):
    f, sid = record(data_dir, seconds=8, blink_every_s=2)
    live_blinks = [e for e in f.events(sid) if e["type"] == "artifact.blink"]
    assert live_blinks
    analyze(f, sid)
    assert len([e for e in f.events(sid) if e["type"] == "artifact.blink"]) == len(live_blinks)
    assert len(f.events(sid, "marker")) == 1
