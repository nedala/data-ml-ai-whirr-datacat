"""Expose the appliance browser's DevTools (CDP) at http://127.0.0.1:9222
inside a workload container — the same address Claude uses on the laptop.

A plain TCP forward to the browser service (CDP_UPSTREAM, default
browser:9223). Because bytes pass through untouched, clients send
`Host: 127.0.0.1:9222`, which Chrome accepts (it rejects host names), and the
webSocketDebuggerUrl it returns (ws://127.0.0.1:9222/devtools/...) points
back at this forward. Loopback only: nothing outside the container can use it.

    python /opt/services/cdp_forward.py        # CDP_LISTEN=127.0.0.1:9222
"""
from __future__ import annotations

import asyncio
import os
import sys

LISTEN = os.environ.get("CDP_LISTEN", "127.0.0.1:9222")
UPSTREAM = os.environ.get("CDP_UPSTREAM", "browser:9223")


def _split(addr: str) -> tuple[str, int]:
    host, _, port = addr.rpartition(":")
    return host, int(port)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while data := await reader.read(65536):
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        pass
    finally:
        writer.close()


async def _handle(client_r: asyncio.StreamReader, client_w: asyncio.StreamWriter) -> None:
    host, port = _split(UPSTREAM)
    try:
        up_r, up_w = await asyncio.wait_for(asyncio.open_connection(host, port), timeout=5)
    except (OSError, asyncio.TimeoutError):
        # browser capability not running: answer like the facade's CDP port does
        body = b'{"error":"browser not running","hint":"start the Desktop capability in Studio"}'
        client_w.write(b"HTTP/1.1 503 Service Unavailable\r\nContent-Type: application/json\r\n"
                       b"Content-Length: " + str(len(body)).encode() + b"\r\nConnection: close\r\n\r\n" + body)
        await client_w.drain()
        client_w.close()
        return
    await asyncio.gather(_pipe(client_r, up_w), _pipe(up_r, client_w))


async def main() -> None:
    host, port = _split(LISTEN)
    server = await asyncio.start_server(_handle, host, port)
    print(f"cdp-forward: http://{LISTEN} -> {UPSTREAM}", file=sys.stderr, flush=True)
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
