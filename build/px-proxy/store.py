"""PolicyStore: the live allowlist file, hot-reloaded on mtime change.

Seeded from allowlist.default.yaml when missing. A reload that fails to parse
keeps the previous good policy (never fails open, never fails closed on a
typo); a reload with some bad rules drops only those rules and records the
problems for the admin API / Studio.
"""

from __future__ import annotations

import hashlib
import logging
import os
import threading
from pathlib import Path
from typing import Callable

from policy import Policy, PolicyError
from trust import atomic_write

log = logging.getLogger("px-proxy.policy")


class PolicyStore:
    def __init__(self, path: Path, default_path: Path, env_mode: str | None = None,
                 allow_audit: bool = True, readonly: bool = False):
        self.path = path
        self.default_path = default_path
        self.env_mode = (env_mode or "").lower() or None
        self.allow_audit = allow_audit
        self.readonly = readonly
        self.policy = Policy()
        self.mtime: float | None = None
        self.version = ""
        self.problems: list[str] = []
        self.last_error: str | None = None
        self._lock = threading.RLock()
        self._listeners: list[Callable[[Policy], None]] = []
        self.seed()
        self.reload(force=True)

    def on_change(self, fn: Callable[[Policy], None]) -> None:
        self._listeners.append(fn)

    def seed(self) -> None:
        if self.path.exists() or self.readonly:
            return
        text = self.default_path.read_text(encoding="utf-8")
        if self.env_mode in ("enforce", "audit"):
            # EGRESS_MODE only seeds the file; afterwards the file (Studio) wins.
            pol = Policy.from_yaml(text)
            pol.mode = self.env_mode
            text = pol.to_yaml()
        atomic_write(self.path, text.encode(), 0o644)
        log.info("seeded %s from %s", self.path, self.default_path.name)

    def reload(self, force: bool = False) -> bool:
        with self._lock:
            try:
                st = self.path.stat()
            except FileNotFoundError:
                self.seed()
                if not self.path.exists():
                    self.last_error = f"{self.path} not found — nothing is allowed"
                    log.error("%s", self.last_error)
                    return False
                st = self.path.stat()
            if not force and self.mtime == st.st_mtime:
                return False
            raw = self.path.read_bytes()
            try:
                pol = Policy.from_yaml(raw.decode("utf-8"), strict=False)
            except PolicyError as e:
                self.last_error = str(e)
                self.mtime = st.st_mtime
                log.error("allowlist reload FAILED, keeping previous policy: %s", e)
                return False
            try:
                from policy import env_rules  # noqa: PLC0415
                pol.rules.extend(env_rules(existing=pol))
            except Exception as e:  # noqa: BLE001 — never lose the file policy over an env typo
                log.warning("environment egress hosts ignored: %s", e)
            if pol.mode == "audit" and not self.allow_audit:
                log.error("audit mode is disabled in this environment (EGRESS_ALLOW_AUDIT=false); enforcing")
                pol.mode = "enforce"
                pol.problems.append("audit mode disabled in this environment; enforcing")  # type: ignore[attr-defined]
            self.policy = pol
            self.problems = list(getattr(pol, "problems", []))
            self.last_error = None
            self.mtime = st.st_mtime
            self.version = hashlib.sha256(raw).hexdigest()[:12]
            for p in self.problems:
                log.warning("allowlist: %s", p)
            log.info("allowlist loaded: mode=%s rules=%d tunnels=%d version=%s",
                     pol.mode, len(pol.rules), len(pol.tcp_tunnels), self.version)
        for fn in self._listeners:
            try:
                fn(pol)
            except Exception:  # pragma: no cover
                log.exception("policy listener failed")
        return True

    def save(self, pol: Policy, base_version: str | None = None) -> None:
        """Validate (strict) + write atomically + reload. Raises PolicyError,
        or ValueError('conflict') if base_version is stale."""
        with self._lock:
            if self.readonly:
                raise PermissionError("the allowlist is managed by GitOps (ConfigMap) in this environment — "
                                      "change it in the infrastructure repository")
            if base_version and base_version != self.version:
                raise ValueError("conflict")
            if pol.mode == "audit" and not self.allow_audit:
                raise PolicyError(["audit mode is disabled in this environment (EGRESS_ALLOW_AUDIT=false)"])
            text = pol.to_yaml()
            Policy.from_yaml(text, strict=True)  # round-trip check
            atomic_write(self.path, text.encode(), 0o644)
            self.reload(force=True)

    def snapshot(self) -> dict:
        with self._lock:
            return {"policy": self.policy.to_dict(), "version": self.version, "mode": self.policy.mode,
                    "path": str(self.path), "problems": self.problems, "last_error": self.last_error,
                    "allow_audit": self.allow_audit, "readonly": self.readonly,
                    "mtime": self.mtime, "env": os.environ.get("APPLIANCE_ENV", "dev")}
