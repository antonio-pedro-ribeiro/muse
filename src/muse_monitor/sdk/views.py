"""Declarative visualizations. Analyzers describe what to show; interfaces decide how."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ViewType = Literal["timeseries", "spectrum", "bars", "metric", "table", "events", "text", "electrodes"]
# Where interfaces place a view: is the signal good / what is it doing now / over time / the raw signal / what happened
Section = Literal["quality", "now", "trends", "signal", "events", ""]


class Axis(BaseModel):
    label: str = ""
    unit: str = ""
    min: float | None = None
    max: float | None = None
    log: bool = False


class Threshold(BaseModel):
    """A coloured value band, e.g. quality ok/bad."""

    min: float | None = None
    max: float | None = None
    level: Literal["ok", "warn", "bad"] = "ok"
    label: str = ""


class ViewSpec(BaseModel):
    id: str
    title: str
    type: ViewType
    description: str = ""                                   # plain-language "what does this show", for info icons
    streams: list[str] = Field(default_factory=list)
    channels: list[str] | None = None
    window_s: float = 10.0
    y: Axis | None = None
    thresholds: list[Threshold] = Field(default_factory=list)
    event_types: list[str] = Field(default_factory=list)   # "events"/"text" views: type prefixes to show
                                                            # ("text": events whose data.text is a paragraph)
    section: Section = ""                                   # page area; "" = interface decides (by type)
    plugin: str = ""                                        # set by the kernel
