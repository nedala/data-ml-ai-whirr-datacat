"""Open WebUI bootstrap/sync for the DataPlatform appliance (runs INSIDE the
openwebui container, next to the server, using only its Python + stdlib/yaml).

1. waits for Open WebUI, signs in as the appliance automation account through
   the trusted-header login (the same mechanism nginx uses for real users);
2. discovers the appliance MCP server's tools (streamable HTTP tools/list) and
   registers one Open WebUI MCP tool server PER GROUP (tool_groups.yaml) on
   its own endpoint (${MCP_URL}/<group>; prefix-filtered root endpoint as a
   fallback), plus the rag OpenAPI tool server;
3. creates/updates the model presets (presets.yaml) with their tool ids;
4. keeps re-checking every OWUI_SYNC_INTERVAL seconds and re-applies when the
   MCP tool list changes (the MCP server may start after Open WebUI).

Managed entries carry ids prefixed "bsp-"; anything an admin adds by hand is
preserved. Status: $DATA_DIR/bsp-sync-status.json.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import yaml

HERE = Path(__file__).resolve().parent
OWUI = os.environ.get("OWUI_INTERNAL_URL", "http://127.0.0.1:8080")
EMAIL_H = os.environ.get("WEBUI_AUTH_TRUSTED_EMAIL_HEADER", "X-Auth-Email")
NAME_H = os.environ.get("WEBUI_AUTH_TRUSTED_NAME_HEADER", "X-Auth-Name")
ROLE_H = os.environ.get("WEBUI_AUTH_TRUSTED_ROLE_HEADER", "")
BOT = os.environ.get("OWUI_AUTOMATION_EMAIL", "appliance-automation@appliance.local")
MCP_URL = os.environ.get("OWUI_MCP_URL") or os.environ.get("MCP_URL", "http://mcp:8095/mcp")
MCP_TOKEN = os.environ.get("MCP_TOKEN", "")
INTERVAL = int(os.environ.get("OWUI_SYNC_INTERVAL", "120"))
STATUS = Path(os.environ.get("DATA_DIR", "/app/backend/data")) / "bsp-sync-status.json"
PUBLIC = [{"principal_type": "user", "principal_id": "*", "permission": "read"}]
SEPS = ("_", ".", "-", ":", "/")


def log(msg: str) -> None:
    print(f"[bsp-owui-sync] {msg}", flush=True)


def http(method: str, url: str, body=None, headers: dict | None = None, timeout: int = 30):
    data = None if body is None else json.dumps(body).encode()
    req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json",
                                                                         "Accept": "application/json, text/event-stream",
                                                                         **(headers or {})})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        raw = r.read().decode()
        return r.status, dict(r.headers), raw


def owui(method: str, path: str, token: str, body=None):
    st, _, raw = http(method, OWUI + path, body, {"Authorization": f"Bearer {token}"})
    return json.loads(raw) if raw else None


def wait_ready(max_s: int = 600) -> None:
    t0 = time.time()
    while time.time() - t0 < max_s:
        try:
            if http("GET", OWUI + "/health", timeout=5)[0] == 200:
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(3)
    raise RuntimeError("Open WebUI did not become healthy")


def signin() -> str:
    h = {EMAIL_H: BOT, NAME_H: "Appliance automation"}
    if ROLE_H:
        h[ROLE_H] = "admin"
    _, _, raw = http("POST", OWUI + "/api/v1/auths/signin", {"email": BOT, "password": "-"}, h)
    d = json.loads(raw)
    if d.get("role") != "admin":
        raise RuntimeError(f"automation account is {d.get('role')!r}, not admin — set WEBUI_AUTH_TRUSTED_ROLE_HEADER")
    return d["token"]


# ── MCP discovery (streamable HTTP, JSON or SSE responses) ───────────────
def _rpc_result(raw: str, headers: dict) -> dict:
    ctype = {k.lower(): v for k, v in headers.items()}.get("content-type", "")
    if "text/event-stream" in ctype:
        for line in raw.splitlines():
            if line.startswith("data:"):
                msg = json.loads(line[5:].strip())
                if "result" in msg or "error" in msg:
                    return msg
        return {}
    return json.loads(raw) if raw.strip() else {}


def mcp_tools(url: str = MCP_URL) -> list[dict]:
    h = {"Authorization": f"Bearer {MCP_TOKEN}"} if MCP_TOKEN else {}
    st, hd, raw = http("POST", url, {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "bsp-owui-sync", "version": "1"}}}, h)
    sid = {k.lower(): v for k, v in hd.items()}.get("mcp-session-id")
    if sid:
        h["Mcp-Session-Id"] = sid
    try:
        http("POST", url, {"jsonrpc": "2.0", "method": "notifications/initialized"}, h)
    except urllib.error.HTTPError:
        pass
    tools, cursor = [], None
    for _ in range(20):
        params = {"cursor": cursor} if cursor else {}
        st, hd, raw = http("POST", url, {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": params}, h)
        res = _rpc_result(raw, hd).get("result") or {}
        tools += res.get("tools", [])
        cursor = res.get("nextCursor")
        if not cursor:
            break
    return tools


def discover(groups: list[dict]) -> dict[str, dict]:
    """{group id: {"url", "tools": [...], "filtered": bool}} — per-group endpoints
    first (${MCP_URL}/<mcp_group>), else prefix-filtered root endpoint."""
    tools = mcp_tools(MCP_URL)                       # raises if the server is down
    by_prefix = group_tools(tools, groups)
    out: dict[str, dict] = {}
    for g in groups:
        sub = MCP_URL.rstrip("/") + "/" + g.get("mcp_group", g["id"])
        try:
            names = [t["name"] for t in mcp_tools(sub)]
        except Exception:  # noqa: BLE001 — no per-group endpoint on this server
            names = []
        if names:
            out[g["id"]] = {"url": sub, "tools": names, "filtered": False}
        elif by_prefix.get(g["id"]):
            out[g["id"]] = {"url": MCP_URL, "tools": by_prefix[g["id"]], "filtered": True}
    return out


def group_tools(tools: list[dict], groups: list[dict]) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {g["id"]: [] for g in groups}
    for t in tools:
        name = t["name"]
        low = name.lower()
        for g in groups:
            if any(low == p or low.startswith(p + s) for p in g["prefixes"] for s in SEPS):
                out[g["id"]].append(name)
                break
    return out


# ── config builders ──────────────────────────────────────────────────────
def build_connections(cfg: dict, found: dict[str, dict] | None) -> list[dict]:
    conns = []
    base = {"type": "mcp", "path": "", "auth_type": "bearer" if MCP_TOKEN else "none", "key": MCP_TOKEN, "headers": None}
    if found is None:
        conns.append({**base, "url": MCP_URL, "config": {"enable": True, "access_grants": PUBLIC},
                      "info": {"id": "bsp-mcp-all", "name": "Appliance tools (all MCP groups)",
                               "description": "All tools of the appliance MCP server (group discovery pending)."}})
    else:
        for g in cfg["mcp_groups"]:
            f = found.get(g["id"])
            if not f:
                continue
            conf = {"enable": True, "access_grants": PUBLIC}
            if f["filtered"]:
                conf["function_name_filter_list"] = ",".join(f["tools"])
            conns.append({**base, "url": f["url"], "config": conf,
                          "info": {"id": f"bsp-mcp-{g['id']}", "name": g["name"],
                                   "description": f"{g['description']} ({len(f['tools'])} tools)"}})
    for o in cfg.get("openapi", []):
        conns.append({"type": "openapi", "url": os.environ.get(o["url_env"], o["url_default"]), "path": o["path"],
                      "auth_type": "bearer", "key": os.environ.get(o.get("key_env", ""), ""), "headers": None,
                      "config": {"enable": True, "access_grants": PUBLIC},
                      "info": {"id": f"bsp-{o['id']}", "name": o["name"], "description": o["description"]}})
    return conns


def tool_ids(preset: dict, conns: list[dict]) -> list[str]:
    ids = {c["info"]["id"]: c for c in conns}
    out = []
    for t in preset.get("tools", []):
        if t.startswith("openapi:"):
            cid = "bsp-" + t.split(":", 1)[1]
            if cid in ids:
                out.append(f"server:{cid}")
        elif f"bsp-mcp-{t}" in ids:
            out.append(f"server:mcp:bsp-mcp-{t}")
    if not any(x.startswith("server:mcp:") for x in out) and "bsp-mcp-all" in ids and any(not t.startswith("openapi:") for t in preset.get("tools", [])):
        out.append("server:mcp:bsp-mcp-all")
    return out


def apply(token: str, cfg: dict, presets: list[dict], grouped: dict[str, dict] | None) -> dict:
    conns = build_connections(cfg, grouped)
    current = (owui("GET", "/api/v1/configs/tool_servers", token) or {}).get("TOOL_SERVER_CONNECTIONS", [])
    keep = [c for c in current if not str((c.get("info") or {}).get("id", "")).startswith("bsp-")]
    owui("POST", "/api/v1/configs/tool_servers", token, {"TOOL_SERVER_CONNECTIONS": keep + conns})
    done = []
    for p in presets:
        body = {"id": p["id"], "base_model_id": p["base_model"], "name": p["name"], "is_active": True,
                "meta": {"description": p["description"], "toolIds": tool_ids(p, conns),
                         "suggestion_prompts": [{"content": s} for s in p.get("suggestions", [])],
                         "tags": [{"name": "dataplatform"}], "capabilities": {"vision": False, "citations": True}},
                "params": {"system": p.get("system", ""), "function_calling": "native"},
                "access_grants": PUBLIC}
        try:
            exists = owui("GET", f"/api/v1/models/model?id={p['id']}", token)
        except urllib.error.HTTPError:
            exists = None
        if exists:
            owui("POST", f"/api/v1/models/model/update?id={p['id']}", token, body)
            try:
                owui("POST", f"/api/v1/models/model/access/update?id={p['id']}", token, {"id": p["id"], "access_grants": PUBLIC})
            except urllib.error.HTTPError:
                pass
        else:
            owui("POST", "/api/v1/models/create", token, body)
        done.append({"id": p["id"], "tools": body["meta"]["toolIds"]})
    return {"connections": [c["info"]["id"] for c in conns], "presets": done}


def main() -> int:
    cfg = yaml.safe_load((HERE / "tool_groups.yaml").read_text())
    presets = yaml.safe_load((HERE / "presets.yaml").read_text())["presets"]
    wait_ready()
    last_sig = None
    while True:
        status: dict = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "mcp_url": MCP_URL}
        try:
            token = signin()
            try:
                grouped = discover(cfg["mcp_groups"])
                status["mcp"] = {"ok": True, "groups": {k: {"tools": len(v["tools"]), "url": v["url"],
                                                            "mode": "prefix-filtered root" if v["filtered"] else "per-group endpoint"}
                                                        for k, v in grouped.items()}}
            except Exception as e:  # noqa: BLE001
                grouped = None
                status["mcp"] = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            sig = json.dumps(grouped, sort_keys=True)
            if sig != last_sig:
                status["applied"] = apply(token, cfg, presets, grouped)
                last_sig = sig
                log(f"applied: {status['applied']['connections']} · mcp={status['mcp']}")
            status["ok"] = True
        except Exception as e:  # noqa: BLE001
            status.update(ok=False, error=f"{type(e).__name__}: {e}")
            log(f"sync failed: {status['error']}")
        try:
            STATUS.write_text(json.dumps(status, indent=2))
        except OSError:
            pass
        if os.environ.get("OWUI_SYNC_ONCE") == "1":
            return 0 if status.get("ok") else 1
        time.sleep(INTERVAL if status.get("ok") and status.get("mcp", {}).get("ok") else 30)


if __name__ == "__main__":
    sys.exit(main())
