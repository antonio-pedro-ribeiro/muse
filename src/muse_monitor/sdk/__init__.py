"""Public plugin SDK. Plugins import only from here."""

from .helpers import (DEFAULT_BANDS, MASK_FLAT, MASK_LABELS, MASK_NOISY, MASK_OK, MASK_POOR_CONTACT, Aligner,
                      QualityMask, Windower, band_powers, eeg_channels, welch_psd)
from .plugin import (API_VERSION, Analyzer, Context, DeviceNotFound, IncompatibleInput, Plugin, Sink, Source,
                     SourceDisconnected)
from .types import (Chunk, DeviceInfo, Event, Health, Report, SessionInfo, StreamInfo, StreamSelector,
                    Window)
from .views import Axis, Threshold, ViewSpec

# Approximate MNE standard_1005 positions (metres, head frame) for Muse electrodes.
MUSE_POSITIONS: dict[str, tuple[float, float, float]] = {
    "TP9": (-0.0856, -0.0465, -0.0457),
    "AF7": (-0.0548, 0.0686, -0.0047),
    "AF8": (0.0559, 0.0684, -0.0049),
    "TP10": (0.0861, -0.0470, -0.0457),
    "FPz": (0.0, 0.0886, 0.0012),
}

__all__ = [
    "API_VERSION", "Analyzer", "Aligner", "Axis", "Chunk", "Context", "DeviceInfo", "DeviceNotFound", "Event", "Health",
    "IncompatibleInput", "MASK_FLAT", "MASK_LABELS", "MASK_NOISY", "MASK_OK", "MASK_POOR_CONTACT",
    "MUSE_POSITIONS", "Plugin", "QualityMask", "Report", "SessionInfo", "Sink", "Source",
    "SourceDisconnected", "StreamInfo", "StreamSelector", "Threshold", "ViewSpec", "Window", "Windower",
    "DEFAULT_BANDS", "band_powers", "eeg_channels", "welch_psd",
]
