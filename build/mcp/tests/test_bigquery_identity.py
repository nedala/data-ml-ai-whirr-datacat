"""BigQuery tools: the optional `identity` argument (principal switch)."""
from __future__ import annotations

import pytest


def test_named_identity_needs_verified_caller(monkeypatch):
    from bsp_mcp import delegated
    from bsp_mcp.common import UserError
    from bsp_mcp.tools import bigquery as T

    monkeypatch.setattr(T, "connector_mode", lambda system: "live")
    monkeypatch.setattr(delegated, "caller", lambda: "")
    with pytest.raises(UserError, match="verified caller"):
        with T._acting("data-analyst"):
            pass
    with T._acting("") as who:          # no identity → the assigned one, untouched
        assert who == ""


def test_named_identity_forwards_caller_proof(monkeypatch):
    from bsp_common import gcp
    from bsp_mcp import delegated
    from bsp_mcp.tools import bigquery as T

    seen = {}
    monkeypatch.setattr(T, "connector_mode", lambda system: "live")
    monkeypatch.setattr(delegated, "caller", lambda: "anna")
    monkeypatch.setattr(T, "_caller_proof", lambda: ("1700000000", "sig"))

    def token(identity, user=None, scopes=None, consumer=""):
        seen.update(gcp._acting.get() or {}, identity=identity)
        return {"principal_email": "anna@example.com", "default_project": "p1"}
    monkeypatch.setattr(gcp, "studio_token", token)
    with T._acting("data-analyst", "da-tsa-dv") as who:
        assert who == "anna@example.com on da-tsa-dv"
    assert seen["user"] == "anna" and seen["caller_sig"] == "sig" and seen["identity"] == "data-analyst"

    def refuse(*a, **k):
        raise gcp.StudioTokenError("anna may not act as x", 403)
    monkeypatch.setattr(gcp, "studio_token", refuse)
    from bsp_mcp.common import UserError
    with pytest.raises(UserError, match="may not act"):
        with T._acting("x"):
            pass
