"""RemoteFacade: the KernelFacade API over HTTP/WebSocket, so the CLI can drive a running `muse serve`."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any

import httpx

from ..kernel.errors import Busy, ConfigError, KernelError, NotFound

_ERRORS = {404: NotFound, 409: Busy, 422: ConfigError}


class RemoteFacade:
    def __init__(self, url: str, token: str | None = None) -> None:
        self.url = url.rstrip("/")
        self.token = token
        headers = {"Authorization": f"Bearer {token}"} if token else {}
        self.http = httpx.AsyncClient(base_url=f"{self.url}/api/v1", headers=headers, timeout=60.0)

    async def _req(self, method: str, path: str, **kw: Any) -> Any:
        try:
            r = await self.http.request(method, path, **kw)
        except httpx.ConnectError as exc:
            raise KernelError(f"cannot reach {self.url}: {exc}") from exc
        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except ValueError:
                detail = r.text
            raise _ERRORS.get(r.status_code, KernelError)(f"{r.status_code}: {detail}")
        return r

    async def _json(self, method: str, path: str, **kw: Any) -> Any:
        return (await self._req(method, path, **kw)).json()

    async def list_plugins(self, kind: str | None = None) -> list[dict[str, Any]]:
        return await self._json("GET", "/plugins", params={"kind": kind} if kind else None)

    async def plugin_info(self, name: str) -> dict[str, Any]:
        return await self._json("GET", f"/plugins/{name}")

    async def discover_devices(self, source: str = "openmuse", config: dict[str, Any] | None = None
                               ) -> list[dict[str, Any]]:
        return await self._json("GET", "/devices", params={"source": source}, timeout=120.0)

    async def start_session(self, pipeline: dict[str, Any]) -> dict[str, Any]:
        return await self._json("POST", "/sessions", json=pipeline, timeout=120.0)

    async def stop_session(self, sid: str) -> dict[str, Any]:
        return await self._json("POST", f"/sessions/{sid}/stop", timeout=120.0)

    async def get_session(self, sid: str) -> dict[str, Any]:
        return await self._json("GET", f"/sessions/{sid}")

    async def list_sessions(self, since: datetime | None = None, mode: str | None = "live",
                            label: str | None = None) -> list[dict[str, Any]]:
        params = {k: v for k, v in {"since": since.isoformat() if since else None, "mode": mode or "all",
                                    "label": label}.items() if v}
        return await self._json("GET", "/sessions", params=params)

    async def views(self, sid: str) -> list[dict[str, Any]]:
        return await self._json("GET", f"/sessions/{sid}/views")

    async def add_marker(self, sid: str, label: str) -> dict[str, Any]:
        return await self._json("POST", f"/sessions/{sid}/markers", json={"label": label})

    async def calibrate(self, sid: str, eyes_open_s: float = 60, eyes_closed_s: float = 60) -> dict[str, Any]:
        return await self._json("POST", f"/sessions/{sid}/calibrate",
                                json={"eyes_open_s": eyes_open_s, "eyes_closed_s": eyes_closed_s})

    async def subscribe(self, sid: str, streams: list[str] | None = None, max_hz: float = 20.0
                        ) -> AsyncIterator[dict[str, Any]]:
        import websockets
        ws_url = self.url.replace("http://", "ws://").replace("https://", "wss://")
        q = f"max_hz={max_hz}" + (f"&streams={','.join(streams)}" if streams else "")
        q += f"&token={self.token}" if self.token else ""
        async with websockets.connect(f"{ws_url}/api/v1/sessions/{sid}/live?{q}", max_size=None) as ws:
            async for raw in ws:
                msg = json.loads(raw)
                if msg.get("type") == "error":
                    raise NotFound(msg.get("detail", "subscription failed"))
                yield msg

    async def run_offline(self, sid: str, pipeline: dict[str, Any] | None = None, report: bool = True,
                          speed: float = 0.0) -> dict[str, Any]:
        return await self._json("POST", f"/sessions/{sid}/analyses", json=pipeline or {})

    async def get_job(self, job_id: str) -> dict[str, Any]:
        return await self._json("GET", f"/jobs/{job_id}")

    async def pull_model(self, plugin: str, revision: str | None = None) -> dict[str, Any]:
        return await self._json("POST", f"/models/{plugin}/pull", params={"revision": revision} if revision else None,
                                timeout=1800.0)

    async def train_probe(self, sessions: list[str], plugin: str = "cbramod", label_from: str = "markers",
                          out: str = "head.npz", options: dict[str, Any] | None = None) -> dict[str, Any]:
        body = {"sessions": sessions, "plugin": plugin, "label_from": label_from, "out": out, "options": options}
        return await self._json("POST", "/probes", json=body, timeout=3600.0)

    async def export(self, sid: str, fmt: str, out: Path | None = None) -> list[str]:
        r = await self._req("POST", f"/sessions/{sid}/export", params={"format": fmt}, timeout=600.0)
        disp = r.headers.get("content-disposition", "")
        name = disp.split("filename=")[-1].strip('"') if "filename=" in disp else f"{sid}.{fmt}"
        path = out or Path(name)
        path.write_bytes(r.content)
        return [str(path)]

    async def repair(self, sid: str) -> dict[str, Any]:
        return await self._json("POST", f"/sessions/{sid}/repair")

    async def close(self) -> None:
        await self.http.aclose()
