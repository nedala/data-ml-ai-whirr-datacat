"""Who is calling: the Studio-gated user, trusted only from nginx.

nginx sets X-Auth-User after Studio's /studio/authz gate (overwriting anything a
client sent). Other workloads on the internal network can reach the harness
directly, so the header is trusted ONLY when the TCP peer is the nginx
container; otherwise the caller is the anonymous "playground".
"""

from __future__ import annotations

import os
import socket
import time

from fastapi import Request

_facade = {"ips": set(), "at": 0.0}


def _from_facade(request: Request) -> bool:
    if time.time() - _facade["at"] > 60:
        try:
            _facade["ips"] = set(socket.gethostbyname_ex(os.environ.get("FACADE_HOST", "nginx"))[2])
        except OSError:
            _facade["ips"] = set()
        _facade["at"] = time.time()
    return bool(request.client) and request.client.host in _facade["ips"]


def user_of(request: Request) -> str:
    u = (request.headers.get("x-auth-user") or "").strip().lower()
    return u if u and _from_facade(request) else "playground"
