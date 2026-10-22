"""TCP tunnels for non-HTTP egress (Oracle, SQL Server, Postgres, SSH, …).

Clients connect to ``px-proxy:<listen_port>``; px-proxy forwards to
``target_host:target_port`` — but only while that target is allowed by an
allowlist rule (checked on EVERY new connection, so removing the rule cuts new
sessions immediately). With EGRESS_UPSTREAM_PROXY set (dev on a TLS-inspecting corporate network)
the forward goes through the upstream proxy with HTTP CONNECT.
"""

from __future__ import annotations

import asyncio
import base64
import logging
import time
from urllib.parse import urlsplit

from decisions import DecisionLog
from policy import Policy, Tunnel
from store import PolicyStore

log = logging.getLogger("px-proxy.tunnels")
BUF = 64 * 1024


class TunnelManager:
    def __init__(self, store: PolicyStore, dlog: DecisionLog, upstream: str | None,
                 bind: str = "0.0.0.0", upstream_auth: str | None = None):
        self.store = store
        self.dlog = dlog
        self.bind = bind
        self.upstream = urlsplit(upstream) if upstream else None
        self.upstream_auth = upstream_auth
        self.servers: dict[int, tuple[Tunnel, asyncio.base_events.Server]] = {}
        self.errors: dict[str, str] = {}
        self.active: dict[str, int] = {}
        self._loop: asyncio.AbstractEventLoop | None = None

    async def start(self) -> None:
        self._loop = asyncio.get_running_loop()
        await self.reconcile(self.store.policy)
        self.store.on_change(lambda pol: self._loop.call_soon_threadsafe(  # type: ignore[union-attr]
            lambda: asyncio.ensure_future(self.reconcile(pol))))

    async def reconcile(self, pol: Policy) -> None:
        want = {t.listen_port: t for t in pol.tcp_tunnels}
        for port, (t, srv) in list(self.servers.items()):
            if port not in want or want[port] != t:
                srv.close()
                await srv.wait_closed()
                del self.servers[port]
                log.info("tunnel %s on :%d stopped", t.name, port)
        self.errors = {}
        for port, t in want.items():
            if port in self.servers:
                continue
            try:
                srv = await asyncio.start_server(lambda r, w, t=t: self._handle(t, r, w), self.bind, port)
                self.servers[port] = (t, srv)
                log.info("tunnel %s listening on :%d → %s:%d", t.name, port, t.target_host, t.target_port)
            except OSError as e:
                self.errors[t.name] = str(e)
                log.error("tunnel %s cannot listen on :%d: %s", t.name, port, e)

    def status(self) -> list[dict]:
        pol = self.store.policy
        out = []
        for t in pol.tcp_tunnels:
            d = pol.decide(t.target_host, t.target_port)
            out.append({**t.to_dict(), "listening": t.listen_port in self.servers,
                        "target_allowed": d.matched, "rule": d.rule.pattern if d.rule else None,
                        "active_connections": self.active.get(t.name, 0), "error": self.errors.get(t.name)})
        return out

    async def _open_target(self, t: Tunnel):
        if not self.upstream:
            return await asyncio.wait_for(asyncio.open_connection(t.target_host, t.target_port), 15)
        up = self.upstream
        r, w = await asyncio.wait_for(asyncio.open_connection(up.hostname, up.port or 3128), 15)
        hdr = f"CONNECT {t.target_host}:{t.target_port} HTTP/1.1\r\nHost: {t.target_host}:{t.target_port}\r\n"
        if self.upstream_auth:
            hdr += "Proxy-Authorization: Basic " + base64.b64encode(self.upstream_auth.encode()).decode() + "\r\n"
        w.write((hdr + "\r\n").encode())
        await w.drain()
        status = await asyncio.wait_for(r.readline(), 30)
        parts = status.split()
        if len(parts) < 2 or parts[1] != b"200":
            w.close()
            raise ConnectionError(f"upstream proxy refused CONNECT: {status.decode(errors='replace').strip()}")
        while (await r.readline()) not in (b"\r\n", b"\n", b""):
            pass
        return r, w

    async def _handle(self, t: Tunnel, cr: asyncio.StreamReader, cw: asyncio.StreamWriter) -> None:
        peer = cw.get_extra_info("peername")
        client = peer[0] if peer else None
        t0 = time.time()
        d = self.store.policy.decide(t.target_host, t.target_port)
        base = {"client": client, "host": t.target_host, "port": t.target_port, "via": "tunnel",
                "tunnel": t.name, "allowed": d.allowed, "action": d.action, "reason": d.reason,
                "rule": d.rule.pattern if d.rule else None, "category": d.rule.category if d.rule else None,
                "mode": d.mode}
        if not d.allowed:
            self.dlog.record(**base)
            cw.close()
            return
        try:
            tr, tw = await self._open_target(t)
        except Exception as e:  # noqa: BLE001
            self.dlog.record(**base, error=f"connect-failed: {e}"[:300], ms=int((time.time() - t0) * 1000))
            cw.close()
            return
        self.active[t.name] = self.active.get(t.name, 0) + 1
        counts = [0, 0]

        async def pipe(src: asyncio.StreamReader, dst: asyncio.StreamWriter, idx: int) -> None:
            try:
                while data := await src.read(BUF):
                    counts[idx] += len(data)
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, asyncio.CancelledError):
                pass
            finally:
                try:
                    dst.close()
                except Exception:  # noqa: BLE001
                    pass

        try:
            await asyncio.gather(pipe(cr, tw, 0), pipe(tr, cw, 1))
        finally:
            self.active[t.name] -= 1
            self.dlog.record(**base, bytes_out=counts[0], bytes_in=counts[1], ms=int((time.time() - t0) * 1000))
