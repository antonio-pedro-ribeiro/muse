"""CBraMod (Wang et al., ICLR 2025): pretrained EEG foundation model, via braindecode.

Input as in the paper: 200 Hz, 0.3-75 Hz band-pass, notch at mains, units of 100 µV, 1 s patches.
Output: the encoder's 200-dim features averaged over channels and patches, so any channel count works."""

from __future__ import annotations

import contextlib
import io
from fractions import Fraction
from pathlib import Path
from typing import Any

import numpy as np
from pydantic import Field

from muse_monitor.sdk import ModelAnalyzer, Window

REPO = "braindecode/cbramod-pretrained"
REVISION = "584cdc415913739a05d84bf0c1cb3db397764507"     # pinned 2026-11-06
MODEL_RATE = 200.0


class CBraMod(ModelAnalyzer):
    name = "cbramod"
    version = "1.1.0"            # 1.1: even (mirror) padding before zero-phase filtering each window
    summary = "CBraMod EEG foundation model -> 200-dim embeddings (+ probe head)"
    requires = ["torch", "braindecode", "safetensors", "huggingface_hub"]
    install_hint = "install the models extra: uv sync --extra models"
    embedding_dim = 200

    class Config(ModelAnalyzer.Config):
        weights: str = Field(REPO, description="Hugging Face repo id, or a local folder with the weights.")
        revision: str = Field(REVISION, description="Exact weights version (commit hash), so results are "
                                                    "reproducible.")
        notch_hz: float = Field(50.0, description="Mains frequency to remove (50 Hz in Europe, 60 Hz in the "
                                                  "Americas; 0 = off).")

    def load(self, path: Path) -> None:
        import torch
        from braindecode.models import CBraMod as Net
        self.torch = torch
        with contextlib.redirect_stdout(io.StringIO()):     # braindecode prints "Loading weights ..."
            self.net = Net.from_pretrained(str(path), return_encoder_output=True).eval()
        self._filters: dict[float, Any] = {}

    def _sos(self, rate: float) -> Any:
        if rate not in self._filters:
            from scipy.signal import butter, iirnotch, tf2sos
            nyq = rate / 2
            sos = butter(4, [0.3, min(75.0, 0.9 * nyq)], btype="bandpass", fs=rate, output="sos")
            if 0 < self.config.notch_hz < 0.95 * nyq:
                sos = np.vstack([sos, tf2sos(*iirnotch(self.config.notch_hz, 30.0, fs=rate))])
            self._filters[rate] = sos
        return self._filters[rate]

    def preprocess(self, w: Window, rate: float) -> np.ndarray:
        from scipy.signal import resample_poly, sosfiltfilt
        x = w.data.astype(np.float64).T                              # (channels, samples), µV
        x = x - x.mean(axis=1, keepdims=True)
        # The 0.3 Hz high-pass takes ~2 s to settle; mirrored padding up to 2 s keeps edge ringing out of a 4 s window
        x = sosfiltfilt(self._sos(rate), x, axis=1, padtype="even", padlen=min(int(2 * rate), x.shape[1] - 1))
        ratio = Fraction(MODEL_RATE / rate).limit_denominator(1000)
        if ratio != 1:
            x = resample_poly(x, ratio.numerator, ratio.denominator, axis=1)
        n = int(round(self.config.window_s * MODEL_RATE))
        x = x[:, :n] if x.shape[1] >= n else np.pad(x, ((0, 0), (0, n - x.shape[1])))
        return (x / 100.0).astype(np.float32)                        # model units: 100 µV

    def infer(self, x: np.ndarray) -> np.ndarray:
        with self.torch.inference_mode():
            out = self.net(self.torch.from_numpy(x[None]))           # (1, channels, patches, 200)
        return out[0].mean(dim=(0, 1)).numpy()
