"""Model analyzers: a pretrained network turns EEG windows into embeddings, and an optional probe head (trained
with `muse train-probe`) turns embeddings into class probabilities.

Weights are fetched only by `muse models pull`; a session never downloads anything. Heads are plain numpy
arrays in an .npz file (no pickle), so applying one needs neither torch nor scikit-learn."""

from __future__ import annotations

import json
import os
import time
from abc import abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar

import numpy as np
from pydantic import BaseModel, Field

from .helpers import Windower, eeg_channels
from .plugin import Analyzer, IncompatibleInput
from .types import Chunk, Report, StreamInfo, StreamSelector, Window
from .views import Axis, ViewSpec


def models_dir() -> Path:
    """Where pulled weights live (override with MUSE_MODELS_DIR)."""
    env = os.environ.get("MUSE_MODELS_DIR")
    return Path(env) if env else Path.home() / ".cache" / "muse-monitor" / "models"


@dataclass
class ProbeHead:
    """Standardize, then a linear layer: logistic (2 classes) or softmax (more)."""

    classes: list[str]
    mean: np.ndarray
    scale: np.ndarray
    coef: np.ndarray            # (1, d) for two classes, else (k, d)
    intercept: np.ndarray
    meta: dict[str, Any] = field(default_factory=dict)   # model, revision, channels, window_s, cv scores, ...

    def predict_proba(self, x: np.ndarray) -> np.ndarray:
        x = np.atleast_2d(x)
        z = (x - self.mean) / self.scale @ self.coef.T + self.intercept
        if len(self.classes) == 2 and z.shape[1] == 1:
            p = 1.0 / (1.0 + np.exp(-z[:, 0]))
            return np.stack([1.0 - p, p], axis=1)
        z = z - z.max(axis=1, keepdims=True)
        e = np.exp(z)
        return e / e.sum(axis=1, keepdims=True)

    def save(self, path: Path) -> None:
        with open(path, "wb") as f:      # np.savez would append ".npz" to other names
            np.savez(f, classes=np.array(self.classes, dtype=str), mean=self.mean, scale=self.scale,
                     coef=self.coef, intercept=self.intercept, meta=np.array(json.dumps(self.meta)))

    @classmethod
    def load(cls, path: str | Path) -> ProbeHead:
        with np.load(path, allow_pickle=False) as z:
            return cls([str(c) for c in z["classes"]], z["mean"], z["scale"], z["coef"], z["intercept"],
                       json.loads(str(z["meta"])))


class ModelAnalyzer(Analyzer):
    """Base class: windows the EEG, runs `preprocess` + `infer` in a worker thread, emits `embedding`
    (and `probs` when a head is configured). Subclasses set `embedding_dim` and implement load/preprocess/infer."""

    cpu_bound = True
    inputs = [StreamSelector(kind="eeg")]
    embedding_dim: ClassVar[int]

    class Config(BaseModel):
        weights: str = Field(description="Hugging Face repo id, or a local folder with the weights.")
        revision: str = Field(description="Exact weights version (commit hash), so results are reproducible.")
        window_s: float = Field(4.0, description="Seconds of EEG per model input.")
        step_s: float = Field(1.0, description="How often the model runs (seconds).")
        head: str | None = Field(None, description="Probe head (.npz) from `muse train-probe`. Empty: emit "
                                                   "embeddings only.")
        channels: list[str] | None = Field(None, description="Electrodes to use; empty = all except AUX inputs.")

    # -- weights ----------------------------------------------------------------------------------
    @classmethod
    def weights_dir(cls, config: Any) -> Path:
        local = Path(config.weights)
        if local.is_dir():
            return local
        return models_dir() / config.weights.replace("/", "--") / config.revision

    @classmethod
    def pull(cls, config: Any) -> Path:
        """Download the pinned weights (only `muse models pull` calls this)."""
        target = cls.weights_dir(config)
        if Path(config.weights).is_dir():
            return target
        from huggingface_hub import snapshot_download
        snapshot_download(config.weights, revision=config.revision, local_dir=target)
        return target

    @classmethod
    def pulled(cls, config: Any) -> bool:
        d = cls.weights_dir(config)
        return d.is_dir() and any(d.iterdir())

    # -- subclass API -----------------------------------------------------------------------------
    @abstractmethod
    def load(self, path: Path) -> None:
        """Load weights from `path` (runs in the worker thread)."""

    @abstractmethod
    def preprocess(self, w: Window, rate: float) -> Any:
        """Window (samples x selected channels, µV) -> model input."""

    @abstractmethod
    def infer(self, x: Any) -> np.ndarray:
        """Model input -> 1-D embedding of length `embedding_dim`."""

    # -- analyzer ---------------------------------------------------------------------------------
    def setup(self, inputs: dict[str, StreamInfo]) -> list[StreamInfo]:
        eeg = [s for s in inputs.values() if s.kind == "eeg"]
        if len(eeg) != 1:
            raise IncompatibleInput(f"expected one EEG stream, got {[s.id for s in eeg]}")
        self.src = eeg[0]
        try:
            self.idx = eeg_channels(self.src.channels, self.config.channels)
        except ValueError as exc:
            raise IncompatibleInput(str(exc)) from exc
        self.chans = [self.src.channels[i] for i in self.idx]
        if not self.pulled(self.config):        # fails this plugin only; the session goes on
            raise RuntimeError(f"weights not found in {self.weights_dir(self.config)}; "
                               f"run `muse models pull {self.name}`")
        self._loaded = False
        self.win = Windower(self.config.window_s, self.config.step_s, self.src.rate)
        self.windows, self.infer_s = 0, 0.0
        self.head = ProbeHead.load(self.config.head) if self.config.head else None
        rate = 1.0 / self.config.step_s
        meta = {"model": self.name, "weights": self.config.weights, "revision": self.config.revision,
                "channels": self.chans, "window_s": self.config.window_s}
        out = [StreamInfo("embedding", "derived", [f"e{i}" for i in range(self.embedding_dim)],
                          ["1"] * self.embedding_dim, rate, meta=meta)]
        if self.head is not None:
            self._check_head(self.head)
            self.class_sum = np.zeros(len(self.head.classes))
            out.append(StreamInfo("probs", "derived", self.head.classes, ["1"] * len(self.head.classes), rate,
                                  meta={**meta, "head": str(self.config.head)}))
        return out

    def _check_head(self, head: ProbeHead) -> None:
        m = head.meta
        problems = []
        if m.get("model") not in (None, self.name):
            problems.append(f"trained for {m['model']!r}")
        if m.get("revision") not in (None, self.config.revision):
            problems.append(f"trained on weights {m['revision'][:10]}")
        if m.get("channels") not in (None, self.chans):
            problems.append(f"trained on channels {m['channels']}, stream has {self.chans}")
        if m.get("window_s") not in (None, self.config.window_s):
            problems.append(f"trained with {m['window_s']} s windows")
        if head.mean.shape[-1] != self.embedding_dim:
            problems.append(f"expects {head.mean.shape[-1]} features, model gives {self.embedding_dim}")
        if problems:
            raise IncompatibleInput("probe head does not match: " + "; ".join(problems))

    def _run(self, w: Window) -> np.ndarray:
        if not self._loaded:                    # first window, in the worker thread: errors stay isolated
            self.load(self.weights_dir(self.config))
            self._loaded = True
        t0 = time.perf_counter()
        emb = np.asarray(self.infer(self.preprocess(w, self.src.rate)), dtype=np.float64).reshape(-1)
        self.infer_s += time.perf_counter() - t0
        return emb

    async def process(self, chunk: Chunk, gap: bool) -> None:
        if gap:
            self.win.reset()
        for w in self.win.push(chunk):
            emb = await self.ctx.run_blocking(self._run, Window(w.data[:, self.idx], w.t))
            self.windows += 1
            self.ctx.emit(Chunk.single("embedding", emb, w.t_end))
            if self.head is not None:
                p = self.head.predict_proba(emb)[0]
                self.class_sum += p
                self.ctx.emit(Chunk.single("probs", p, w.t_end))

    def views(self) -> list[ViewSpec]:
        if self.head is None:
            return []
        classes = ", ".join(self.head.classes)
        return [
            ViewSpec(id="probs", title="Probe output", type="bars", streams=["probs"],
                     description=f"How strongly the trained probe matches the current {self.config.window_s:g} s "
                                 f"of EEG to each of your labels ({classes}). It was trained on your own labelled "
                                 "sessions; it is a pattern match, not a measure of mental state.",
                     y=Axis(label="probability", min=0, max=1)),
            ViewSpec(id="probs_trend", title="Probe output over time", type="timeseries", streams=["probs"],
                     description="The same probabilities over the last 2 minutes.", window_s=120.0,
                     y=Axis(label="probability", min=0, max=1)),
        ]

    async def summarize(self) -> Report | None:
        if not self.windows:
            return None
        metrics: dict[str, float | int | str | None] = {
            "windows": self.windows, "ms_per_window": round(1000 * self.infer_s / self.windows, 1)}  # incl. load
        notes = [f"Weights {self.config.weights} @ {self.config.revision[:10]}."]
        if self.head is not None:
            for c, v in zip(self.head.classes, self.class_sum / self.windows):
                metrics[f"mean_p_{c}"] = round(float(v), 3)
            cv = self.head.meta.get("cv", {})
            if cv:
                notes.append(f"Head cross-validation: balanced accuracy {cv.get('model_mean', float('nan')):.2f} "
                             f"vs band-power baseline {cv.get('baseline_mean', float('nan')):.2f}.")
        return Report(title=f"Model: {self.name}", metrics=metrics, notes=notes)
