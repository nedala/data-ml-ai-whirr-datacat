"""Configuration backup (*.bspconfig) — see docs/site/v2/studio.md.

File = JSON envelope {"format": "bsp-appliance-config", "v": 1,
"created", "kdf": "scrypt", "n", "r", "p", "salt", "nonce", "ct"}; ct is
AES-256-GCM over the JSON payload with AAD "bsp-appliance-config/v1",
keyed by scrypt(export passphrase)."""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import secrets
import socket
import time
from pathlib import Path
from typing import Any

import cryptobox
import orchestrator
import pluginhost
import settings
import users
from fsutil import read_json, write_file, write_private_json
from vault import store

FORMAT = "bsp-appliance-config"
VERSION = 1
AAD = f"{FORMAT}/v{VERSION}".encode()
SECTIONS = ("vault", "users", "selection", "plugins", "packages", "egress")
MIN_PASSPHRASE = 12
PENDING_TTL = 600


class BackupError(ValueError):
    pass


def _workbench(method: str, path: str, **kw: Any) -> Any:
    import requests  # local: only needed here
    with requests.Session() as session:
        session.trust_env = False   # internal call, never via px-proxy
        return session.request(method, f"{settings.cfg.workbench_url}/pkg/api/{path}", timeout=kw.pop("timeout", 10), **kw)


def _workbench_manifest() -> Any:
    try:
        r = _workbench("GET", "manifest")
        return r.json() if r.ok else None
    except (OSError, ValueError):
        return None


def _restore_manifest(manifest: Any) -> str:
    try:
        r = _workbench("POST", "manifest/restore", json=manifest, headers={"X-Studio": "1"}, timeout=60)
    except OSError as exc:
        return f"packages skipped (workspace not reachable: {type(exc).__name__}) — start the Workspace and import again"
    return "packages restored" if r.ok else f"packages failed ({r.status_code}: {r.text[:120]})"


def _allowlist() -> Path:
    return settings.cfg.data / "egress" / "allowlist.yaml"


def _egress_plugin_exports() -> bool:
    return any(p.name == "egress" for p, _ in pluginhost.registry.with_hook("export_backup"))


def collect(user: str) -> dict[str, Any]:
    allow = _allowlist()
    return {
        "meta": {"created": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"), "by": user,
                 "appliance_env": settings.cfg.appliance_env, "host": socket.gethostname()},
        "vault": store.snapshot(),
        "selection": orchestrator.load_selection(),
        "users": read_json(settings.cfg.vault_dir / "users.json", {"users": []}),
        "plugins": pluginhost.registry.export_backup(),
        "packages": _workbench_manifest(),
        # the egress plugin exports the allowlist itself; only keep a raw copy without it
        "egress": None if _egress_plugin_exports() or not allow.is_file() else allow.read_text(encoding="utf-8"),
    }


def export(passphrase: str, user: str) -> tuple[str, bytes]:
    if len(passphrase) < MIN_PASSPHRASE:
        raise BackupError(f"Export passphrase must be at least {MIN_PASSPHRASE} characters")
    payload = json.dumps(collect(user)).encode()
    envelope = {"format": FORMAT, "v": VERSION,
                "created": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
                **cryptobox.seal(passphrase, payload, AAD)}
    name = f"bsp-appliance-{dt.date.today():%Y-%m-%d}.bspconfig"
    return name, json.dumps(envelope, indent=1).encode()


def decrypt(blob: bytes, passphrase: str) -> dict[str, Any]:
    try:
        envelope = json.loads(blob)
    except ValueError as exc:
        raise BackupError("Not a .bspconfig file") from exc
    if envelope.get("format") != FORMAT:
        raise BackupError("Not a DataPlatform appliance configuration file")
    if envelope.get("v") != VERSION:
        raise BackupError(f"Unsupported backup version {envelope.get('v')}")
    try:
        return json.loads(cryptobox.unseal(envelope, passphrase, AAD))
    except cryptobox.DecryptError as exc:
        raise BackupError("Wrong passphrase, or the file is damaged") from exc


def _leaves(node: Any, prefix: str = "") -> dict[str, str]:
    """Flatten to {dotted path: fingerprint} — values never leave the server."""
    if isinstance(node, dict):
        out: dict[str, str] = {}
        for k, v in node.items():
            out.update(_leaves(v, f"{prefix}.{k}" if prefix else str(k)))
        return out
    return {prefix: hashlib.sha256(json.dumps(node, sort_keys=True).encode()).hexdigest()}


def _diff(current: dict[str, str], incoming: dict[str, str]) -> dict[str, list[str]]:
    return {"added": sorted(set(incoming) - set(current)),
            "removed": sorted(set(current) - set(incoming)),
            "changed": sorted(k for k in set(current) & set(incoming) if current[k] != incoming[k])}


def preview(payload: dict[str, Any], current_user: str) -> dict[str, Any]:
    strip_meta = lambda d: {k: v for k, v in (d or {}).items() if k != "meta"}  # noqa: E731
    cur_users = {u["name"]: u.get("role") for u in read_json(settings.cfg.vault_dir / "users.json", {"users": []})["users"]}
    new_users = {u["name"]: u.get("role") for u in (payload.get("users") or {}).get("users", [])}
    sel_now, sel_new = orchestrator.load_selection(), payload.get("selection") or {}
    allow = _allowlist()
    return {
        "meta": payload.get("meta", {}),
        "vault": _diff(_leaves(strip_meta(store.snapshot())), _leaves(strip_meta(payload.get("vault")))),
        "users": {"added": sorted(set(new_users) - set(cur_users)), "removed": sorted(set(cur_users) - set(new_users)),
                  "changed": sorted(n for n in set(cur_users) & set(new_users) if cur_users[n] != new_users[n]),
                  "keeps_you": current_user in new_users},
        "selection": {"current": {k: sel_now.get(k, []) for k in ("personas", "capabilities")},
                      "incoming": {k: sel_new.get(k, []) for k in ("personas", "capabilities")}},
        "plugins": sorted((payload.get("plugins") or {}).keys()),
        "packages": sorted((payload.get("packages") or {}).keys()) if isinstance(payload.get("packages"), dict) else [],
        "egress": {"present": payload.get("egress") is not None,
                   "changed": payload.get("egress") is not None and
                   payload.get("egress") != (allow.read_text(encoding="utf-8") if allow.is_file() else None)},
        "vault_has_data": bool(_leaves(strip_meta(store.snapshot()))),
    }


_pending: dict[str, tuple[float, str, dict[str, Any]]] = {}


def stage(payload: dict[str, Any], user: str) -> str:
    now = time.time()
    for k in [k for k, (exp, _, _) in _pending.items() if exp < now]:
        del _pending[k]
    token = secrets.token_urlsafe(16)
    _pending[token] = (now + PENDING_TTL, user, payload)
    return token


def take(token: str, user: str) -> dict[str, Any]:
    exp, owner, payload = _pending.get(token, (0, "", {}))
    if exp < time.time() or owner != user:
        raise BackupError("This import has expired — upload the file again")
    del _pending[token]
    return payload


def apply(payload: dict[str, Any], sections: list[str], current_user: str) -> list[str]:
    done: list[str] = []
    if "vault" in sections and isinstance(payload.get("vault"), dict):
        incoming = payload["vault"]
        store.mutate(lambda d: (d.clear(), d.update(incoming)))
        done.append("vault")
    if "users" in sections and payload.get("users"):
        incoming = {u["name"]: u for u in payload["users"].get("users", [])}
        if current_user not in incoming and (me := users.get(current_user)):
            incoming[current_user] = me
        write_private_json(settings.cfg.vault_dir / "users.json", {"users": sorted(incoming.values(), key=lambda u: u["name"])})
        done.append("users")
    if "selection" in sections and payload.get("selection"):
        sel = payload["selection"]
        orchestrator.save_selection(sel.get("personas", []), sel.get("capabilities", []), current_user)
        done.append("selection")
    if "plugins" in sections and payload.get("plugins"):
        failed = pluginhost.registry.import_backup(payload["plugins"])
        done.append("plugins" + (f" (failed: {', '.join(failed)})" if failed else ""))
    if "packages" in sections and payload.get("packages"):
        done.append(_restore_manifest(payload["packages"]))
    if "egress" in sections and payload.get("egress") is not None:
        write_file(_allowlist(), payload["egress"])
        done.append("egress")
    return done
