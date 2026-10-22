"""Delegated identity: only an HMAC-signed, fresh 'acting as' header is trusted."""
import hashlib
import hmac
import time

import fastmcp.server.dependencies as deps

from bsp_mcp import delegated


def _headers(user, key, ts=None, sig=None):
    ts = str(int(time.time()) if ts is None else ts)
    sig = sig or hmac.new(key.encode(), f"{user}|{ts}".encode(), hashlib.sha256).hexdigest()
    return {"x-caller": user, "x-caller-ts": ts, "x-caller-sig": sig}


def test_signature_rules(monkeypatch):
    monkeypatch.setenv("MCP_DELEGATION_KEY", "k" * 48)
    cur = {}
    monkeypatch.setattr(deps, "get_http_headers", lambda include_all=False: cur)
    cur.update(_headers("data.analyst@example.com", "k" * 48))
    assert delegated.caller() == "data.analyst@example.com"
    cur.update(_headers("someone.else@example.com", "wrong-key"))
    assert delegated.caller() == "", "forged signature rejected"
    cur.clear(); cur.update(_headers("data.analyst@example.com", "k" * 48, ts=int(time.time()) - 3600))
    assert delegated.caller() == "", "stale timestamp rejected"
    monkeypatch.delenv("MCP_DELEGATION_KEY")
    cur.clear(); cur.update(_headers("data.analyst@example.com", "k" * 48))
    assert delegated.caller() == "", "no key configured → never delegated"


def test_wants_self():
    me = "data.analyst@example.com"
    assert delegated.wants_self(None, me) and delegated.wants_self("me", me) and delegated.wants_self(me.upper(), me)
    assert not delegated.wants_self("billing-disputes@example.net", me)
