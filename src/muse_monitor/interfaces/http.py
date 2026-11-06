"""REST + WebSocket API (FastAPI) and the static web UI. A thin adapter over KernelFacade."""

from __future__ import annotations

import asyncio
import hmac
import io
import ipaddress
import json
import re
import secrets
import uuid
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastapi import Body, Depends, FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, Response
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from ..kernel import APP_VERSION, KernelError, KernelFacade
from ..sdk import DeviceNotFound

WEB_DIR = Path(__file__).parent / "web"


def is_loopback(host: str) -> bool:
    if host in ("localhost", ""):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def load_or_create_token(path: Path) -> tuple[str, bool]:
    """Returns (token, created)."""
    if path.is_file():
        return path.read_text(encoding="utf-8").strip(), False
    path.parent.mkdir(parents=True, exist_ok=True)
    token = secrets.token_urlsafe(24)
    path.write_text(token, encoding="utf-8")
    try:
        path.chmod(0o600)
    except OSError:
        pass
    return token, True


def cross_origin(origin: str | None, host: str | None) -> bool:
    """True if a browser sent this request from a page served elsewhere. Without a token, any site open in the
    user's browser could otherwise POST to (or open a WebSocket on) 127.0.0.1. Non-browser clients send no Origin."""
    if origin is None:
        return False
    return urlsplit(origin).netloc.lower() != (host or "").lower()


REPO_ID = re.compile(r"[A-Za-z0-9][\w.-]*/[\w.-]+")
REVISION = re.compile(r"[\w.-]{1,64}")


def check_model_options(options: dict[str, Any]) -> None:
    """Model options from HTTP clients may name Hugging Face weights but not files or folders on the server."""
    if "head" in options:
        raise HTTPException(422, "`head` cannot be set here")
    weights, revision = options.get("weights"), options.get("revision")
    if weights is not None and (not REPO_ID.fullmatch(str(weights)) or ".." in str(weights)):
        raise HTTPException(422, "`weights` must be a Hugging Face repo id (org/name), not a server path")
    check_revision(revision)


def check_revision(revision: Any) -> None:
    if revision is not None and (not REVISION.fullmatch(str(revision)) or ".." in str(revision)):
        raise HTTPException(422, "invalid `revision`")


def problem(status: int, title: str, detail: str, plugin: str | None = None) -> JSONResponse:
    body: dict[str, Any] = {"type": "about:blank", "title": title, "status": status, "detail": detail}
    if plugin:
        body["plugin"] = plugin
    return JSONResponse(body, status_code=status, media_type="application/problem+json")


def create_app(facade: KernelFacade, token: str | None = None) -> FastAPI:
    app = FastAPI(title="Muse Monitor", version=APP_VERSION, docs_url="/docs",
                  openapi_url="/api/v1/openapi.json")

    @app.middleware("http")
    async def revalidate_static(request: Request, call_next: Any) -> Response:
        """The UI has no build step or hashed file names, so browsers must revalidate (ETag) after upgrades.
        Requests that change something must come from this server's own pages (or from a non-browser client)."""
        if request.method not in ("GET", "HEAD", "OPTIONS") and cross_origin(request.headers.get("origin"),
                                                                              request.headers.get("host")):
            return problem(403, "Forbidden", "cross-origin request refused")
        response = await call_next(request)
        if not request.url.path.startswith("/api/"):
            response.headers.setdefault("Cache-Control", "no-cache")
        return response

    def authorized(header: str | None, query: str | None) -> bool:
        if not token:
            return True
        given = (header or "").removeprefix("Bearer ").strip() or (query or "")
        return hmac.compare_digest(given, token)

    async def auth(request: Request) -> None:
        if not authorized(request.headers.get("authorization"), request.query_params.get("token")):
            raise HTTPException(401, "missing or invalid bearer token")

    @app.exception_handler(KernelError)
    async def kernel_error(_: Request, exc: KernelError) -> JSONResponse:
        return problem(exc.status, type(exc).__name__, str(exc), exc.plugin)

    @app.exception_handler(DeviceNotFound)
    async def device_error(_: Request, exc: DeviceNotFound) -> JSONResponse:
        return problem(404, "DeviceNotFound", str(exc))

    @app.exception_handler(HTTPException)
    async def http_error(_: Request, exc: HTTPException) -> JSONResponse:
        return problem(exc.status_code, "HTTPError", str(exc.detail))

    deps = [Depends(auth)]
    P = "/api/v1"

    @app.get(f"{P}/health", dependencies=deps)
    async def health() -> dict[str, Any]:
        live = facade.live
        return {"status": "ok", "version": APP_VERSION,
                "live_session": live.info.id if live and not live.done.is_set() else None,
                "last_session": live.info.id if live else None,
                "plugins": facade.health("current") if live and not live.done.is_set() else {}}

    @app.get(f"{P}/plugins", dependencies=deps)
    async def plugins(kind: str | None = None) -> list[dict[str, Any]]:
        return facade.list_plugins(kind)

    @app.get(f"{P}/plugins/{{name}}", dependencies=deps)
    async def plugin(name: str) -> dict[str, Any]:
        return facade.plugin_info(name)

    @app.get(f"{P}/devices", dependencies=deps)
    async def devices(source: str = "openmuse") -> list[dict[str, Any]]:
        return await facade.discover_devices(source)

    @app.post(f"{P}/sessions", status_code=201, dependencies=deps)
    async def start(pipeline: dict[str, Any] = Body(...)) -> dict[str, Any]:
        return await facade.start_session(pipeline)

    @app.get(f"{P}/sessions", dependencies=deps)
    async def sessions(since: datetime | None = None, mode: str | None = "live",
                       label: str | None = None) -> list[dict[str, Any]]:
        return facade.list_sessions(since, None if mode == "all" else mode, label)

    @app.get(f"{P}/sessions/{{sid}}", dependencies=deps)
    async def session(sid: str) -> dict[str, Any]:
        return facade.get_session(sid)

    # -- session management: rename, delete, export several, import an archive ------------------------
    def scratch(name: str) -> Path:
        d = facade.data_dir / ".muse" / "tmp"
        d.mkdir(parents=True, exist_ok=True)
        return d / f"{uuid.uuid4().hex}-{name}"

    @app.post(f"{P}/sessions/export", dependencies=deps)
    async def export_sessions(body: dict[str, Any] = Body(...)) -> FileResponse:
        """Body: {"ids": [...], "format": "session" | "csv" | "edf"}. One file to download."""
        ids, fmt = list(body.get("ids") or []), str(body.get("format", "session"))
        if len(ids) == 1:
            name = f"{ids[0]}.muse.zip" if fmt == "session" else f"{ids[0]}.edf" if fmt == "edf" else f"{ids[0]}-csv.zip"
        else:
            name = f"muse-sessions-{len(ids)}-{fmt}.zip"
        out = await asyncio.to_thread(facade.export_sessions, ids, fmt, scratch(name))
        return FileResponse(out, filename=name, background=BackgroundTask(out.unlink, missing_ok=True))

    @app.post(f"{P}/sessions/import", dependencies=deps)
    async def import_sessions(request: Request) -> dict[str, Any]:
        """Body: a session archive (.zip) as the raw request body. Existing sessions are skipped."""
        from ..archive import MAX_IMPORT_BYTES
        path, size = scratch("import.zip"), 0
        try:
            with open(path, "wb") as f:
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > MAX_IMPORT_BYTES:
                        raise HTTPException(413, "archive too large")
                    f.write(chunk)
            if not size:
                raise HTTPException(422, "empty upload: send the archive as the request body")
            return await asyncio.to_thread(facade.import_sessions, path)
        finally:
            path.unlink(missing_ok=True)

    @app.patch(f"{P}/sessions/{{sid}}", dependencies=deps)
    async def rename_session(sid: str, body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Body: {"label": "..."} (empty or null clears it)."""
        return facade.rename_session(sid, body.get("label"))

    @app.delete(f"{P}/sessions/{{sid}}", dependencies=deps)
    async def delete_session(sid: str) -> dict[str, Any]:
        return await asyncio.to_thread(facade.delete_session, sid)

    @app.post(f"{P}/sessions/{{sid}}/replay", status_code=201, dependencies=deps)
    async def replay(sid: str, speed: float = 1.0,
                     body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        """Replay a recording as a live session (nothing recorded). Body: optional {"analyzers": [...]}."""
        return await facade.replay_session(sid, speed, body.get("analyzers"))

    @app.post(f"{P}/sessions/{{sid}}/stop", dependencies=deps)
    async def stop(sid: str) -> dict[str, Any]:
        return await facade.stop_session(sid)

    @app.post(f"{P}/sessions/{{sid}}/markers", dependencies=deps)
    async def marker(sid: str, body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        return await facade.add_marker(sid, str(body.get("label") or "marker"))

    @app.post(f"{P}/sessions/{{sid}}/calibrate", dependencies=deps)
    async def calibrate(sid: str, body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        return await facade.calibrate(sid, float(body.get("eyes_open_s", 60)), float(body.get("eyes_closed_s", 60)))

    @app.get(f"{P}/sessions/{{sid}}/views", dependencies=deps)
    async def views(sid: str) -> list[dict[str, Any]]:
        return facade.views(sid)

    @app.get(f"{P}/sessions/{{sid}}/streams", dependencies=deps)
    async def streams(sid: str) -> list[dict[str, Any]]:
        info = facade.get_session(sid)
        return facade.streams(info["parent"] if info["mode"] == "offline" else sid)

    @app.get(f"{P}/sessions/{{sid}}/streams/{{stream:path}}", dependencies=deps)
    async def stream(sid: str, stream: str, t0: float | None = Query(None, alias="from"),
                     t1: float | None = Query(None, alias="to"), format: str = "json",
                     max_points: int = 0) -> Any:
        d = await asyncio.to_thread(facade.read_stream, sid, stream, t0, t1, max_points)
        if format == "csv":
            lines = ["t," + ",".join(d["info"]["channels"])]
            lines += [f"{t}," + ",".join("" if v is None else str(v) for v in row) for t, row in zip(d["t"], d["data"])]
            return PlainTextResponse("\n".join(lines) + "\n", media_type="text/csv")
        return d

    @app.get(f"{P}/sessions/{{sid}}/events", dependencies=deps)
    async def events(sid: str, type: str | None = None, t0: float | None = Query(None, alias="from"),
                     t1: float | None = Query(None, alias="to")) -> list[dict[str, Any]]:
        return facade.events(sid, type, t0, t1)

    @app.post(f"{P}/sessions/{{sid}}/analyses", status_code=202, dependencies=deps)
    async def analyze(sid: str, pipeline: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
        return await facade.run_offline(sid, pipeline or None)

    @app.get(f"{P}/jobs/{{job_id}}", dependencies=deps)
    async def job(job_id: str) -> dict[str, Any]:
        return facade.get_job(job_id)

    @app.get(f"{P}/sessions/{{sid}}/reports", dependencies=deps)
    async def reports(sid: str) -> list[str]:
        return facade.reports(sid)

    @app.get(f"{P}/sessions/{{sid}}/reports/{{name}}", dependencies=deps)
    async def report(sid: str, name: str) -> FileResponse:
        return FileResponse(facade.report_path(sid, name), media_type="text/html")

    @app.post(f"{P}/sessions/{{sid}}/export", dependencies=deps)
    async def export(sid: str, format: str = "csv") -> Response:
        files = [Path(f) for f in await asyncio.to_thread(facade.export, sid, format, None)]
        if len(files) == 1:
            return FileResponse(files[0], filename=files[0].name)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for f in files:
                z.write(f, f.name)
        return Response(buf.getvalue(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{sid}-{format}.zip"'})

    @app.post(f"{P}/sessions/{{sid}}/repair", dependencies=deps)
    async def repair(sid: str) -> dict[str, Any]:
        return facade.repair(sid)

    @app.post(f"{P}/models/{{plugin}}/pull", dependencies=deps)
    async def pull_model(plugin: str, revision: str | None = None) -> dict[str, Any]:
        check_revision(revision)
        return await facade.pull_model(plugin, revision)

    @app.post(f"{P}/probes", dependencies=deps)
    async def train_probe(body: dict[str, Any] = Body(...)) -> dict[str, Any]:
        """Body: {sessions, plugin, label_from, name, options, min_usable}. The head is stored on the server as
        <data>/.muse/probes/<name>.npz; download it from GET /probes/{name}."""
        if "out" in body:
            raise HTTPException(422, "`out` is not accepted over HTTP: give a `name`, then download the head "
                                     "from /api/v1/probes/{name}")
        options = dict(body.get("options") or {})
        check_model_options(options)
        return await facade.train_probe(list(body.get("sessions") or []), str(body.get("plugin") or "cbramod"),
                                        str(body.get("label_from") or "markers"), options=options,
                                        min_usable=float(body.get("min_usable", 0.5)),
                                        name=str(body.get("name") or "head"))

    @app.get(f"{P}/probes/{{name}}", dependencies=deps)
    async def probe_file(name: str) -> FileResponse:
        path = facade.probe_path(name)
        return FileResponse(path, media_type="application/octet-stream", filename=path.name)

    @app.websocket(f"{P}/sessions/{{sid}}/live")
    async def live(ws: WebSocket, sid: str, streams: str | None = None, max_hz: float = 20.0) -> None:
        if not authorized(ws.headers.get("authorization"), ws.query_params.get("token")):
            await ws.close(code=4401)
            return
        if cross_origin(ws.headers.get("origin"), ws.headers.get("host")):     # browsers don't apply CORS to WS
            await ws.close(code=4403)
            return
        await ws.accept()
        wanted = [s for s in (streams or "").split(",") if s] or None
        try:
            it = facade.subscribe(sid, wanted, max_hz)

            async def receive() -> None:
                while True:
                    msg = json.loads(await ws.receive_text())
                    if msg.get("type") == "marker":
                        await facade.add_marker(sid, str(msg.get("label") or "marker"))

            reader = asyncio.create_task(receive())
            try:
                async for msg in it:
                    await ws.send_text(json.dumps(msg, default=str))
                    if reader.done():
                        break
            finally:
                reader.cancel()
            await ws.close()
        except KernelError as exc:
            await ws.send_text(json.dumps({"type": "error", "detail": str(exc)}))
            await ws.close(code=4404)
        except (WebSocketDisconnect, RuntimeError):
            pass

    @app.get("/favicon.ico", include_in_schema=False)
    async def favicon() -> FileResponse:
        return FileResponse(WEB_DIR / "favicon.svg", media_type="image/svg+xml")

    app.mount("/", StaticFiles(directory=WEB_DIR, html=True), name="web")
    return app
