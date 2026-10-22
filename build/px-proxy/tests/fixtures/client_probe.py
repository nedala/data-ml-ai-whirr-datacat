"""e2e client probes that curl cannot express.

  client_probe.py sni <proxy_host> <connect_host> <sni_host>
      CONNECT to connect_host:443, then TLS with a DIFFERENT SNI; prints the
      HTTP status line of GET / (expect 403 sni-mismatch).
  client_probe.py tunnel <host> <port> <message>
      raw TCP; prints whatever comes back (or CLOSED).
  client_probe.py requests <url>
      python-requests with the container's proxy/CA env; prints status + body.
"""

import socket
import ssl
import sys


def sni(proxy, connect_host, sni_host):
    s = socket.create_connection((proxy, 3128), timeout=10)
    s.sendall(f"CONNECT {connect_host}:443 HTTP/1.1\r\nHost: {connect_host}:443\r\n\r\n".encode())
    resp = s.recv(4096).decode(errors="replace")
    print("CONNECT ->", resp.splitlines()[0] if resp else "nothing")
    if " 200" not in resp.splitlines()[0]:
        return
    ctx = ssl.create_default_context(cafile="/trust/ca-bundle.pem")
    t = ctx.wrap_socket(s, server_hostname=sni_host)
    t.sendall(f"GET / HTTP/1.1\r\nHost: {sni_host}\r\nConnection: close\r\n\r\n".encode())
    data = b""
    while chunk := t.recv(4096):
        data += chunk
    text = data.decode(errors="replace")
    print("GET ->", text.splitlines()[0] if text else "nothing")
    print([line for line in text.splitlines() if line.lower().startswith("x-egress-reason")])


def tunnel(host, port, msg):
    s = socket.create_connection((host, int(port)), timeout=10)
    s.settimeout(5)
    out = b""
    try:
        s.sendall(msg.encode() + b"\n")
        while True:
            chunk = s.recv(4096)
            if not chunk:
                break
            out += chunk
            if b"echo:" in out:
                break
    except (socket.timeout, ConnectionError, OSError):
        pass
    print(out.decode(errors="replace").replace("\n", " | ") if out else "CLOSED")


def req(url):
    import requests

    r = requests.get(url, timeout=15)
    print(r.status_code, r.text[:200].replace("\n", " "))


if __name__ == "__main__":
    cmd, *args = sys.argv[1:]
    {"sni": sni, "tunnel": tunnel, "requests": req}[cmd](*args)
