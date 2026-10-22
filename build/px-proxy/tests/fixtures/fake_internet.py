"""Stand-in for "the internet" in the px-proxy e2e test.

* HTTPS :443 — certificate for pypi.org, files.pythonhosted.org, evil.example.com,
  notallowed.example.net signed by a FAKE "Corporate Root CA" (simulates
  a TLS-inspecting proxy re-signing every site; px-proxy must trust it via CORPORATE_CA_DIR)
* HTTP  :80  — same handler
* TCP   :1433 — echo server (stand-in for SQL Server behind a tunnel)

Usage: python fake_internet.py <certdir>   (certdir has server.pem + server.key)
"""

import asyncio
import json
import ssl
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import threading


class H(BaseHTTPRequestHandler):
    def do_GET(self):
        body = json.dumps({"fake_internet": True, "host": self.headers.get("Host"), "path": self.path}).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        sys.stdout.write("inet: " + (a[0] % a[1:]) + "\n")
        sys.stdout.flush()


def serve(port, tls_dir=None):
    srv = ThreadingHTTPServer(("0.0.0.0", port), H)
    if tls_dir:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(f"{tls_dir}/server.pem", f"{tls_dir}/server.key")
        srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    threading.Thread(target=srv.serve_forever, daemon=True).start()


async def echo(r, w):
    w.write(b"SQLSERVER-STANDIN READY\n")
    await w.drain()
    while data := await r.read(4096):
        w.write(b"echo:" + data)
        await w.drain()
    w.close()


async def main():
    serve(443, sys.argv[1])
    serve(80)
    srv = await asyncio.start_server(echo, "0.0.0.0", 1433)
    print("fake internet up: 443/80/1433", flush=True)
    await srv.serve_forever()


asyncio.run(main())
