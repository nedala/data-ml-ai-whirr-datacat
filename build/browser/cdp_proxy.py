"""CDP proxy: expose Chromium's DevTools (127.0.0.1:9222) to appliance-net
at http://browser:9223.

Chromium refuses DevTools HTTP requests whose Host header is not an IP or
`localhost`, and builds `webSocketDebuggerUrl` from that header. This proxy
therefore rewrites the request Host to 127.0.0.1:9222 and, in the JSON
answers of /json*, rewrites `127.0.0.1:9222` back to the Host the client
used — so `chromium.connect_over_cdp("http://browser:9223")` (Playwright),
Puppeteer and plain CDP clients all get usable ws:// URLs. WebSocket
upgrades are piped through untouched after the head is rewritten.

    CDP_PROXY_LISTEN=0.0.0.0:9223  CDP_PROXY_UPSTREAM=127.0.0.1:9222
"""
from __future__ import annotations

import asyncio
import os
import sys

LISTEN = os.environ.get("CDP_PROXY_LISTEN", "0.0.0.0:9223")
UPSTREAM = os.environ.get("CDP_PROXY_UPSTREAM", "127.0.0.1:9222")
MAX_HEAD = 64 * 1024


def _split(addr: str) -> tuple[str, int]:
    host, _, port = addr.rpartition(":")
    return host, int(port)


def rewrite_head(head: bytes, upstream: str) -> tuple[bytes, str, bool]:
    """(new head, client Host, is websocket upgrade)."""
    lines = head.decode("latin-1").split("\r\n")
    client_host, upgrade, out = "", False, [lines[0]]
    for line in lines[1:]:
        if not line:
            continue
        name, _, value = line.partition(":")
        key = name.strip().lower()
        if key == "host":
            client_host = value.strip()
            continue
        if key == "upgrade" and value.strip().lower() == "websocket":
            upgrade = True
        if key == "connection" and not upgrade and "upgrade" not in value.lower():
            continue
        out.append(line)
    out.insert(1, f"Host: {upstream}")
    if not upgrade:
        out.append("Connection: close")
    return ("\r\n".join(out) + "\r\n\r\n").encode("latin-1"), client_host, upgrade


def rewrite_body(body: bytes, upstream: str, client_host: str) -> bytes:
    if not client_host or client_host == upstream:
        return body
    return body.replace(upstream.encode(), client_host.encode())


async def _read_head(reader: asyncio.StreamReader) -> bytes:
    data = await reader.readuntil(b"\r\n\r\n")
    if len(data) > MAX_HEAD:
        raise ValueError("request head too large")
    return data


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        try:
            writer.close()
        except Exception:  # noqa: BLE001
            pass


def _unavailable(writer: asyncio.StreamWriter) -> None:
    body = b'{"error":"browser not ready","hint":"Chromium is (re)starting on the desktop"}'
    writer.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\nContent-Length: "
                 + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)


async def handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
    try:
        head = await _read_head(client_r)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ValueError, ConnectionError):
        client_w.close()
        return
    new_head, client_host, upgrade = rewrite_head(head, UPSTREAM)
    try:
        up_r, up_w = await asyncio.wait_for(asyncio.open_connection(*_split(UPSTREAM)), timeout=5)
    except (OSError, asyncio.TimeoutError):
        _unavailable(client_w)
        await client_w.drain()
        client_w.close()
        return
    up_w.write(new_head)
    await up_w.drain()
    if upgrade:
        await asyncio.gather(_pipe(client_r, up_w), _pipe(up_r, client_w))
        return
    # plain HTTP (/json, /json/version, …): buffer, rewrite ws URLs, fix length
    try:
        resp = await asyncio.wait_for(up_r.readuntil(b"\r\n\r\n"), timeout=30)
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError, ConnectionError):
        up_w.close()
        client_w.close()
        return
    resp_head, sep = resp[:-4], b"\r\n\r\n"
    length = None
    for ln in resp_head.split(b"\r\n")[1:]:
        k, _, v = ln.partition(b":")
        if k.strip().lower() == b"content-length":
            length = int(v.strip() or 0)
    body = await (up_r.readexactly(length) if length is not None else up_r.read())
    up_w.close()
    body = rewrite_body(body, UPSTREAM, client_host)
    lines = [ln for ln in resp_head.split(b"\r\n") if not ln.lower().startswith(b"content-length:")]
    lines.append(b"Content-Length: " + str(len(body)).encode())
    client_w.write(b"\r\n".join(lines) + (sep or b"\r\n\r\n") + body)
    try:
        await client_w.drain()
    finally:
        client_w.close()


async def main() -> None:
    server = await asyncio.start_server(handle, *_split(LISTEN))
    print(f"cdp-proxy: http://{LISTEN} -> {UPSTREAM}", file=sys.stderr, flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
