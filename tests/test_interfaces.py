"""HTTP API, WebSocket and CLI."""

import io
import json
import time
import zipfile
from pathlib import Path

from fastapi.testclient import TestClient
from typer.testing import CliRunner

from muse_monitor.interfaces.cli import app as cli
from muse_monitor.interfaces.http import create_app, is_loopback
from muse_monitor.kernel import KernelFacade

SYNTH = {"source": {"plugin": "synthetic", "chunk_s": 0.0625}, "analyzer": [{"plugin": "quality"},
         {"plugin": "bandpower"}], "sink": [{"plugin": "recorder"}]}


def test_api_live_session_roundtrip(data_dir: Path):
    with TestClient(create_app(KernelFacade(data_dir))) as client:
        _roundtrip(client)


def _roundtrip(client: TestClient) -> None:
    assert {p["name"] for p in client.get("/api/v1/plugins").json()} >= {"synthetic", "recorder", "bandpower"}
    r = client.post("/api/v1/sessions", json=SYNTH)
    assert r.status_code == 201, r.text
    sid = r.json()["id"]
    assert client.post("/api/v1/sessions", json=SYNTH).status_code == 409        # one live session at a time

    with client.websocket_connect(f"/api/v1/sessions/{sid}/live?max_hz=20") as ws:
        hello = ws.receive_json()
        assert hello["type"] == "hello" and any(v["id"] == "bandpower/mean_rel" for v in hello["views"])
        ws.send_json({"type": "marker", "label": "from ws"})
        kinds = set()
        deadline = time.time() + 10
        while time.time() < deadline and not {"chunk", "health"} <= kinds:
            kinds.add(ws.receive_json()["type"])
        assert {"chunk", "health"} <= kinds
    assert client.post(f"/api/v1/sessions/{sid}/markers", json={"label": "rest"}).json()["type"] == "marker"
    time.sleep(2.5)
    stopped = client.post(f"/api/v1/sessions/{sid}/stop").json()
    assert stopped["status"] == "stopped" and not stopped["active"]

    assert [s["id"] for s in client.get("/api/v1/sessions").json()] == [sid]
    streams = {s["id"] for s in client.get(f"/api/v1/sessions/{sid}/streams").json()}
    assert streams == {"eeg", "acc", "battery"}
    d = client.get(f"/api/v1/sessions/{sid}/streams/eeg?max_points=100").json()
    assert len(d["t"]) <= 100 and d["info"]["channels"] == ["TP9", "AF7", "AF8", "TP10"]
    labels = [e["data"].get("label") for e in client.get(f"/api/v1/sessions/{sid}/events?type=marker").json()]
    assert labels == ["from ws", "rest"]

    job = client.post(f"/api/v1/sessions/{sid}/analyses", json={}).json()
    for _ in range(100):
        j = client.get(f"/api/v1/jobs/{job['id']}").json()
        if j["status"] != "running":
            break
        time.sleep(0.1)
    assert j["status"] == "done", j
    assert client.get(f"/api/v1/sessions/{sid}/streams/bandpower/mean_rel").status_code == 200
    report = client.get(f"/api/v1/sessions/{sid}/reports").json()[0]
    assert "MuseRenderers" in client.get(f"/api/v1/sessions/{sid}/reports/{report}").text

    z = client.post(f"/api/v1/sessions/{sid}/export?format=csv")
    assert z.status_code == 200 and "eeg.csv" in zipfile.ZipFile(io.BytesIO(z.content)).namelist()
    assert client.get("/api/v1/sessions/..%2F..%2Fetc").status_code == 404
    assert client.get("/").status_code == 200 and "Muse Monitor" in client.get("/").text
    assert client.get("/api/v1/openapi.json").status_code == 200


def test_api_problem_json_and_token(data_dir: Path):
    with TestClient(create_app(KernelFacade(data_dir), token="secret")) as client:
        _token(client)


def _token(client: TestClient) -> None:
    assert client.get("/api/v1/plugins").status_code == 401
    ok = client.get("/api/v1/plugins", headers={"Authorization": "Bearer secret"})
    assert ok.status_code == 200
    r = client.get("/api/v1/sessions/NOPE", headers={"Authorization": "Bearer secret"})
    assert r.status_code == 404 and r.headers["content-type"].startswith("application/problem+json")
    assert r.json()["title"] == "NotFound"


def test_cross_origin_writes_and_websockets_are_refused(data_dir: Path):
    """Without a token, a page on another site must not drive the local server through the user's browser."""
    import pytest
    from starlette.websockets import WebSocketDisconnect
    with TestClient(create_app(KernelFacade(data_dir))) as client:
        evil = {"Origin": "http://evil.example"}
        r = client.post("/api/v1/sessions/NOPE/stop", headers=evil)
        assert r.status_code == 403 and r.json()["detail"] == "cross-origin request refused"
        assert client.post("/api/v1/sessions/NOPE/stop", headers={"Origin": "null"}).status_code == 403
        assert client.post("/api/v1/sessions/NOPE/stop", headers={"Origin": "http://testserver"}).status_code == 404
        assert client.post("/api/v1/sessions/NOPE/stop").status_code == 404          # CLI / scripts: no Origin
        assert client.get("/api/v1/plugins", headers=evil).status_code == 200         # reads: the browser blocks them
        with pytest.raises(WebSocketDisconnect) as exc:
            with client.websocket_connect("/api/v1/sessions/current/live", headers=evil) as ws:
                ws.receive_text()
        assert exc.value.code == 4403


def test_probe_api_cannot_write_or_read_server_paths(data_dir: Path, tmp_path: Path):
    import pytest

    from muse_monitor.kernel import ConfigError
    f = KernelFacade(data_dir)
    with TestClient(create_app(f)) as client:
        target = tmp_path / "pwned.npz"
        r = client.post("/api/v1/probes", json={"sessions": ["X"], "out": str(target)})
        assert r.status_code == 422 and "`out` is not accepted" in r.json()["detail"]
        for options in ({"weights": str(tmp_path)}, {"weights": "../../etc"}, {"revision": ".."},
                        {"head": str(target)}):
            assert client.post("/api/v1/probes", json={"sessions": ["X"], "options": options}).status_code == 422
        assert client.post("/api/v1/probes", json={"sessions": ["X"], "name": "../x"}).status_code == 422
        assert client.post("/api/v1/models/cbramod/pull", params={"revision": "../.."}).status_code == 422
        assert client.get("/api/v1/probes/nothing").status_code == 404
        assert client.get("/api/v1/probes/..%2Fsession").status_code in (404, 422)
        assert not target.exists()
    assert f.probe_path("head", must_exist=False) == f.data_dir / ".muse" / "probes" / "head.npz"
    with pytest.raises(ConfigError):
        f.probe_path("../../x", must_exist=False)


def test_loopback_detection():
    assert is_loopback("127.0.0.1") and is_loopback("localhost") and not is_loopback("0.0.0.0")


def test_cli_json_commands(data_dir: Path, tmp_path: Path):
    runner = CliRunner()
    r = runner.invoke(cli, ["plugins", "list", "--json"])
    assert r.exit_code == 0 and any(p["name"] == "openmuse" for p in json.loads(r.stdout))
    r = runner.invoke(cli, ["run", "--source", "synthetic", "--duration", "3s", "--json",
                            "-o", "chunk_s=0.125", "--label", "cli test"])
    assert r.exit_code == 0, r.output
    info = json.loads(r.stdout)
    assert info["status"] == "stopped" and info["label"] == "cli test"
    r = runner.invoke(cli, ["sessions", "list", "--json"])
    assert [s["id"] for s in json.loads(r.stdout)] == [info["id"]]
    r = runner.invoke(cli, ["analyze", info["id"], "--json"])
    assert r.exit_code == 0 and json.loads(r.stdout)["status"] == "done"
    bad = tmp_path / "bad.toml"
    bad.write_text('[source]\nplugin = "synthetic"\nnope = 1\n', encoding="utf-8")
    assert runner.invoke(cli, ["run", "-c", str(bad), "--json"]).exit_code == 2
    assert runner.invoke(cli, ["sessions", "show", "MISSING"]).exit_code == 1


def test_api_replay_recorded_session_live(data_dir: Path):
    """A recording plays back as a live session: its analyzers plus describe, its markers, nothing recorded."""
    rec = {"source": {"plugin": "synthetic", "realtime": False, "duration_s": 70, "start_time": 1.7e9,
                      "script": [[0.0, "eyes open", {"20": 8.0}], [35.0, "eyes closed", {"10": 25.0}]]},
           "analyzer": [{"plugin": "quality"}, {"plugin": "bandpower"}], "sink": [{"plugin": "recorder"}]}
    with TestClient(create_app(KernelFacade(data_dir))) as client:
        sid = client.post("/api/v1/sessions", json=rec).json()["id"]
        while client.get(f"/api/v1/sessions/{sid}").json()["active"]:
            time.sleep(0.1)
        before = sorted(p.name for p in data_dir.iterdir())
        assert client.post(f"/api/v1/sessions/{sid}/replay?speed=0").status_code == 422      # speed > 0
        r = client.post(f"/api/v1/sessions/{sid}/replay?speed=100")
        assert r.status_code == 201, r.text
        rep = r.json()
        assert rep["label"] == "Replay of " + sid + " (100×)" and rep["pipeline"]["source"]["plugin"] == "replay"
        assert [a["plugin"] for a in rep["pipeline"]["analyzer"]] == ["quality", "bandpower", "describe"]
        assert client.post(f"/api/v1/sessions/{sid}/replay").status_code == 409             # one live session
        while client.get(f"/api/v1/sessions/{rep['id']}").json()["active"]:
            time.sleep(0.1)
        done = client.get(f"/api/v1/sessions/{rep['id']}").json()
        assert done["status"] == "stopped" and done["stats"]["eeg"]["samples"] == 70 * 256
        text = next(r for r in done["reports"] if r["title"].endswith("Session description"))
        assert any(n.startswith("“eyes closed”") for n in text["notes"])                    # markers replayed
        assert sorted(p.name for p in data_dir.iterdir()) == before                          # nothing recorded
        assert client.post("/api/v1/sessions/NOSUCHSESSION/replay").status_code == 404
