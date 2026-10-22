"""Stand-in for the host's genotrance/px (or a corporate proxy) in the e2e test.

Minimal HTTP proxy on :3128: CONNECT host:port tunnels (any port) and
absolute-form plain-HTTP forwarding. Logs every request so the test can prove
px-proxy chained through it.
"""

import asyncio
import sys
from urllib.parse import urlsplit


async def pipe(r, w):
    try:
        while data := await r.read(65536):
            w.write(data)
            await w.drain()
    except ConnectionError:
        pass
    finally:
        w.close()


async def handle(cr, cw):
    line = await cr.readline()
    headers = []
    while (h := await cr.readline()) not in (b"\r\n", b"\n", b""):
        headers.append(h)
    try:
        method, target, ver = line.decode().split()
    except ValueError:
        cw.close()
        return
    print(f"upstream-proxy: {method} {target}", flush=True)
    try:
        if method == "CONNECT":
            host, port = target.rsplit(":", 1)
            tr, tw = await asyncio.open_connection(host, int(port))
            cw.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await cw.drain()
        else:
            u = urlsplit(target)
            tr, tw = await asyncio.open_connection(u.hostname, u.port or 80)
            path = u.path + (f"?{u.query}" if u.query else "")
            tw.write(f"{method} {path or '/'} {ver}\r\n".encode() + b"".join(headers) + b"\r\n")
            await tw.drain()
    except OSError as e:
        cw.write(f"HTTP/1.1 502 Bad Gateway\r\n\r\n{e}".encode())
        cw.close()
        return
    await asyncio.gather(pipe(cr, tw), pipe(tr, cw))


async def main():
    srv = await asyncio.start_server(handle, "0.0.0.0", int(sys.argv[1]) if len(sys.argv) > 1 else 3128)
    print("fake upstream proxy up", flush=True)
    await srv.serve_forever()


asyncio.run(main())
