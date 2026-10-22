"""OIDC: id_token signature/claims validation against a JWKS, and the full
login → callback → session flow (token endpoint and JWKS mocked)."""

import os
import time
import urllib.parse

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient
from jose import jwk, jwt

import oidc
import settings

TENANT, CLIENT = "tenant-123", "client-abc"
KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
OTHER = rsa.generate_private_key(public_exponent=65537, key_size=2048)
FETCHES = {"n": 0}


def pem(k):
    return k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())


def public_jwk(k, kid):
    d = jwk.construct(pem(k).decode(), "RS256").public_key().to_dict()
    return {**{a: (b.decode() if isinstance(b, bytes) else b) for a, b in d.items()}, "kid": kid, "use": "sig"}


def token(nonce="n1", key=KEY, kid="k1", **over):
    now = int(time.time())
    claims = {"iss": f"https://login.microsoftonline.com/{TENANT}/v2.0", "aud": CLIENT, "iat": now, "nbf": now,
              "exp": now + 600, "nonce": nonce, "preferred_username": "Jane.Doe@DataPlatform.com", "groups": ["g-admins"], **over}
    return jwt.encode(claims, pem(key).decode(), algorithm="RS256", headers={"kid": kid})


def fake_jwks():
    FETCHES["n"] += 1
    return [public_jwk(KEY, "k1")]


def test_setup():
    os.environ.update(SSO_PROVIDER="oidc", OIDC_TENANT_ID=TENANT, OIDC_CLIENT_ID=CLIENT, OIDC_CLIENT_SECRET="s",
                      OIDC_REDIRECT_URI="https://localhost:31115/studio/oidc/callback", OIDC_ADMIN_GROUP="g-admins")
    settings.reload()
    oidc.fetch_jwks = fake_jwks


def test_valid_token_and_identity():
    claims = oidc.validate_id_token(token(), "n1")
    assert oidc.identity(claims) == ("jane.doe@example.com", "admin")
    assert oidc.identity({**claims, "groups": ["other"]})[1] == "analyst"
    assert oidc.identity({**claims, "groups": [], "roles": ["g-admins"]})[1] == "admin", "app roles count too"
    os.environ["OIDC_ADMIN_USERS"] = " Sam.Lee@DataPlatform.com , other@example.com"
    settings.reload()
    sam = {**claims, "groups": [], "preferred_username": "sam.lee@example.com"}
    assert oidc.identity(sam) == ("sam.lee@example.com", "admin"), "allowlisted UPN is admin"
    assert oidc.identity({**sam, "preferred_username": "kim@example.com"})[1] == "analyst"
    os.environ.pop("OIDC_ADMIN_USERS")
    settings.reload()


def rejects(tok, nonce="n1", why=""):
    try:
        oidc.validate_id_token(tok, nonce)
    except oidc.OIDCError:
        return
    raise AssertionError(f"accepted: {why}")


def test_rejections():
    rejects(token(key=OTHER), why="signed by a different key")
    rejects(token(aud="someone-else"), why="wrong audience")
    rejects(token(iss="https://login.microsoftonline.com/evil/v2.0"), why="wrong issuer")
    rejects(token(exp=int(time.time()) - 60), why="expired")
    rejects(token(), nonce="other", why="nonce mismatch")
    parts = token().split(".")
    rejects(".".join([parts[0], parts[1][:-2] + "AA", parts[2]]), why="tampered payload")
    hs = jwt.encode({"aud": CLIENT}, "secret", algorithm="HS256", headers={"kid": "k1"})
    rejects(hs, why="HS256")


def test_unknown_kid_refetches_once():
    before = FETCHES["n"]
    rejects(token(kid="rotated"), why="unknown kid")
    assert FETCHES["n"] - before == 1


def test_full_flow():
    import app as studio
    sent = {}

    def exchange(code):
        sent["code"] = code
        return {"id_token": token(nonce=sent["nonce"]), "access_token": "at"}
    oidc.exchange_code = exchange
    with TestClient(studio.app, base_url="https://testserver") as c:
        state = c.get("/studio/api/auth/state").json()
        assert state["sso"] and state["setup_needed"], "dev still offers local setup alongside SSO"
        r = c.get("/studio/oidc/login?next=/superset/", follow_redirects=False)
        assert r.status_code == 302
        loc = urllib.parse.urlparse(r.headers["location"])
        q = dict(urllib.parse.parse_qsl(loc.query))
        assert loc.netloc == "login.microsoftonline.com" and q["client_id"] == CLIENT and q["scope"] == "openid profile email"
        sent["nonce"] = q["nonce"]
        bad = c.get("/studio/oidc/callback?code=x&state=forged", follow_redirects=False)
        assert bad.status_code == 303 and bad.headers["location"].startswith("/studio/login?error=")
        r = c.get(f"/studio/oidc/callback?code=abc&state={q['state']}", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/superset/", r.headers
        assert sent["code"] == "abc"
        authz = c.get("/studio/authz", headers={"X-Required-Role": "admin"})
        assert authz.status_code == 204 and authz.headers["x-auth-user"] == "jane.doe@example.com"
        me = c.get("/studio/api/me").json()
        assert me["user"]["source"] == "sso" and me["user"]["role"] == "admin"


def test_loopback_callback_redirects_to_facade():
    """Entra ignores localhost ports, so an http://localhost:<port>/auth/callback
    redirect URI works with an already-registered one; Studio must then send
    the browser back to the https facade with absolute URLs, and keep the
    state cookie host-wide so it reaches /auth/callback."""
    import app as studio

    os.environ["OIDC_REDIRECT_URI"] = "http://localhost:31211/auth/callback"
    settings.reload()
    try:
        with TestClient(studio.app, base_url="https://testserver") as c:
            r = c.get("/studio/oidc/login", follow_redirects=False)
            q = dict(urllib.parse.parse_qsl(urllib.parse.urlparse(r.headers["location"]).query))
            assert q["redirect_uri"] == "http://localhost:31211/auth/callback"
            assert "Path=/;" in r.headers["set-cookie"] or r.headers["set-cookie"].rstrip().endswith("Path=/")
            bad = c.get("/studio/oidc/callback?code=x&state=forged", follow_redirects=False)
            assert bad.status_code == 303
            assert bad.headers["location"].startswith(f"https://localhost:{settings.cfg.facade_port}/studio/login?error=")
    finally:
        os.environ["OIDC_REDIRECT_URI"] = "https://localhost:31115/studio/oidc/callback"
        settings.reload()
