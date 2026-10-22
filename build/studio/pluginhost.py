"""Plugin discovery (plan §4.1): import build/studio/plugins/*.py and collect
router, PAGE, export_backup, import_backup, secret_env, before_up, ROTATE and
health. A plugin that fails to import — or whose hook raises — is logged and
reported, never fatal."""

from __future__ import annotations

import importlib
import logging
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from types import ModuleType
from typing import Any

import settings

log = logging.getLogger("studio.plugins")

PLUGIN_DIR = settings.HERE / "plugins"


@dataclass
class Plugin:
    name: str
    module: ModuleType

    def hook(self, attr: str) -> Any:
        return getattr(self.module, attr, None)


@dataclass
class Registry:
    plugins: list[Plugin] = field(default_factory=list)
    errors: list[dict[str, str]] = field(default_factory=list)

    def fail(self, plugin: str, what: str, exc: BaseException) -> None:
        log.error("plugin %s: %s failed: %s", plugin, what, exc)
        log.debug("".join(traceback.format_exception(exc)))
        self.errors.append({"plugin": plugin, "where": what, "error": f"{type(exc).__name__}: {exc}"})

    def pages(self) -> list[dict[str, Any]]:
        out = []
        for p in self.plugins:
            page = p.hook("PAGE")
            if isinstance(page, dict) and page.get("id") and page.get("script"):
                out.append({"order": 50, "group": "Configure", "icon": "plug", "admin": True, **page,
                            "plugin": p.name})
        return out

    def with_hook(self, attr: str) -> list[tuple[Plugin, Any]]:
        return [(p, h) for p in self.plugins if (h := p.hook(attr)) is not None]

    def secret_env(self) -> dict[str, str]:
        env: dict[str, str] = {}
        for p, fn in self.with_hook("secret_env"):
            try:
                env.update({str(k): str(v) for k, v in (fn() or {}).items() if v is not None})
            except Exception as exc:  # noqa: BLE001 — plugin isolation
                self.fail(p.name, "secret_env", exc)
        return env

    def before_up(self, services: list[str], log_line: Callable[[str], None]) -> None:
        for p, fn in self.with_hook("before_up"):
            try:
                fn(list(services))
                log_line(f"plugin {p.name}: prepared configuration")
            except Exception as exc:  # noqa: BLE001
                self.fail(p.name, "before_up", exc)
                log_line(f"plugin {p.name}: before_up failed — {exc}")

    def rotators(self, env_name: str) -> list[tuple[str, Callable[[str], None]]]:
        out = []
        for p, table in self.with_hook("ROTATE"):
            if isinstance(table, dict) and callable(table.get(env_name)):
                out.append((p.name, table[env_name]))
        return out

    def health(self) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        for p, fn in self.with_hook("health"):
            try:
                for item in fn() or []:
                    items.append({"state": "ok", "detail": "", **item, "plugin": p.name})
            except Exception as exc:  # noqa: BLE001
                items.append({"label": f"{p.name} plugin", "state": "error", "detail": str(exc), "plugin": p.name})
        return items

    def export_backup(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for p, fn in self.with_hook("export_backup"):
            try:
                out[p.name] = fn()
            except Exception as exc:  # noqa: BLE001
                self.fail(p.name, "export_backup", exc)
        return out

    def import_backup(self, data: dict[str, Any]) -> list[str]:
        """Returns the names of plugins that failed."""
        failed = []
        for p, fn in self.with_hook("import_backup"):
            if p.name not in data:
                continue
            try:
                fn(data[p.name])
            except Exception as exc:  # noqa: BLE001
                self.fail(p.name, "import_backup", exc)
                failed.append(p.name)
        return failed


registry = Registry()


def discover(directory: Path = PLUGIN_DIR) -> Registry:
    reg = Registry()
    for path in sorted(directory.glob("*.py")):
        if path.name.startswith("_"):
            continue
        name = path.stem
        try:
            module = importlib.import_module(f"plugins.{name}")
        except Exception as exc:  # noqa: BLE001
            reg.fail(name, "import", exc)
            continue
        reg.plugins.append(Plugin(name, module))
        log.info("plugin loaded: %s", name)
    return reg


def load() -> Registry:
    global registry
    registry = discover()
    return registry
