"""`muse train-probe`: fit a small linear head on a model's embeddings from labelled sessions, and compare it
honestly with the same classifier on band-power features.

Labels come from the recording's events:
  markers      each marker starts a segment labelled with its text, until the next marker or the end
               ("", "-" or "rest" mean unlabelled)
  calibration  the eyes-open / eyes-closed calibration periods
A window is used only if it lies completely inside one labelled segment.

Cross-validation holds out whole sessions. With a single session it falls back to contiguous time blocks with
a buffer around each test block (overlapping windows would otherwise leak), and says so: that estimate is
optimistic."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np

from .kernel.config import Pipeline, PluginEntry, SourceEntry
from .kernel.errors import ConfigError, KernelError
from .sdk import ProbeHead, storage

UNLABELLED = {"", "-", "rest"}


@dataclass
class Dataset:
    t: np.ndarray            # window end times
    session: np.ndarray      # session id per window
    labels: np.ndarray       # str
    model: np.ndarray        # embeddings (n, d)
    baseline: np.ndarray     # log10 band powers (n, channels*bands)
    meta: dict[str, Any]     # embedding stream meta (model, revision, channels, window_s)


def segments(events: list[dict[str, Any]], label_from: str, t_end: float) -> list[tuple[float, float, str]]:
    if label_from == "markers":
        marks = sorted((e["t"], str(e.get("data", {}).get("label", ""))) for e in events if e["type"] == "marker")
        ends = [t for t, _ in marks[1:]] + [t_end]
        return [(t0, t1, lab) for (t0, lab), t1 in zip(marks, ends) if lab.strip().lower() not in UNLABELLED]
    if label_from == "calibration":
        return [(e["t"], e["t"] + e.get("duration", 0.0), e["type"].split(".", 1)[1]) for e in events
                if e["type"] in ("calibration.eyes_open", "calibration.eyes_closed")]
    raise ConfigError(f"unknown label source {label_from!r} (use markers or calibration)")


def label_windows(t_end: np.ndarray, window_s: float, segs: list[tuple[float, float, str]]) -> np.ndarray:
    out = np.full(len(t_end), "", dtype=object)
    for t0, t1, lab in segs:
        out[(t_end - window_s >= t0) & (t_end <= t1)] = lab
    return out


async def collect(facade: Any, session_ids: list[str], plugin: str, options: dict[str, Any],
                  label_from: str) -> Dataset:
    cls = facade.registry.get(plugin, "analyzer")
    cfg = cls.Config(**options)
    if not cls.pulled(cfg):
        raise ConfigError(f"weights for {plugin} are not downloaded; run `muse models pull {plugin}`")
    rows: list[tuple[np.ndarray, ...]] = []
    meta: dict[str, Any] = {}
    for sid in session_ids:
        pipe = Pipeline(source=SourceEntry(plugin="replay"), analyzer=[
            PluginEntry(plugin=plugin, id="model", **{**options, "head": None}),
            PluginEntry(plugin="bandpower", id="bp", window_s=cfg.window_s, step_s=cfg.step_s, use_quality=False,
                        channels=cfg.channels)])
        job = await facade.wait_job((await facade.run_offline(sid, pipe, report=False))["id"])
        if job["status"] != "done":
            raise KernelError(f"analysis of {sid} failed: {job['error']}")
        d = facade.session_dir(sid)
        info, te, _, emb = storage.read_stream(d, "model/embedding")
        _, tb, _, bp = storage.read_stream(d, "bp/abs")
        meta = info.meta
        # band power and the model use the same windows, so their end times match
        j = np.clip(np.searchsorted(tb, te), 0, len(tb) - 1)
        j = np.where((j > 0) & (np.abs(tb[j - 1] - te) < np.abs(tb[j] - te)), j - 1, j)
        near = np.abs(tb[j] - te) < cfg.step_s / 2
        base = np.log10(np.maximum(bp[j], 1e-12))
        _, raw_t, _, _ = storage.read_stream(d, "eeg")
        segs = segments(storage.read_events(d, derived=False), label_from, float(raw_t[-1]))
        labels = label_windows(te, cfg.window_s, segs)
        keep = near & (labels != "") & np.isfinite(emb).all(axis=1) & np.isfinite(base).all(axis=1)
        rows.append((te[keep], np.full(keep.sum(), sid, dtype=object), labels[keep], emb[keep], base[keep]))
    t, sess, lab, x_model, x_base = (np.concatenate(c) for c in zip(*rows))
    return Dataset(t, sess, lab.astype(str), x_model, x_base, meta)


def _folds(ds: Dataset, window_s: float, n_blocks: int = 5) -> tuple[list[tuple[np.ndarray, np.ndarray]], str]:
    sessions = np.unique(ds.session)
    if len(sessions) >= 2:
        return [(ds.session != s, ds.session == s) for s in sessions], "session-held-out"
    order = np.argsort(ds.t)
    blocks = np.empty(len(ds.t), int)
    blocks[order] = np.arange(len(ds.t)) * n_blocks // max(1, len(ds.t))
    folds = []
    for b in range(n_blocks):
        test = blocks == b
        if not test.any():
            continue
        lo, hi = ds.t[test].min() - window_s, ds.t[test].max() + window_s
        folds.append((~test & ((ds.t < lo) | (ds.t > hi)), test))
    return folds, "time-blocks (single session: optimistic)"


def _classifier() -> Any:
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    return make_pipeline(StandardScaler(), LogisticRegression(max_iter=5000, C=0.1))


def cross_validate(ds: Dataset, window_s: float) -> dict[str, Any]:
    from sklearn.metrics import balanced_accuracy_score
    folds, scheme = _folds(ds, window_s)
    out: dict[str, Any] = {"scheme": scheme, "folds": []}
    for train, test in folds:
        if len(np.unique(ds.labels[train])) < 2 or not test.any():
            continue
        fold = {"test_windows": int(test.sum()),
                "held_out": str(ds.session[test][0]) if scheme.startswith("session") else None}
        for name, x in (("model", ds.model), ("baseline", ds.baseline)):
            clf = _classifier().fit(x[train], ds.labels[train])
            fold[name] = float(balanced_accuracy_score(ds.labels[test], clf.predict(x[test])))
        out["folds"].append(fold)
    if not out["folds"]:
        raise ConfigError("not enough labelled data: every training fold needs at least two labels")
    for name in ("model", "baseline"):
        scores = [f[name] for f in out["folds"]]
        out[f"{name}_mean"], out[f"{name}_std"] = float(np.mean(scores)), float(np.std(scores))
    out["chance"] = 1.0 / len(np.unique(ds.labels))
    out["model_beats_baseline"] = out["model_mean"] > out["baseline_mean"]
    return out


def fit_head(ds: Dataset, meta: dict[str, Any]) -> ProbeHead:
    clf = _classifier().fit(ds.model, ds.labels)
    scaler, lr = clf.steps[0][1], clf.steps[1][1]
    return ProbeHead([str(c) for c in lr.classes_], scaler.mean_, scaler.scale_, lr.coef_, lr.intercept_, meta)


async def train_probe(facade: Any, session_ids: list[str], plugin: str = "cbramod", label_from: str = "markers",
                      out: str | Path = "head.npz", options: dict[str, Any] | None = None) -> dict[str, Any]:
    try:
        import sklearn  # noqa: F401
    except ImportError as exc:
        raise ConfigError("train-probe needs scikit-learn: uv sync --extra models") from exc
    if not session_ids:
        raise ConfigError("give at least one session")
    options = dict(options or {})
    ds = await collect(facade, session_ids, plugin, options, label_from)
    if len(np.unique(ds.labels)) < 2:
        raise ConfigError(f"need at least two labels, found {sorted(set(ds.labels))}")
    cv = cross_validate(ds, float(ds.meta.get("window_s", 4.0)))
    counts = {str(k): int(v) for k, v in zip(*np.unique(ds.labels, return_counts=True))}
    meta = {**ds.meta, "model": plugin, "label_from": label_from, "sessions": session_ids, "counts": counts,
            "trained_at": datetime.now(UTC).isoformat(), "cv": cv}
    head = fit_head(ds, meta)
    path = Path(out).resolve()
    head.save(path)
    verdict = ("The probe beats the band-power baseline." if cv["model_beats_baseline"] else
               "Band power does as well or better: the model is not recommended for this task.")
    return {"head": str(path), "classes": head.classes, "windows": counts, "cv": cv, "verdict": verdict}
