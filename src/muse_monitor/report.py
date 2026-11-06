"""Self-contained HTML reports: plugin summaries + the session's views, drawn by the web renderers."""

from __future__ import annotations

import html
import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .kernel.session import Session

WEB = Path(__file__).parent / "interfaces" / "web"


def _clean(x: Any) -> Any:
    if isinstance(x, float) and not math.isfinite(x):
        return None
    if isinstance(x, list):
        return [_clean(v) for v in x]
    return x


def build_report_html(session: Session) -> str:
    info = session.info
    streams: dict[str, Any] = {}
    for sid, rows in session.snapshot.rows.items():
        if sid in session.streams and rows:
            streams[sid] = {"info": session.streams[sid].to_dict(), "t": [r[0] for r in rows],
                            "data": [_clean(r[1]) for r in rows]}
    events = [e.to_dict() for e in session.snapshot.events
              if not e.type.startswith(("kernel.", "stream.gap"))]
    views = [v for v in info.views if all(s in streams for s in v["streams"]) or v["type"] == "events"]
    payload = json.dumps({"streams": streams, "events": events, "views": views}, default=str)

    sections = []
    for r in info.reports:
        rows = "".join(f"<tr><th>{html.escape(k)}</th><td>{html.escape(str(v))}</td></tr>" for k, v in r.metrics.items())
        notes = "".join(f"<p class='muted'>{html.escape(n)}</p>" for n in r.notes)
        sections.append(f"<div class='card'><h2>{html.escape(r.title)}</h2><table class='kv'>{rows}</table>{notes}</div>")
    title = f"Report · {info.label or info.parent or info.id}"
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{html.escape(title)}</title><style>{(WEB / "style.css").read_text(encoding="utf-8")}</style></head>
<body><header><span class="brand">Muse Monitor report</span></header><main>
<h1>{html.escape(info.label or "Session")} <span class="muted">{html.escape(info.parent or "")}</span></h1>
<p class="muted">Analysis {html.escape(info.id)} · generated {datetime.now(UTC):%Y-%m-%d %H:%M UTC} ·
plugins {html.escape(", ".join(f"{k} ({v})" for k, v in info.plugins.items()))}</p>
<p class="muted">Measurements from a consumer EEG headband. Not a medical assessment.</p>
<div class="grid">{"".join(sections)}</div><div class="grid" id="views"></div>
</main>
<script>{(WEB / "renderers.js").read_text(encoding="utf-8")}</script>
<script>
const DATA = {payload};
const store = {{ streams: DATA.streams, events: DATA.events }};
for (const s of Object.values(DATA.streams)) if (s.t.length) {{
  store.tStart = Math.min(store.tStart ?? Infinity, s.t[0]); store.tEnd = Math.max(store.tEnd ?? -Infinity, s.t[s.t.length - 1]);
}}
const root = document.getElementById("views");
const cards = DATA.views.map((v) => {{
  const card = document.createElement("div"); card.className = "card";
  const h = MuseRenderers.heading("h2", v.title, v.description); const body = document.createElement("div");
  card.append(h, body); root.append(card); return [v, body];
}});
const draw = () => cards.forEach(([v, body]) => MuseRenderers.render(body, {{ ...v, window_s: 1e9 }}, store));
draw(); window.addEventListener("resize", draw);
</script></body></html>"""


def write_report(session_dir: Path, session: Session) -> Path:
    out = session_dir / "reports" / f"{datetime.now(UTC):%Y-%m-%dT%H-%M-%SZ}.html"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(build_report_html(session), encoding="utf-8")
    return out
