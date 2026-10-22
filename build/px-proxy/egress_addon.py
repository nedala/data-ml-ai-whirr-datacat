"""mitmproxy addon: enforce the egress allowlist and log every decision.

Enforcement points
  * ``http_connect``    CONNECT host:port (HTTPS / any tunnelled protocol). A
                        blocked CONNECT is refused with 403 before any TLS or
                        upstream connection happens.
  * ``tls_clienthello`` SNI must equal the CONNECT host, or itself be allowed
                        (no domain-fronting past the allowlist). Rules with
                        ``tls: passthrough`` are not decrypted.
  * ``requestheaders``  plain HTTP (absolute-form) AND every request inside an
                        intercepted TLS tunnel: request host and Host header
                        must be allowed. Runs before the body is read, and with
                        ``connection_strategy=lazy`` before anything is sent
                        upstream.
Bodies are streamed (never buffered) and only counted.
"""

from __future__ import annotations

import json
import logging
import time

from mitmproxy import http, tcp, tls
from mitmproxy.connection import Client

from decisions import DecisionLog
from policy import Decision, normalize_host
from store import PolicyStore

log = logging.getLogger("px-proxy.addon")

ADMIN_HINT = "ask your appliance admin to allow it in Studio → Egress"
REASON_PHRASE = "Blocked by appliance egress policy"


def _peer(client: Client | None) -> str | None:
    try:
        return client.peername[0] if client and client.peername else None
    except Exception:
        return None


def _block_response(host: str, port: int | None, reason: str, accept: str = "") -> http.Response:
    msg = (f"Egress to {host}" + (f":{port}" if port and port not in (80, 443) else "")
           + f" is not allowed by the appliance egress policy ({reason}). "
           + f"To use it, {ADMIN_HINT}.")
    headers = {"X-Egress-Decision": "block", "X-Egress-Host": host, "X-Egress-Reason": reason,
               "Cache-Control": "no-store", "Connection": "close"}
    if "json" in accept and "html" not in accept:
        body = json.dumps({"error": "egress_blocked", "host": host, "port": port, "reason": reason,
                           "message": msg}).encode()
        headers["Content-Type"] = "application/json"
    elif "html" in accept:
        body = (
            "<!doctype html><meta charset=utf-8><title>Blocked by egress policy</title>"
            "<body style=\"font:15px/1.5 'Instrument Sans',system-ui,sans-serif;color:#1c2127;"
            "background:#eef0f2;margin:0;display:grid;place-items:center;min-height:100vh\">"
            "<div style=\"background:#fff;border-radius:8px;padding:28px 32px;max-width:560px;"
            "box-shadow:0 4px 16px rgba(47,44,49,.08);border-top:4px solid #0b6e79\">"
            "<div style=\"font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:#7f7f7d\">"
            "DataPlatform appliance · egress</div>"
            f"<h1 style=\"font-size:20px;margin:6px 0 10px\">{_esc(host)} is not on the allowlist</h1>"
            f"<p>{_esc(msg)}</p></div></body>"
        ).encode()
        headers["Content-Type"] = "text/html; charset=utf-8"
    else:
        body = (msg + "\n").encode()
        headers["Content-Type"] = "text/plain; charset=utf-8"
    r = http.Response.make(403, body, headers)
    r.reason = REASON_PHRASE
    return r


def _strip_port(h: str) -> str:
    if h.startswith("[") and "]" in h:
        return h[1:h.index("]")]
    return h.split(":", 1)[0] if h.count(":") == 1 else h


def _esc(s: str) -> str:
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


class EgressAddon:
    def __init__(self, store: PolicyStore, dlog: DecisionLog, tls_inspect: bool = True):
        self.store = store
        self.dlog = dlog
        self.tls_inspect = tls_inspect
        self.sni_violations: dict[str, str] = {}

    # ── helpers ─────────────────────────────────────────────────────
    def _log(self, d: Decision | None, *, host: str, port: int | None, via: str, client: str | None,
             **extra) -> None:
        rec = {"client": client, "host": host, "port": port, "via": via}
        if d is not None:
            rec.update({"allowed": d.allowed, "action": d.action, "rule": d.rule.pattern if d.rule else None,
                        "category": d.rule.category if d.rule else None, "reason": d.reason,
                        "mode": d.mode})
            if d.action == "audit-allow":
                rec["would_block"] = True
        rec.update({k: v for k, v in extra.items() if v is not None})
        self.dlog.record(**rec)

    # ── CONNECT ─────────────────────────────────────────────────────
    def http_connect(self, flow: http.HTTPFlow) -> None:
        host = normalize_host(flow.request.host)
        port = flow.request.port
        d = self.store.policy.decide(host, port)
        flow.metadata["egress_connect"] = d.to_dict()
        if not d.allowed:
            flow.response = _block_response(host, port, d.reason)
            flow.metadata["egress_logged"] = True
            self._log(d, host=host, port=port, via="connect", client=_peer(flow.client_conn), method="CONNECT")

    # ── TLS ─────────────────────────────────────────────────────────
    def tls_clienthello(self, data: tls.ClientHelloData) -> None:
        ctx = data.context
        if not ctx.server.address:
            return
        connect_host = normalize_host(ctx.server.address[0])
        port = ctx.server.address[1]
        sni = normalize_host(data.client_hello.sni or "")
        pol = self.store.policy
        if sni and sni != connect_host:
            sd = pol.decide(sni, port)
            if not sd.matched and pol.mode != "audit":
                # Never pass through. Intercepting means a client without the
                # egress CA fails the handshake, and one with it gets a 403
                # from requestheaders — either way nothing reaches upstream.
                self.sni_violations[ctx.client.id] = sni
                if not self.tls_inspect:
                    self._log(Decision(False, False, None, "sni-mismatch", pol.mode), host=sni, port=port,
                              via="connect", client=_peer(ctx.client),
                              detail=f"TLS SNI {sni} differs from CONNECT host {connect_host}")
                return
        d = pol.decide(sni or connect_host, port)
        if not self.tls_inspect or (d.matched and d.rule and d.rule.tls == "passthrough"):
            data.ignore_connection = True
            self._log(d, host=sni or connect_host, port=port, via="tls-passthrough", client=_peer(ctx.client))

    def tls_failed_client(self, data: tls.TlsData) -> None:
        ctx = data.context
        host = normalize_host(data.conn.sni or (ctx.server.address[0] if ctx.server.address else ""))
        port = ctx.server.address[1] if ctx.server.address else None
        d = self.store.policy.decide(host, port)
        self._log(d, host=host, port=port, via="connect", client=_peer(ctx.client), error="client-tls-failed",
                  detail="client rejected the egress CA certificate — is /trust/ca-bundle.pem "
                         "configured, or does the client pin certificates? "
                         "(rule option tls: passthrough)")

    def client_disconnected(self, client: Client) -> None:
        self.sni_violations.pop(client.id, None)

    # ── HTTP requests (plain + inside intercepted TLS) ─────────────
    def requestheaders(self, flow: http.HTTPFlow) -> None:
        if flow.request.method == "CONNECT":
            return
        req = flow.request
        host = normalize_host(req.host)
        port = req.port
        client = _peer(flow.client_conn)
        accept = req.headers.get("accept", "")
        pol = self.store.policy
        d = pol.decide(host, port)
        reason_override = None
        violation = self.sni_violations.get(flow.client_conn.id)
        if violation:
            d = Decision(pol.mode == "audit", False, None, "sni-mismatch", pol.mode)
            reason_override = f"TLS SNI {violation} differs from CONNECT host {host}"
        else:
            hh = normalize_host(_strip_port(req.host_header or ""))
            if hh and hh != host:
                hd = pol.decide(hh, port)
                if not hd.matched:
                    d = Decision(pol.mode == "audit", False, None, "host-header-not-allowed", pol.mode)
                    reason_override = f"Host header {hh} is not allowed"
                    host = hh
        flow.metadata["egress"] = d
        flow.metadata["egress_t0"] = time.time()
        if not d.allowed:
            flow.response = _block_response(host, port, d.reason, accept)
            flow.metadata["egress_logged"] = True
            self._log(d, host=host, port=port, via="http", client=client, method=req.method,
                      path=req.path if req.scheme == "http" else None, scheme=req.scheme, status=403,
                      detail=reason_override)
            return
        # stream + count request body (never buffered in memory)
        counter = [0]

        def _count_req(chunk: bytes) -> bytes:
            counter[0] += len(chunk)
            return chunk

        req.stream = _count_req
        flow.metadata["egress_tx"] = counter
        if reason_override:
            flow.metadata["egress_detail"] = reason_override

    def responseheaders(self, flow: http.HTTPFlow) -> None:
        if flow.request.method == "CONNECT" or flow.metadata.get("egress_logged"):
            return
        counter = [0]

        def _count_resp(chunk: bytes) -> bytes:
            counter[0] += len(chunk)
            return chunk

        flow.response.stream = _count_resp
        flow.metadata["egress_rx"] = counter

    def _finish(self, flow: http.HTTPFlow, error: str | None = None) -> None:
        if flow.request.method == "CONNECT" or flow.metadata.get("egress_logged"):
            return
        d: Decision | None = flow.metadata.get("egress")
        if d is None:
            return
        flow.metadata["egress_logged"] = True
        req = flow.request
        t0 = flow.metadata.get("egress_t0") or time.time()
        rx = flow.metadata.get("egress_rx")
        tx = flow.metadata.get("egress_tx")
        self._log(d, host=normalize_host(req.host), port=req.port, via="http", client=_peer(flow.client_conn),
                  method=req.method, path=req.path if req.scheme == "http" else None, scheme=req.scheme,
                  status=flow.response.status_code if flow.response else None,
                  bytes_in=rx[0] if rx else None, bytes_out=tx[0] if tx else None,
                  ms=int((time.time() - t0) * 1000), error=error, detail=flow.metadata.get("egress_detail"))

    def response(self, flow: http.HTTPFlow) -> None:
        self._finish(flow)

    def error(self, flow: http.HTTPFlow) -> None:
        msg = str(flow.error.msg if flow.error else "error")
        if "certificate verify failed" in msg.lower() or "certificate" in msg.lower():
            msg = f"upstream-tls: {msg} (corporate root missing from build/certs/corporate?)"
        self._finish(flow, error=msg[:300])

    # ── raw TCP inside an allowed CONNECT (non-HTTP protocols) ─────
    def tcp_start(self, flow: tcp.TCPFlow) -> None:
        flow.metadata["egress_t0"] = time.time()

    def tcp_end(self, flow: tcp.TCPFlow) -> None:
        if not flow.server_conn.address:
            return
        host, port = flow.server_conn.address[0], flow.server_conn.address[1]
        d = self.store.policy.decide(host, port)
        tx = sum(len(m.content) for m in flow.messages if m.from_client)
        rx = sum(len(m.content) for m in flow.messages if not m.from_client)
        self._log(d, host=normalize_host(host), port=port, via="tcp", client=_peer(flow.client_conn),
                  bytes_in=rx, bytes_out=tx, ms=int((time.time() - flow.metadata.get("egress_t0", time.time())) * 1000))
