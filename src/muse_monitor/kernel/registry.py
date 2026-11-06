"""Plugin discovery: entry points (installed packages, built-ins) and local plugin directories."""

from __future__ import annotations

import importlib.util
import inspect
import os
import sys
from dataclasses import dataclass, field
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from pydantic_core import to_jsonable_python

from ..sdk.plugin import API_VERSION, Analyzer, Plugin, Sink, Source
from .errors import ConfigError, NotFound, PluginUnavailable

ENTRY_GROUP = "muse_monitor.plugins"
KINDS = ("source", "analyzer", "sink")


@dataclass
class PluginManifest:
    name: str
    kind: str
    version: str
    summary: str
    api_version: int
    status: str                   # ok | unavailable | incompatible | broken
    hint: str = ""
    origin: str = ""
    config_schema: dict[str, Any] = field(default_factory=dict)
    config_defaults: dict[str, Any] = field(default_factory=dict)
    cpu_bound: bool = False
    critical: bool = False

    def to_dict(self) -> dict[str, Any]:
        return dict(self.__dict__)


def default_plugin_paths() -> list[Path]:
    paths = [Path.cwd() / "plugins"]
    extra = os.environ.get("MUSE_PLUGIN_PATH", "")
    paths += [Path(p) for p in extra.split(os.pathsep) if p]
    return paths


class PluginRegistry:
    def __init__(self, paths: list[Path] | None = None, use_entry_points: bool = True) -> None:
        self._classes: dict[str, type[Plugin]] = {}
        self._origins: dict[str, str] = {}
        self.broken: dict[str, str] = {}          # origin -> error
        if use_entry_points:
            for ep in entry_points(group=ENTRY_GROUP):
                try:
                    self.add(ep.load(), origin=f"entry point {ep.value}")
                except ConfigError:
                    raise
                except Exception as exc:  # a broken third-party package must not break the app
                    self.broken[ep.value] = f"{type(exc).__name__}: {exc}"
        for path in default_plugin_paths() if paths is None else paths:
            self._load_dir(path)

    # -- loading ----------------------------------------------------------------------------------
    def add(self, cls: type[Plugin], origin: str = "") -> None:
        if not (inspect.isclass(cls) and issubclass(cls, Plugin)):
            raise ConfigError(f"{cls!r} is not a Plugin subclass")
        name = getattr(cls, "name", None)
        if not name or getattr(cls, "kind", None) not in KINDS:
            raise ConfigError(f"{cls.__qualname__}: plugins need a name and kind in {KINDS}")
        if name in self._classes and self._classes[name] is not cls:
            raise ConfigError(f"duplicate plugin name {name!r} ({self._origins[name]} and {origin})")
        self._classes[name] = cls
        self._origins[name] = origin

    def _load_dir(self, path: Path) -> None:
        if not path.is_dir():
            return
        candidates = sorted(path.glob("*.py")) + sorted(p / "__init__.py" for p in path.iterdir()
                                                        if (p / "__init__.py").is_file())
        for file in candidates:
            if file.name.startswith("_") and file.name != "__init__.py":
                continue
            modname = f"muse_local_plugins.{file.parent.name if file.name == '__init__.py' else file.stem}"
            try:
                spec = importlib.util.spec_from_file_location(modname, file)
                assert spec and spec.loader
                module = importlib.util.module_from_spec(spec)
                sys.modules[modname] = module
                spec.loader.exec_module(module)
            except Exception as exc:
                self.broken[str(file)] = f"{type(exc).__name__}: {exc}"
                continue
            for _, obj in inspect.getmembers(module, inspect.isclass):
                if (issubclass(obj, Plugin) and obj.__module__ == modname
                        and obj not in (Plugin, Source, Analyzer, Sink) and getattr(obj, "name", None)):
                    self.add(obj, origin=str(file))

    # -- queries ----------------------------------------------------------------------------------
    def _status(self, cls: type[Plugin]) -> tuple[str, str]:
        if cls.api_version > API_VERSION:
            return "incompatible", f"targets plugin API v{cls.api_version}; this app supports v{API_VERSION}"
        missing = [m for m in cls.requires if not _importable(m)]
        if missing:
            return "unavailable", cls.install_hint or f"missing modules: {', '.join(missing)}"
        return "ok", ""

    def manifest(self, name: str) -> PluginManifest:
        cls = self._classes.get(name)
        if cls is None:
            raise NotFound(f"unknown plugin {name!r}")
        status, hint = self._status(cls)
        try:
            schema = cls.Config.model_json_schema()
        except Exception:
            schema = {}
        defaults: dict[str, Any] = {}
        for fname, f in cls.Config.model_fields.items():
            if not f.is_required():
                try:   # the JSON schema leaves out defaults that come from a default_factory
                    defaults[fname] = to_jsonable_python(f.get_default(call_default_factory=True))
                except Exception:
                    pass
        return PluginManifest(name=name, kind=cls.kind, version=cls.version, summary=cls.summary,
                              api_version=cls.api_version, status=status, hint=hint,
                              origin=self._origins.get(name, ""), config_schema=schema, config_defaults=defaults,
                              cpu_bound=cls.cpu_bound, critical=bool(getattr(cls, "critical", False)))

    def manifests(self, kind: str | None = None) -> list[PluginManifest]:
        return [self.manifest(n) for n in sorted(self._classes)
                if kind is None or self._classes[n].kind == kind]

    def get(self, name: str, kind: str | None = None) -> type[Plugin]:
        m = self.manifest(name)
        if kind and m.kind != kind:
            raise ConfigError(f"plugin {name!r} is a {m.kind}, not a {kind}", plugin=name)
        if m.status != "ok":
            raise PluginUnavailable(f"plugin {name!r} is {m.status}: {m.hint}", plugin=name)
        return self._classes[name]


def _importable(module: str) -> bool:
    try:
        return importlib.util.find_spec(module) is not None
    except (ImportError, ValueError):
        return False
