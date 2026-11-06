"""Smoke test for an installed muse-monitor (a built wheel in a clean environment, not the source tree).

Usage: python scripts/smoke_test.py [EXPECTED_VERSION]

Checks what packaging can break: the version, the console scripts, the web UI files, the plugin entry points
and an offline session end to end (synthetic source -> analyzers -> recorder -> report)."""

from __future__ import annotations

import asyncio
import json
import shutil
import subprocess
import sys
import tempfile
from importlib.metadata import version
from pathlib import Path


def check(ok: bool, what: str) -> None:
    print(("ok   " if ok else "FAIL ") + what)
    if not ok:
        sys.exit(1)


def main() -> None:
    import muse_monitor
    check("site-packages" in Path(muse_monitor.__file__).as_posix(), f"imported from {muse_monitor.__file__}")

    installed = version("muse-monitor")
    if len(sys.argv) > 1:
        check(installed == sys.argv[1].removeprefix("v"), f"version {installed} == {sys.argv[1]}")

    for script in ("muse", "muse-check"):
        check(shutil.which(script) is not None, f"console script {script} installed")
    out = subprocess.run(["muse", "--version"], capture_output=True, text=True, check=True).stdout
    check(installed in out, f"`muse --version` prints {installed}")

    from muse_monitor.interfaces.http import WEB_DIR
    for name in ("index.html", "app.js", "renderers.js", "style.css", "favicon.svg"):
        check((WEB_DIR / name).is_file(), f"web/{name} packaged")

    out = subprocess.run(["muse", "plugins", "list", "--json"], capture_output=True, text=True, check=True).stdout
    plugins = {p["name"]: p for p in json.loads(out)}
    expected = {"synthetic", "openmuse", "mindmonitor", "replay", "quality", "filter", "baseline", "bandpower",
                "artifacts", "cbramod", "describe", "recorder", "export"}
    check(expected <= set(plugins), f"all {len(expected)} built-in plugins registered")
    broken = [n for n, p in plugins.items() if n != "cbramod" and p["status"] != "ok"]    # cbramod: [models] extra
    check(not broken, f"plugins load without the models extra (broken: {broken})")

    from muse_monitor.kernel import KernelFacade
    from muse_monitor.kernel.config import default_pipeline

    async def session(data: Path) -> dict:
        f = KernelFacade(data)
        info = await f.start_session(default_pipeline("synthetic", realtime=False, duration_s=10))
        done = await f.wait_session(info["id"])
        job = await f.wait_job((await f.run_offline(done["id"]))["id"])
        return {"session": done, "job": job}

    with tempfile.TemporaryDirectory() as tmp:
        r = asyncio.run(session(Path(tmp)))
        check(r["session"]["status"] == "stopped", "synthetic session records")
        check(r["job"]["status"] == "done" and bool(r["job"]["result"].get("report")),
              "offline analysis writes a report")
    print(f"muse-monitor {installed}: smoke test passed")


if __name__ == "__main__":
    main()
