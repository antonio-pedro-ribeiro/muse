"""Session management: rename, delete, export (archive / CSV / EDF+, several at once) and import."""

import asyncio
import io
import json
import zipfile
from pathlib import Path

import numpy as np
import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from muse_monitor.interfaces.cli import app as cli
from muse_monitor.interfaces.http import create_app
from muse_monitor.kernel import Busy, ConfigError, KernelFacade, NotFound
from muse_monitor.kernel.config import default_pipeline
from muse_monitor.sdk import storage


def record(f: KernelFacade, label: str, seconds: float = 4, seed: int = 0) -> str:
    pipe = default_pipeline("synthetic", realtime=False, duration_s=seconds, start_time=1_700_000_000.0 + seed * 1e4,
                            seed=seed)
    pipe.session.label = label

    async def go():
        info = await f.start_session(pipe)
        await f.add_marker(info["id"], "go")
        return (await f.wait_session(info["id"]))["id"]
    return asyncio.run(go())


def test_rename_delete_and_archive_round_trip(data_dir: Path, tmp_path: Path):
    f = KernelFacade(data_dir)
    a, b = record(f, "first", seed=1), record(f, "second", seed=2)
    assert f.rename_session(a, "  morning  ")["label"] == "morning"
    assert f.rename_session(b, "")["label"] is None                              # empty clears
    with pytest.raises(ConfigError):
        f.rename_session(a, "x" * 201)
    _, _, _, eeg_before = storage.read_stream(f.session_dir(a), "eeg")

    archive = f.export_sessions([a, b], "session", tmp_path / "both.muse.zip")
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
    assert f"{a}/session.json" in names and f"{b}/raw/eeg.parquet" in names and "MUSE-ARCHIVE.json" in names

    assert f.delete_session(a) == {"deleted": a}
    assert not (data_dir / a).exists() and [s["id"] for s in f.list_sessions()] == [b]
    with pytest.raises(NotFound):
        f.delete_session(a)

    result = f.import_sessions(archive)
    assert [x["id"] for x in result["imported"]] == [a] and result["imported"][0]["label"] == "morning"
    assert result["skipped"] == [{"session": b, "reason": "already exists here"}]          # never overwritten
    _, _, _, eeg_after = storage.read_stream(f.session_dir(a), "eeg")
    assert np.array_equal(eeg_before, eeg_after)
    assert any(e["type"] == "marker" for e in f.events(a))
    assert not list(data_dir.glob(".import-*"))                                  # no leftovers


def test_running_session_cannot_be_deleted_but_can_be_renamed(data_dir: Path):
    f = KernelFacade(data_dir)

    async def go():
        info = await f.start_session(default_pipeline("synthetic", realtime=True))
        sid = info["id"]
        try:
            with pytest.raises(Busy):
                f.delete_session(sid)
            assert f.rename_session(sid, "renamed live")["label"] == "renamed live"
        finally:
            await f.stop_session(sid)
        return sid
    sid = asyncio.run(go())
    assert json.loads((data_dir / sid / "session.json").read_text(encoding="utf-8"))["label"] == "renamed live"


def test_export_several_as_csv_and_one_as_edf(data_dir: Path, tmp_path: Path):
    f = KernelFacade(data_dir)
    a, b = record(f, "a", seed=1), record(f, "b", seed=2)
    out = f.export_sessions([a, b], "csv", tmp_path / "csv.zip")
    with zipfile.ZipFile(out) as z:
        names = set(z.namelist())
    assert {f"{a}/eeg.csv", f"{b}/eeg.csv", f"{a}/events.csv"} <= names
    edf = f.export_sessions([a], "edf", tmp_path / "a.edf")
    assert edf.read_bytes()[192:197] == b"EDF+C"
    with pytest.raises(ConfigError):
        f.export_sessions([a], "xlsx", tmp_path / "x")


def zip_bytes(files: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        for name, data in files.items():
            z.writestr(name, data)
    return buf.getvalue()


def test_import_rejects_unsafe_and_invalid_archives(data_dir: Path, tmp_path: Path):
    f = KernelFacade(data_dir)
    sid = record(f, "ok")
    good = (data_dir / sid / "session.json").read_bytes()
    evil = tmp_path / "evil.zip"
    evil.write_bytes(zip_bytes({f"{sid}x/session.json": good, f"{sid}x/../../escape.txt": b"x"}))
    with pytest.raises(ConfigError, match="unsafe path"):
        f.import_sessions(evil)
    assert not (data_dir.parent / "escape.txt").exists() and not list(data_dir.glob(".import-*"))

    none = tmp_path / "none.zip"
    none.write_bytes(zip_bytes({"readme.txt": b"hello"}))
    with pytest.raises(ConfigError, match="no sessions found"):
        f.import_sessions(none)
    notzip = tmp_path / "notzip.zip"
    notzip.write_bytes(b"not a zip")
    with pytest.raises(ConfigError, match="not a zip"):
        f.import_sessions(notzip)

    bad_id = json.loads(good)
    bad_id["id"] = "../outside"
    nodata = json.loads(good)
    nodata["id"] = "01NODATA"
    mixed = tmp_path / "mixed.zip"
    mixed.write_bytes(zip_bytes({"one/session.json": json.dumps(bad_id).encode(),
                                 "two/session.json": json.dumps(nodata).encode()}))
    r = f.import_sessions(mixed)
    assert r["imported"] == [] and len(r["skipped"]) == 2
    assert "invalid session.json" in r["skipped"][0]["reason"] and "no recorded data" in r["skipped"][1]["reason"]
    assert not (data_dir / "01NODATA").exists()


def test_http_management_round_trip(data_dir: Path):
    f = KernelFacade(data_dir)
    sid = record(f, "via http")
    with TestClient(create_app(f)) as client:
        r = client.patch(f"/api/v1/sessions/{sid}", json={"label": "renamed"})
        assert r.status_code == 200 and r.json()["label"] == "renamed"
        r = client.post("/api/v1/sessions/export", json={"ids": [sid], "format": "session"})
        assert r.status_code == 200 and f'{sid}.muse.zip' in r.headers["content-disposition"]
        archive = r.content
        assert not list((data_dir / ".muse" / "tmp").glob("*"))                 # temporary file removed
        assert client.delete(f"/api/v1/sessions/{sid}").json() == {"deleted": sid}
        assert client.get(f"/api/v1/sessions/{sid}").status_code == 404
        r = client.post("/api/v1/sessions/import", content=archive, headers={"Content-Type": "application/zip"})
        assert r.status_code == 200 and [x["id"] for x in r.json()["imported"]] == [sid]
        assert client.get(f"/api/v1/sessions/{sid}").json()["label"] == "renamed"
        assert client.post("/api/v1/sessions/import", content=b"junk").status_code == 422
        assert client.post("/api/v1/sessions/export", json={"ids": [], "format": "csv"}).status_code == 422


def test_cli_management(data_dir: Path, tmp_path: Path):
    f = KernelFacade(data_dir)
    sid = record(f, "via cli")
    runner = CliRunner()
    assert runner.invoke(cli, ["sessions", "rename", sid, "new name", "--json"]).exit_code == 0
    out = tmp_path / "s.zip"
    r = runner.invoke(cli, ["sessions", "export", sid, "--format", "session", "--out", str(out), "--json"])
    assert r.exit_code == 0 and out.is_file()
    assert runner.invoke(cli, ["sessions", "delete", sid], input="n\n").exit_code == 1      # declined
    assert (data_dir / sid).exists()
    assert runner.invoke(cli, ["sessions", "delete", sid, "--yes", "--json"]).exit_code == 0
    r = runner.invoke(cli, ["sessions", "import", str(out), "--json"])
    assert r.exit_code == 0 and json.loads(r.stdout)["imported"][0]["label"] == "new name"


def test_remote_client_management(data_dir: Path, tmp_path: Path, monkeypatch):
    """RemoteFacade against the real app (in-process transport): uploads must stream asynchronously."""
    import httpx

    from muse_monitor.interfaces.client import RemoteFacade
    f = KernelFacade(data_dir)
    sid = record(f, "remote")
    app = create_app(f)

    async def go():
        remote = RemoteFacade("http://test")
        remote.http = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test/api/v1")
        assert (await remote.rename_session(sid, "renamed remotely"))["label"] == "renamed remotely"
        out = await remote.export_sessions([sid], "session", tmp_path / "r.zip")
        assert (await remote.delete_session(sid)) == {"deleted": sid}
        r = await remote.import_sessions(out)
        assert [x["id"] for x in r["imported"]] == [sid]
        with pytest.raises(NotFound):
            await remote.delete_session("NOSUCH")
        await remote.http.aclose()
    asyncio.run(go())
