"""`muse` command line. Works in-process, or against a running server with --server URL."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import re
import sys
import time
from collections import deque
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Annotated, Any, Optional

import typer
from rich.console import Console, Group
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ..kernel import ConfigError, KernelError, default_pipeline, load_pipeline, parse_pipeline
from ..kernel.config import PluginEntry, SourceEntry
from ..sdk import DeviceNotFound

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Monitor and analyze Muse EEG recordings.")
plugins_app = typer.Typer(no_args_is_help=True, help="Inspect installed plugins.")
sessions_app = typer.Typer(no_args_is_help=True, help="Recorded sessions.")
app.add_typer(plugins_app, name="plugins")
app.add_typer(sessions_app, name="sessions")
models_app = typer.Typer(no_args_is_help=True, help="Model weights.")
app.add_typer(models_app, name="models")
for _stream in (sys.stdout, sys.stderr):     # Windows pipes default to cp1252, which can't print "●"
    if hasattr(_stream, "reconfigure") and (_stream.encoding or "").lower() not in ("utf-8", "utf8"):
        _stream.reconfigure(encoding="utf-8", errors="replace")
console = Console()
err = Console(stderr=True)

JsonOpt = Annotated[bool, typer.Option("--json", help="Machine-readable output.")]
ServerOpt = Annotated[Optional[str], typer.Option("--server", envvar="MUSE_SERVER",
                                                  help="Use a running `muse serve` (http://host:port).")]
TokenOpt = Annotated[Optional[str], typer.Option("--token", envvar="MUSE_TOKEN", help="Bearer token for --server.")]


# -- plumbing -----------------------------------------------------------------------------------------
def facade(server: str | None, token: str | None = None) -> Any:
    if server:
        from .client import RemoteFacade
        return RemoteFacade(server, token)
    from ..kernel import KernelFacade
    return KernelFacade()


async def call(fn: Any, *args: Any, **kw: Any) -> Any:
    result = fn(*args, **kw)
    return await result if inspect.isawaitable(result) else result


def run(coro_fn: Any) -> Any:
    """Runs a coroutine and maps errors to the spec's exit codes."""
    try:
        return asyncio.run(coro_fn)
    except DeviceNotFound as exc:
        err.print(f"[red]device not found:[/] {exc}")
        raise typer.Exit(3) from exc
    except KernelError as exc:
        err.print(f"[red]error:[/] {exc}")
        raise typer.Exit(exc.exit_code) from exc
    except KeyboardInterrupt:
        err.print("interrupted")
        raise typer.Exit(130) from None


def emit(data: Any, as_json: bool, human: Any = None) -> None:
    if as_json or human is None:
        typer.echo(json.dumps(data, indent=2, default=str))
    else:
        human(data)


def parse_duration(text: str | None) -> float | None:
    if not text:
        return None
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([smhd]?)\s*", text)
    if not m:
        raise typer.BadParameter(f"invalid duration {text!r}; use e.g. 30s, 10m, 1h, 7d")
    return float(m.group(1)) * {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}[m.group(2)]


def parse_options(pairs: list[str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for p in pairs:
        if "=" not in p:
            raise typer.BadParameter(f"expected key=value, got {p!r}")
        k, v = p.split("=", 1)
        try:
            out[k.strip()] = json.loads(v)
        except json.JSONDecodeError:
            out[k.strip()] = v
    return out


def fmt_num(v: Any) -> str:
    if v is None:
        return "–"
    if isinstance(v, float):
        if v != v:
            return "–"
        return f"{v:.0f}" if abs(v) >= 1000 else f"{v:.1f}" if abs(v) >= 10 else f"{v:.3f}" if abs(v) < 1 else f"{v:.2f}"
    return str(v)


# -- plugins / devices / check --------------------------------------------------------------------------
@plugins_app.command("list")
def plugins_list(kind: Annotated[Optional[str], typer.Option(help="source | analyzer | sink")] = None,
                 json_out: JsonOpt = False, server: ServerOpt = None, token: TokenOpt = None) -> None:
    """List plugins with their status."""
    async def go() -> None:
        data = await call(facade(server, token).list_plugins, kind)
        def human(rows: list[dict[str, Any]]) -> None:
            t = Table("name", "kind", "version", "status", "summary")
            for p in rows:
                status = "[green]ok[/]" if p["status"] == "ok" else f"[yellow]{p['status']}[/]: {p['hint']}"
                t.add_row(p["name"], p["kind"], p["version"], status, p["summary"])
            console.print(t)
        emit(data, json_out, human)
    run(go())


@plugins_app.command("info")
def plugins_info(name: str, json_out: JsonOpt = False, server: ServerOpt = None, token: TokenOpt = None) -> None:
    """Show a plugin's manifest and config options."""
    async def go() -> None:
        p = await call(facade(server, token).plugin_info, name)
        def human(p: dict[str, Any]) -> None:
            console.print(f"[bold]{p['name']}[/] {p['version']} ({p['kind']}) — {p['summary']}")
            console.print(f"status: {p['status']} {p['hint']}  origin: {p['origin']}")
            t = Table("option", "type", "default")
            for k, s in (p["config_schema"].get("properties") or {}).items():
                typ = s.get("type") or "/".join(x.get("type", "?") for x in s.get("anyOf", []))
                t.add_row(k, typ, json.dumps(s["default"]) if "default" in s else "[red]required[/]")
            console.print(t)
        emit(p, json_out, human)
    run(go())


@app.command()
def devices(source: str = "openmuse", json_out: JsonOpt = False, server: ServerOpt = None,
            token: TokenOpt = None) -> None:
    """Scan for devices with a source plugin."""
    async def go() -> None:
        found = await call(facade(server, token).discover_devices, source)
        def human(rows: list[dict[str, Any]]) -> None:
            if not rows:
                console.print("No devices found. Is the headband on, and not connected to the phone app?")
                raise typer.Exit(3)
            for d in rows:
                rssi = f"  RSSI {d['rssi']} dBm" if d.get("rssi") is not None else ""
                console.print(f"  {d['name']:<16} {d['id']}{rssi}")
        emit(found, json_out, human)
    run(go())


@app.command(context_settings={"allow_extra_args": True, "ignore_unknown_options": True})
def check(ctx: typer.Context) -> None:
    """Connectivity test (same as `muse-check test`): stream briefly, report rates and contact."""
    from ..check import main as check_main
    raise typer.Exit(check_main(["test", *ctx.args]))


# -- live sessions --------------------------------------------------------------------------------------
class KeyReader:
    """Non-blocking single-key reads from the terminal."""

    def __init__(self) -> None:
        self.ok = sys.stdin.isatty()
        self._old = None
        if self.ok and os.name != "nt":
            import termios
            import tty
            self._old = termios.tcgetattr(sys.stdin)
            tty.setcbreak(sys.stdin.fileno())

    def read(self) -> str | None:
        if not self.ok:
            return None
        if os.name == "nt":
            import msvcrt
            return msvcrt.getwch() if msvcrt.kbhit() else None
        import select
        return sys.stdin.read(1) if select.select([sys.stdin], [], [], 0)[0] else None

    def close(self) -> None:
        if self._old is not None:
            import termios
            termios.tcsetattr(sys.stdin, termios.TCSADRAIN, self._old)


SPARK = "▁▂▃▄▅▆▇█"


def sparkline(values: list[float], width: int = 40) -> str:
    vals = [v for v in values if v is not None and v == v]
    if not vals:
        return ""
    if len(values) > width:
        step = len(values) / width
        values = [values[int(i * step)] for i in range(width)]
    lo, hi = min(vals), max(vals)
    span = hi - lo or 1.0
    return "".join(" " if v is None or v != v else SPARK[min(7, int((v - lo) / span * 7.999))] for v in values)


class Dashboard:
    """Renders ViewSpecs in the terminal: metric, bars, table, events, timeseries (sparklines)."""

    def __init__(self, info: dict[str, Any]) -> None:
        self.info = info
        self.views: list[dict[str, Any]] = []
        self.streams: dict[str, dict[str, Any]] = {}
        self.rows: dict[str, deque[tuple[float, list[Any]]]] = {}
        self.events: deque[dict[str, Any]] = deque(maxlen=50)
        self.health: dict[str, Any] = {}
        self.started = time.monotonic()
        self.calibration = ""

    def add(self, msg: dict[str, Any]) -> None:
        kind = msg["type"]
        if kind == "hello":
            self.views = msg["views"]
            self.streams = {s["id"]: s for s in msg["streams"]}
        elif kind == "chunk":
            q = self.rows.setdefault(msg["stream"], deque(maxlen=400))
            q.extend(zip(msg["t"], msg["data"]))
        elif kind == "event":
            e = msg["event"]
            self.events.append(e)
            if e["type"] == "calibration.eyes_open":
                self.calibration = "[bold yellow]Calibration: keep your eyes OPEN and relax[/]"
            elif e["type"] == "calibration.eyes_closed":
                self.calibration = "[bold yellow]Calibration: CLOSE your eyes and relax[/]"
            elif e["type"] in ("calibration.end", "baseline.ready", "baseline.failed"):
                self.calibration = "" if e["type"] != "baseline.failed" else "[red]Calibration failed[/]"
        elif kind == "health":
            self.health = msg["plugins"]

    def _last(self, stream: str) -> list[Any] | None:
        q = self.rows.get(stream)
        return q[-1][1] if q else None

    def _channels(self, stream: str) -> list[str]:
        return self.streams.get(stream, {}).get("channels", [])

    def _view(self, v: dict[str, Any]) -> Any:
        s = v["streams"][0] if v["streams"] else ""
        row = self._last(s) if s else None
        y = v.get("y") or {}
        if v["type"] == "events":
            prefixes = tuple(v.get("event_types") or [""])
            evs = [e for e in self.events if e["type"].startswith(prefixes)][-6:]
            return Text("\n".join(f"{datetime.fromtimestamp(e['t']):%H:%M:%S} {e['type']}" for e in reversed(evs))
                        or "no events", style="dim" if not evs else "")
        if v["type"] == "text":
            prefixes = tuple(v.get("event_types") or [""])
            last = next((e for e in reversed(self.events) if e["type"].startswith(prefixes)
                         and (e.get("data") or {}).get("text")), None)
            if last is None:
                return Text("nothing to describe yet", style="dim")
            return Text(f"{datetime.fromtimestamp(last['t']):%H:%M:%S}  {last['data']['text']}")
        if row is None:
            return Text("waiting for data…", style="dim")
        if v["type"] == "electrodes":           # terminal version of the head map: symbol + status per electrode
            look = {0: ("●", "green", "good"), 1: ("○", "red", "poor contact"), 2: ("◐", "yellow", "noisy"),
                    3: ("◌", "dim", "no signal")}
            out = Text()
            for name, x in zip(self._channels(s), row):
                if name.upper().startswith("AUX"):
                    continue
                sym, style, word = look.get(int(x) if x == x else -1, ("?", "dim", "no data"))
                out.append(f"{sym} ", style=style)
                out.append(f"{name} {word}   ")
            return out
        if v["type"] == "metric":
            pct = y.get("unit") == "%"
            return Text("  ".join((f"{round(x * 100)}%" if pct and x is not None else fmt_num(x)) for x in row),
                        style="bold")
        if v["type"] in ("bars", "bands", "table"):
            t = Table.grid(padding=(0, 1))
            hi = y.get("max") or max([x for x in row if x is not None and x == x] or [1.0]) or 1.0
            lo = y.get("min") or 0.0
            for name, x in zip(self._channels(s), row):
                bar = ""
                if v["type"] in ("bars", "bands") and x is not None and x == x:
                    frac = max(0.0, min(1.0, (x - lo) / ((hi - lo) or 1)))
                    bar = "█" * int(frac * 24)
                style = ""
                for th in v.get("thresholds") or []:
                    if (th.get("min") is None or x >= th["min"]) and (th.get("max") is None or x < th["max"]):
                        style = {"ok": "green", "warn": "yellow", "bad": "red"}[th["level"]]
                        break
                t.add_row(name, Text(bar, style=style or "cyan"), fmt_num(x))
            return t
        if v["type"] == "timeseries":
            q = list(self.rows.get(s, []))
            t = Table.grid(padding=(0, 1))
            for i, name in enumerate(self._channels(s)[:6]):
                vals = [r[1][i] for r in q]
                t.add_row(name, sparkline(vals), fmt_num(vals[-1] if vals else None))
            return t
        return Text(f"({v['type']} view: see the web UI)", style="dim")

    def render(self) -> Any:
        elapsed = time.monotonic() - self.started
        battery = self._last("battery")
        chips = " ".join(f"[{'green' if h['status'] == 'ok' else 'yellow' if h['status'] == 'degraded' else 'red'}]●[/]"
                         f"{name}" for name, h in self.health.items())
        head = (f"[bold]{self.info.get('label') or 'Live session'}[/]  {self.info['id']}  "
                f"{int(elapsed // 60):02d}:{int(elapsed % 60):02d}"
                + (f"  battery {battery[0]:.0f}%" if battery else "") + f"\n{chips}\n"
                "[dim]keys: m = marker, q = stop[/]" + (f"\n{self.calibration}" if self.calibration else ""))
        panels = [Panel(self._view(v), title=v["title"], title_align="left") for v in self.views
                  if v["type"] != "spectrum"]
        return Group(Panel(head), *panels)


async def live_session(f: Any, pipeline: dict[str, Any], duration: float | None, calibrate: bool,
                       json_out: bool) -> dict[str, Any]:
    info = await call(f.start_session, pipeline)
    sid = info["id"]
    if calibrate:
        await call(f.calibrate, sid)
    keys = KeyReader() if not json_out else None
    deadline = time.monotonic() + duration if duration else None
    dash = Dashboard(info)
    stopping = False
    try:
        with Live(dash.render(), console=console, refresh_per_second=4, transient=False,
                  auto_refresh=False) if not json_out else _Null() as live:
            last = 0.0
            async for msg in f.subscribe(sid, None, 4.0):
                dash.add(msg)
                if msg["type"] == "end":
                    break
                key = keys.read() if keys else None
                if key in ("m", "M"):
                    await call(f.add_marker, sid, "marker")
                if not stopping and (key in ("q", "Q") or (deadline and time.monotonic() >= deadline)):
                    stopping = True
                    await call(f.stop_session, sid)
                now = time.monotonic()
                if now - last >= 0.25:
                    live.update(dash.render(), refresh=True)
                    last = now
    except (asyncio.CancelledError, KeyboardInterrupt):
        await call(f.stop_session, sid)
    finally:
        if keys:
            keys.close()
    return await call(f.get_session, sid)


class _Null:
    def __enter__(self) -> _Null:
        return self

    def __exit__(self, *a: Any) -> None:
        pass

    def update(self, *a: Any, **k: Any) -> None:
        pass


def print_session(d: dict[str, Any]) -> None:
    console.print(f"[bold]{d.get('label') or 'Session'}[/] {d['id']} — {d['status']}"
                  + (f" ([red]{d['error']}[/])" if d.get("error") else ""))
    console.print(f"started {d['started_at']}  ended {d.get('ended_at') or '–'}  mode {d['mode']}"
                  + (f"  parent {d['parent']}" if d.get("parent") else ""))
    if d.get("device"):
        dev = d["device"]
        console.print(f"device {dev.get('name') or dev.get('model')} {dev.get('id')} fw {dev.get('firmware') or '–'}")
    t = Table("stream", "samples", "rate", "nominal", "gaps", "missing", "duplicates")
    for sid, s in (d.get("stats") or {}).items():
        if "/" not in sid:
            t.add_row(sid, str(s["samples"]), fmt_num(s["effective_rate"]), fmt_num(s["nominal_rate"]),
                      str(s["gaps"]), str(s["missing_samples"]), str(s["duplicates"]))
    console.print(t)
    for r in d.get("reports") or []:
        print_report(r)


def print_report(r: dict[str, Any]) -> None:
    """A plugin's report: metrics table, then its notes as paragraphs (for `describe`, the text itself)."""
    t = Table(title=r["title"], show_header=False, title_justify="left")
    for k, v in r["metrics"].items():
        t.add_row(k, fmt_num(v))
    console.print(t)
    for note in r.get("notes") or []:
        console.print(Text(note), style="dim" if not r["title"].endswith("Session description") else "")
        console.print()


@app.command("run")
def run_cmd(
    config: Annotated[Optional[Path], typer.Option("-c", "--config", help="Pipeline TOML.")] = None,
    source: Annotated[Optional[str], typer.Option(help="Source plugin (default openmuse).")] = None,
    analyzer: Annotated[Optional[list[str]], typer.Option("--analyzer", "-a", help="Analyzer (repeatable).")] = None,
    option: Annotated[Optional[list[str]], typer.Option("--option", "-o", help="Source option key=value.")] = None,
    label: Annotated[Optional[str], typer.Option(help="Session label.")] = None,
    no_record: Annotated[bool, typer.Option("--no-record", help="Do not record to disk.")] = False,
    duration: Annotated[Optional[str], typer.Option(help="Stop after e.g. 30s, 10m, 1h.")] = None,
    calibrate: Annotated[bool, typer.Option(help="Start with 60 s eyes open + 60 s eyes closed.")] = False,
    json_out: JsonOpt = False, server: ServerOpt = None, token: TokenOpt = None,
) -> None:
    """Live session with a terminal dashboard."""
    pipe = load_pipeline(config) if config else default_pipeline(source or "openmuse", record=not no_record)
    updates: dict[str, Any] = {}
    if source and config:
        updates["source"] = SourceEntry(plugin=source)
    if option:
        base = updates.get("source", pipe.source)
        updates["source"] = SourceEntry(**{**base.model_dump(), **parse_options(option)})
    if analyzer:
        updates["analyzer"] = [PluginEntry(plugin=a) for a in analyzer]
    if no_record:
        updates["sink"] = [s for s in pipe.sink if s.plugin != "recorder"]
    if label:
        updates["session"] = pipe.session.model_copy(update={"label": label})
    pipe = parse_pipeline(pipe.model_copy(update=updates).model_dump())
    seconds = parse_duration(duration)

    async def go() -> None:
        f = facade(server, token)
        result = await live_session(f, pipe.model_dump(mode="json"), seconds, calibrate, json_out)
        emit(result, json_out, print_session)
        if result["status"] == "failed":
            raise typer.Exit(1)
    run(go())


# -- sessions ---------------------------------------------------------------------------------------------
@sessions_app.command("list")
def sessions_list(since: Annotated[Optional[str], typer.Option(help="e.g. 7d, 12h")] = None,
                  mode: Annotated[str, typer.Option(help="live | offline | all")] = "live",
                  label: Optional[str] = None, json_out: JsonOpt = False, server: ServerOpt = None,
                  token: TokenOpt = None) -> None:
    """List sessions, newest first."""
    secs = parse_duration(since)
    cutoff = datetime.now(UTC) - timedelta(seconds=secs) if secs else None

    async def go() -> None:
        rows = await call(facade(server, token).list_sessions, cutoff, None if mode == "all" else mode, label)
        def human(rows: list[dict[str, Any]]) -> None:
            t = Table("id", "started", "label", "duration", "status", "gaps", "mode")
            for d in rows:
                start = datetime.fromisoformat(d["started_at"])
                end = datetime.fromisoformat(d["ended_at"]) if d.get("ended_at") else None
                dur = f"{(end - start).total_seconds() / 60:.1f} min" if end else "–"
                gaps = sum(s.get("gaps", 0) for k, s in (d.get("stats") or {}).items() if "/" not in k)
                t.add_row(d["id"], start.astimezone().strftime("%Y-%m-%d %H:%M"), d.get("label") or "",
                          dur, d["status"], str(gaps), d["mode"])
            console.print(t if rows else "No sessions.")
        emit(rows, json_out, human)
    run(go())


@sessions_app.command("show")
def sessions_show(session_id: str, json_out: JsonOpt = False, server: ServerOpt = None,
                  token: TokenOpt = None) -> None:
    """Show a session's metadata, stream statistics and reports."""
    async def go() -> None:
        emit(await call(facade(server, token).get_session, session_id), json_out, print_session)
    run(go())


@sessions_app.command("export")
def sessions_export(
    session_ids: Annotated[list[str], typer.Argument(help="Session ids.")],
    format: Annotated[str, typer.Option("--format", help="csv | edf | session (an archive that "
                                                         "`muse sessions import` reads)")] = "csv",
    out: Annotated[Optional[Path], typer.Option(help="Output file or folder.")] = None,
    json_out: JsonOpt = False, server: ServerOpt = None, token: TokenOpt = None,
) -> None:
    """Export sessions to CSV, EDF+ or a session archive. Several sessions go into one zip."""
    async def go() -> None:
        f = facade(server, token)
        try:
            if len(session_ids) == 1 and format in ("csv", "edf"):
                files = await call(f.export, session_ids[0], format, out)
            else:
                one = session_ids[0]
                name = (f"{one}.muse.zip" if format == "session" else f"{one}-{format}.zip") if len(session_ids) == 1 \
                    else f"muse-sessions-{len(session_ids)}-{format}.zip"
                target = (out / name if out and out.is_dir() else out) or Path(name)
                files = [str(await call(f.export_sessions, session_ids, format, target))]
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        emit(files, json_out, lambda fs: [console.print(f"wrote {x}") for x in fs])
    run(go())


@sessions_app.command("rename")
def sessions_rename(session_id: str, label: Annotated[str, typer.Argument(help='New label; "" clears it.')],
                    json_out: JsonOpt = False, server: ServerOpt = None, token: TokenOpt = None) -> None:
    """Set a session's label."""
    async def go() -> None:
        d = await call(facade(server, token).rename_session, session_id, label)
        emit(d, json_out, lambda x: console.print(f"{x['id']}: {x.get('label') or '(no label)'}"))
    run(go())


@sessions_app.command("delete")
def sessions_delete(
    session_ids: Annotated[list[str], typer.Argument(help="Session ids.")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Do not ask for confirmation.")] = False,
    json_out: JsonOpt = False, server: ServerOpt = None, token: TokenOpt = None,
) -> None:
    """Delete sessions and everything derived from them (cannot be undone)."""
    if not yes and not typer.confirm(f"Permanently delete {len(session_ids)} session(s)?"):
        raise typer.Exit(1)

    async def go() -> None:
        f = facade(server, token)
        done = [await call(f.delete_session, sid) for sid in session_ids]
        emit(done, json_out, lambda ds: [console.print(f"deleted {d['deleted']}") for d in ds])
    run(go())


@sessions_app.command("import")
def sessions_import(path: Annotated[Path, typer.Argument(help="Session archive (.zip) from `muse sessions export "
                                                              "--format session` or the web UI.")],
                    json_out: JsonOpt = False, server: ServerOpt = None, token: TokenOpt = None) -> None:
    """Import sessions from an archive. Sessions that already exist are skipped, never overwritten."""
    if not path.is_file():
        raise typer.BadParameter(f"no such file: {path}")

    async def go() -> None:
        r = await call(facade(server, token).import_sessions, path)

        def human(d: dict[str, Any]) -> None:
            for x in d["imported"]:
                console.print(f"imported {x['id']}  {x.get('label') or ''}")
            for x in d["skipped"]:
                console.print(f"[yellow]skipped[/] {x['session']}: {x['reason']}")
            if not d["imported"] and not d["skipped"]:
                console.print("nothing to import")
        emit(r, json_out, human)
    run(go())


@sessions_app.command("repair")
def sessions_repair(session_id: str, json_out: JsonOpt = False, server: ServerOpt = None,
                    token: TokenOpt = None) -> None:
    """Finalize a session after a crash (compacts row-group files, marks it stopped)."""
    async def go() -> None:
        emit(await call(facade(server, token).repair, session_id), json_out,
             lambda r: console.print(f"compacted: {', '.join(r['compacted_streams']) or 'nothing to do'}"))
    run(go())


# -- models ---------------------------------------------------------------------------------------------------
@models_app.command("pull")
def models_pull(plugin: str,
                revision: Annotated[Optional[str], typer.Option(help="Weights version; default: the plugin's pin.")] = None,
                json_out: JsonOpt = False, server: ServerOpt = None, token: TokenOpt = None) -> None:
    """Download a model plugin's weights (sessions never download anything)."""
    async def go() -> None:
        with console.status(f"downloading {plugin} weights…"):
            r = await call(facade(server, token).pull_model, plugin, revision)
        emit(r, json_out, lambda d: console.print(f"{d['weights']} @ {d['revision'][:10]} -> {d['path']}"))
    run(go())


@app.command("train-probe")
def train_probe(
    sessions: Annotated[list[str], typer.Argument(help="Labelled session ids.")],
    plugin: Annotated[str, typer.Option(help="Model plugin.")] = "cbramod",
    label_from: Annotated[str, typer.Option(help="markers | calibration")] = "markers",
    out: Annotated[Path, typer.Option(help="Head file to write (.npz).")] = Path("head.npz"),
    window_s: Annotated[Optional[float], typer.Option(help="Seconds per window (model default if empty).")] = None,
    json_out: JsonOpt = False, server: ServerOpt = None, token: TokenOpt = None,
) -> None:
    """Train a linear probe on model embeddings and compare it with band power (session-held-out CV)."""
    options = {"window_s": window_s} if window_s else {}

    async def go() -> None:
        with console.status("embedding sessions and cross-validating…"):
            r = await call(facade(server, token).train_probe, sessions, plugin, label_from,
                           str(out if server else out.resolve()), options)

        def human(d: dict[str, Any]) -> None:
            cv = d["cv"]
            console.print(f"windows per label: {d['windows']}   CV: {cv['scheme']}   chance {cv['chance']:.2f}")
            t = Table("fold", "test windows", f"{plugin} probe", "band power", title="balanced accuracy",
                      title_justify="left")
            for i, f in enumerate(cv["folds"]):
                t.add_row(f["held_out"] or str(i + 1), str(f["test_windows"]), f"{f['model']:.3f}",
                          f"{f['baseline']:.3f}")
            t.add_row("[bold]mean[/]", "", f"[bold]{cv['model_mean']:.3f}[/] ± {cv['model_std']:.3f}",
                      f"[bold]{cv['baseline_mean']:.3f}[/] ± {cv['baseline_std']:.3f}")
            console.print(t)
            console.print(("[green]" if cv["model_beats_baseline"] else "[yellow]") + d["verdict"] + "[/]")
            console.print(f"head written to {d['head']}")
            console.print(f"To use it, add head = '{d['head']}' to the {plugin} analyzer in a pipeline file.")
        emit(r, json_out, human)
    run(go())


# -- offline analysis ---------------------------------------------------------------------------------------
@app.command()
def analyze(
    session_id: str,
    config: Annotated[Optional[Path], typer.Option("-c", "--config", help="Pipeline TOML (analyzers used).")] = None,
    analyzer: Annotated[Optional[list[str]], typer.Option("--analyzer", "-a")] = None,
    report: Annotated[Optional[Path], typer.Option(help="Also copy the HTML report here.")] = None,
    json_out: JsonOpt = False, server: ServerOpt = None, token: TokenOpt = None,
) -> None:
    """Re-run analyzers over a recording (replay) and write derived data + an HTML report."""
    pipeline: dict[str, Any] | None = None
    if config or analyzer:
        base = load_pipeline(config) if config else default_pipeline("replay", record=False)
        if analyzer:
            base = base.model_copy(update={"analyzer": [PluginEntry(plugin=a) for a in analyzer]})
        pipeline = base.model_dump(mode="json")

    async def go() -> None:
        f = facade(server, token)
        job = await call(f.run_offline, session_id, pipeline)
        with console.status("analyzing…") as status:
            while job["status"] == "running":
                await asyncio.sleep(0.3)
                job = await call(f.get_job, job["id"])
                status.update(f"analyzing… {job['progress'] * 100:.0f}%")
        if job["status"] == "failed":
            raise KernelError(job["error"] or "analysis failed")
        name = job["result"].get("report")
        if name and report and not server:
            from ..kernel import KernelFacade
            assert isinstance(f, KernelFacade)
            report.write_bytes(f.report_path(session_id, name).read_bytes())
        def human(j: dict[str, Any]) -> None:
            for r in j["result"].get("reports", []):
                print_report(r)
            if name:
                where = j["result"].get("report_path") or name
                console.print(f"report: {where}" + (f" (copied to {report})" if report and not server else ""))
        emit(job, json_out, human)
    run(go())


# -- server -------------------------------------------------------------------------------------------------
@app.command()
def serve(host: str = "127.0.0.1", port: int = 8765,
          token_file: Annotated[Optional[Path], typer.Option(help="Bearer token file (non-local binds).")] = None
          ) -> None:
    """Run the REST/WebSocket API and the web UI."""
    import uvicorn

    from ..kernel import KernelFacade
    from .http import create_app, is_loopback, load_or_create_token
    f = KernelFacade()
    token = None
    if not is_loopback(host) or token_file:
        path = token_file or f.data_dir / ".muse" / "token"
        try:
            token, created = load_or_create_token(path)
        except OSError as exc:
            err.print(f"[red]refusing to start:[/] binding to {host} needs a token, and {path} is unusable: {exc}")
            raise typer.Exit(2) from exc
        if created:
            console.print(f"[bold]API token[/] (stored in {path}, shown once): {token}")
        else:
            console.print(f"Token required (from {path}).")
    console.print(f"Web UI and API on http://{host}:{port}  (docs: /docs)")
    uvicorn.run(create_app(f, token), host=host, port=port, log_level="warning")


def main() -> None:
    try:
        app()
    except ConfigError as exc:
        err.print(f"[red]config error:[/] {exc}")
        sys.exit(2)


if __name__ == "__main__":
    main()
