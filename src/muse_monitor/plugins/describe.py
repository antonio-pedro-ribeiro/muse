"""Plain-language descriptions of the measurements: band power (vs. the personal baseline when there is one),
signal quality, artifacts and, when validated, a trained probe.

Rules (see docs): describe measurements, never mental states; say nothing about rhythms when the signal is
poor; name artifacts before rhythms; a probe is only quoted if it beat band power on held-out sessions.
Every sentence comes from a template, and the numbers behind it travel with the event (data.facts)."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from typing import Any

import numpy as np
from pydantic import BaseModel, Field

from muse_monitor.sdk import (Analyzer, Chunk, Event, IncompatibleInput, Report, StreamInfo, StreamSelector,
                              ViewSpec)

BAND_NAMES = {"delta": "delta", "theta": "theta", "alpha": "alpha", "beta": "beta", "gamma": "gamma"}
RHYTHMS = ("theta", "alpha", "beta")          # bands worth interpreting on a headband
UP, DOWN = 1.3, 1 / 1.3                       # what counts as a notable change (×)

# What a change is "commonly seen with". Patterns from the literature, phrased as such: never a state.
HEDGES = {
    ("alpha", +1): "a pattern commonly seen with closed eyes or relaxed wakefulness",
    ("alpha", -1): "a pattern commonly seen with open eyes, visual attention or mental effort",
    ("theta", +1): "a pattern seen with drowsiness and in some meditation practices",
    ("beta", +1): "a pattern that can accompany mental effort, but also muscle tension",
    ("beta", -1): "a pattern often seen as muscles relax",
}

# Wording that must never appear: diagnoses, conditions, or claims about what someone feels or thinks.
BANNED = re.compile(r"diagnos|disorder|disease|anxi|depress|adhd|autis|epilep|seizure|dementia|stress|"
                    r"\bcalm\b|focused|concentrat|emotion|\bmood|happy|\bsad\b|\bfeel|you are|you were|"
                    r"your (?:mind|brain) (?:is|was)|thinking about|abnormal|healthy|unhealthy",
                    re.IGNORECASE)


def check_wording(text: str) -> str | None:
    """Returns the offending fragment, or None if the text is acceptable."""
    m = BANNED.search(text)
    return m.group(0) if m else None


def _pct(x: float) -> str:
    return f"{100 * x:.0f}%"


def _clock(seconds: float) -> str:
    s = max(0, int(round(seconds)))
    return f"{s // 3600}:{s // 60 % 60:02d}:{s % 60:02d}" if s >= 3600 else f"{s // 60:02d}:{s % 60:02d}"


@dataclass
class Facts:
    seconds: float
    usable: float | None = None                     # share of electrode readings that were usable
    rel: dict[str, float] = field(default_factory=dict)        # mean relative power per band
    earlier_rel: dict[str, float] = field(default_factory=dict)  # same, before this period (no baseline case)
    norm: dict[str, float] = field(default_factory=dict)       # × eyes-open baseline
    blinks: int = 0
    jaw: int = 0
    motion_s: float = 0.0
    context: str = ""                               # marker label / calibration condition covering the period
    probe: dict[str, float] = field(default_factory=dict)      # share of windows each label was the best match
    probe_validation: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        def clean(v: Any) -> Any:
            if isinstance(v, float):
                return None if math.isnan(v) else round(v, 4)
            if isinstance(v, dict):
                return {k: clean(x) for k, x in v.items()}
            return v
        return {k: clean(v) for k, v in self.__dict__.items() if v not in (None, {}, "")}


def compose(f: Facts, min_usable: float = 0.5) -> str:
    """Facts -> a short paragraph. Pure function: everything it says is in `f`."""
    out: list[str] = []
    lead = f"During “{f.context}”: " if f.context else ""
    if f.usable is not None and f.usable < min_usable:
        return (f"{lead}Signal quality too low to describe this period: {_pct(f.usable)} of electrode readings "
                f"were usable. Check that the sensors touch the skin.")
    if not f.rel:
        return f"{lead}No band-power estimates for this period."
    minutes = max(f.seconds / 60, 1e-9)
    blink_rate, motion = f.blinks / minutes, f.motion_s / max(f.seconds, 1e-9)

    # rhythms: against the personal baseline when there is one, else against earlier in the session
    notable: list[tuple[str, int, float]] = []
    if f.norm:
        parts = [f"{BAND_NAMES[b]} {f.norm[b]:.1f}×" for b in ("delta", *RHYTHMS, "gamma") if b in f.norm]
        out.append(f"{lead}Compared with your eyes-open baseline: {', '.join(parts)}.")
        notable = [(b, +1 if f.norm[b] >= UP else -1, f.norm[b]) for b in RHYTHMS
                   if b in f.norm and (f.norm[b] >= UP or f.norm[b] <= DOWN)]
    else:
        parts = [f"{BAND_NAMES[b]} {_pct(v)}" for b, v in f.rel.items()]
        out.append(f"{lead}Share of power by band: {', '.join(parts)}.")
        for b in RHYTHMS:
            if b in f.rel and b in f.earlier_rel and f.earlier_rel[b] > 0:
                r = f.rel[b] / f.earlier_rel[b]
                if r >= UP or r <= DOWN:
                    notable.append((b, +1 if r >= UP else -1, r))
    if notable:
        if not f.norm and any(sign > 0 for _, sign, _ in notable):
            notable = [n for n in notable if n[1] > 0]      # shares sum to 1: a rise pushes the others down
        b, sign, r = max(notable, key=lambda x: abs(math.log(x[2])))
        if f.norm:
            what = f"The {b} {'increase' if sign > 0 else 'decrease'}"
        else:
            what = (f"The {b} share was {'higher' if sign > 0 else 'lower'} than earlier in the session "
                    f"({_pct(f.rel[b])} vs {_pct(f.earlier_rel[b])}); this")
        hedge = HEDGES.get((b, sign))
        out.append(f"{what} is {hedge}." if hedge else f"{what} stands out.")
        if b == "beta" and sign > 0 and (f.jaw or motion > 0.1):
            out.append("Jaw clenching or movement in this period may account for part of the beta.")

    # artifacts, and what they do to the low and high bands
    moving = "little movement" if motion < 0.05 else f"movement {_pct(motion)} of the time"
    jaw = f"{f.jaw} jaw-clench episode{'s' if f.jaw != 1 else ''}"
    out.append(f"{blink_rate:.0f} blinks/min, {jaw}, {moving}.")
    delta_big = (f.norm.get("delta", 0) >= UP) if f.norm else f.rel.get("delta", 0) >= 0.4
    if delta_big and (blink_rate > 20 or motion > 0.1):
        out.append("Much of the low-frequency (delta) power here likely comes from blinks and movement, "
                   "not brain activity.")
    gamma_big = (f.norm.get("gamma", 0) >= UP) if f.norm else f.rel.get("gamma", 0) >= 0.15
    if gamma_big:
        out.append("Gamma on a headband mostly reflects muscle activity (jaw, forehead).")

    # a trained probe, only when it proved itself on held-out sessions
    if f.probe and f.probe_validation:
        label, share = max(f.probe.items(), key=lambda kv: kv[1])
        v = f.probe_validation
        out.append(f"Your probe’s closest label was “{label}” in {_pct(share)} of windows (held-out accuracy "
                   f"{v['model_mean']:.2f} vs {v['baseline_mean']:.2f} for band power).")
    if f.usable is not None and f.usable < 0.8:
        out.append(f"Only {_pct(f.usable)} of electrode readings were usable, so treat these numbers with care.")
    return " ".join(out)


class Describe(Analyzer):
    name = "describe"
    version = "1.0.0"
    summary = "Plain-language description of band power, signal quality, artifacts (and a validated probe)"
    inputs = [StreamSelector(kind="derived", optional=True), StreamSelector(kind="mask", optional=True)]

    class Config(BaseModel):
        every_s: float = Field(30.0, description="Seconds covered by each live description.")
        min_usable: float = Field(0.5, description="Below this share of usable electrode readings, describe "
                                                   "only the signal quality.")
        bandpower: str = Field("bandpower", description="Instance id of the band-power analyzer to read.")
        quality: str = Field("quality", description="Instance id of the quality analyzer to read.")
        model: str = Field("cbramod", description="Instance id of a model analyzer with a probe head (optional).")
        probe_min_margin: float = Field(0.05, description="A probe is quoted only if its held-out accuracy beat "
                                                          "band power by at least this much, in most sessions.")

    def setup(self, inputs: dict[str, StreamInfo]) -> list[StreamInfo]:
        c = self.config
        self.rel_id, self.norm_id = f"{c.bandpower}/mean_rel", f"{c.bandpower}/norm"
        self.usable_id, self.probs_id = f"{c.quality}/usable", f"{c.model}/probs"
        if self.rel_id not in inputs:
            raise IncompatibleInput(f"needs {self.rel_id}: list `{c.bandpower}` before `describe`")
        self.bands = list(inputs[self.rel_id].channels)
        self.has_norm = self.norm_id in inputs
        self.probe_labels: list[str] = []
        self.validation: dict[str, Any] | None = None
        self.probe_note = ""
        if self.probs_id in inputs:
            info = inputs[self.probs_id]
            self.probe_labels = list(info.channels)
            v = info.meta.get("validation") or {}
            ok = (str(v.get("scheme", "")).startswith("session")
                  and (v.get("model_mean") or 0) - (v.get("baseline_mean") or 0) >= c.probe_min_margin
                  and v.get("folds", 0) > 0 and v.get("model_fold_wins", 0) * 2 > v["folds"])
            self.validation = v if ok else None
            if not ok:
                self.probe_note = ("The probe was not used in these descriptions: on held-out sessions it did not "
                                   f"beat band power by {c.probe_min_margin:.2f} in most sessions"
                                   + (f" ({v['model_mean']:.2f} vs {v['baseline_mean']:.2f})."
                                      if v.get("model_mean") is not None else "."))
        self.rows: dict[str, list[tuple[float, np.ndarray]]] = {k: [] for k in
                                                                 (self.rel_id, self.norm_id, self.usable_id,
                                                                  self.probs_id)}
        self.artifacts: list[tuple[str, float, float]] = []     # type, t, duration
        self.markers: list[tuple[float, str]] = []
        self.calibration: list[tuple[float, float, str]] = []
        self.t_first: float | None = None
        self.next_end: float | None = None
        self.periods = 0
        self.withheld = 0
        return []

    # -- inputs -----------------------------------------------------------------------------------
    async def on_event(self, event: Event) -> None:
        if event.type.startswith("artifact."):
            self.artifacts.append((event.type, event.t, event.duration))
        elif event.type == "marker":
            self.markers.append((event.t, str(event.data.get("label", "")).strip()))
        elif event.type in ("calibration.eyes_open", "calibration.eyes_closed"):
            self.calibration.append((event.t, event.t + event.duration,
                                     "calibration, eyes " + event.type.rsplit("_", 1)[1]))

    async def process(self, chunk: Chunk, gap: bool) -> None:
        if chunk.stream not in self.rows:
            return
        for t, row in zip(chunk.t, chunk.data):
            self.rows[chunk.stream].append((float(t), np.asarray(row, dtype=float)))
        if chunk.stream != self.rel_id:
            return
        t = float(chunk.t[-1])
        if self.t_first is None:
            self.t_first = float(chunk.t[0])
            self.next_end = self.t_first + self.config.every_s
        lag = 2.0                                   # let slower inputs (quality, model) catch up
        while self.next_end is not None and t >= self.next_end + lag:
            start = self.next_end - self.config.every_s
            self._emit(start, self.next_end)
            self.next_end += self.config.every_s

    # -- facts ------------------------------------------------------------------------------------
    def _mean(self, sid: str, t0: float, t1: float) -> np.ndarray | None:
        rows = [r for t, r in self.rows.get(sid, []) if t0 <= t <= t1]
        if not rows:
            return None
        with np.errstate(all="ignore"):
            m = np.nanmean(np.vstack(rows), axis=0)
        return None if np.isnan(m).all() else m

    def _context(self, t0: float, t1: float) -> str:
        for a, b, label in self.calibration:
            if a <= t0 + 1 and t1 <= b + 1:
                return label
        active = [lab for t, lab in self.markers if t <= t0]
        inside = [lab for t, lab in self.markers if t0 < t < t1]
        label = active[-1] if active else ""
        return label if label and label != "-" and not inside else ""

    def facts(self, t0: float, t1: float, context: str | None = None) -> Facts:
        f = Facts(seconds=t1 - t0, context=self._context(t0, t1) if context is None else context)
        u = self._mean(self.usable_id, t0, t1)
        f.usable = float(u[0]) if u is not None else None
        rel = self._mean(self.rel_id, t0, t1)
        if rel is not None:
            f.rel = {b: float(v) for b, v in zip(self.bands, rel) if np.isfinite(v)}
        if self.t_first is not None and t0 - self.t_first >= 30:
            early = self._mean(self.rel_id, self.t_first, t0)
            if early is not None:
                f.earlier_rel = {b: float(v) for b, v in zip(self.bands, early) if np.isfinite(v)}
        norm = self._mean(self.norm_id, t0, t1)
        if norm is not None:
            f.norm = {b: float(v) for b, v in zip(self.bands, norm) if np.isfinite(v)}
        for kind, t, d in self.artifacts:
            if t0 <= t < t1:
                f.blinks += kind == "artifact.blink"
                f.jaw += kind == "artifact.jaw"
                f.motion_s += d if kind == "artifact.motion" else 0.0
        if self.validation is not None:
            probs = [r for t, r in self.rows[self.probs_id] if t0 <= t <= t1]
            if probs:
                best = np.argmax(np.vstack(probs), axis=1)
                f.probe = {lab: float(np.mean(best == i)) for i, lab in enumerate(self.probe_labels)}
                f.probe_validation = self.validation
        return f

    def text(self, f: Facts) -> str:
        text = compose(f, self.config.min_usable)
        bad = check_wording(text)
        if bad:                                     # a template bug; never show it
            self.withheld += 1
            self.ctx.log.error("description withheld, wording check failed on %r: %s", bad, text)
            return "Description withheld: it failed the wording check (please report this)."
        return text

    def _emit(self, t0: float, t1: float) -> None:
        f = self.facts(t0, t1)
        self.periods += 1
        self.ctx.emit(Event("description", t0, duration=t1 - t0, data={"text": self.text(f), "facts": f.to_dict()}))

    # -- outputs ----------------------------------------------------------------------------------
    def segments(self, t_end: float) -> list[tuple[float, float, str]]:
        segs = [(a, min(b, t_end), label) for a, b, label in self.calibration if a < t_end]
        marks = sorted(self.markers)
        for (t, label), nxt in zip(marks, [m[0] for m in marks[1:]] + [t_end]):
            if label and label != "-" and nxt - t >= 10:
                segs.append((t, nxt, label))
        return sorted(segs)

    async def summarize(self) -> Report | None:
        if self.t_first is None:
            return None
        t_end = self.rows[self.rel_id][-1][0]
        start = min([self.t_first] + [t for t, _ in self.markers] + [a for a, _, _ in self.calibration])
        notes = [f"Whole session ({_clock(t_end - start)}): "
                 + self.text(self.facts(self.t_first, t_end, context=""))]
        for a, b, label in self.segments(t_end):
            f = self.facts(a, b, context="")
            notes.append(f"“{label}” ({_clock(a - start)}–{_clock(b - start)}): {self.text(f)}")
        if not self.has_norm:
            notes.append("Run Calibrate (2 min) to compare future sessions with your own eyes-open baseline.")
        if self.probe_note:
            notes.append(self.probe_note)
        notes.append("These are measurements from a consumer EEG headband, described in plain language. They are "
                     "not a medical assessment and do not describe states of mind.")
        whole = self.facts(self.t_first, t_end, context="")
        metrics: dict[str, float | int | str | None] = {"descriptions": self.periods}
        if whole.usable is not None:
            metrics["usable_pct"] = round(100 * whole.usable, 1)
        if self.withheld:
            metrics["withheld"] = self.withheld
        return Report(title="Session description", metrics=metrics, notes=notes)

    def views(self) -> list[ViewSpec]:
        return [ViewSpec(
            id="description", title="Session description", type="text", event_types=["description"], section="now",
            description=f"Every {self.config.every_s:g} s, a plain-language summary of the measurements: signal "
                        "quality, how power is spread over the frequency bands (compared with your eyes-open "
                        "baseline once you have calibrated), blinks, jaw clenching and movement. It describes "
                        "the signal, not states of mind, and is not a medical assessment.")]
