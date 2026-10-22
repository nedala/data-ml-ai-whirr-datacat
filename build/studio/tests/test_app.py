"""HTTP surface: setup, login, authz, CSRF, throttling, step-up, users,
credentials, backup round-trip, certificates, audit."""

import json
import os

from fastapi.testclient import TestClient

import app as studio
import auth
import settings
import users
from vault import store

H = {"X-Studio": "1"}
ADMIN, ADMIN_PW, MASTER = "seshu", "Tr0ub4dour-and-3", "vault master passphrase 1"
C: dict[str, TestClient] = {}


def client() -> TestClient:
    return TestClient(studio.app, base_url="https://testserver")


def test_boot_redirects_to_setup():
    c = C["admin"] = client()
    c.__enter__()   # run lifespan (plugins, dev cert)
    r = c.get("/studio/", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/studio/setup"
    assert c.get("/studio/login", follow_redirects=False).headers["location"] == "/studio/setup"
    assert c.get("/studio/setup").status_code == 200
    assert "Content-Security-Policy" in c.get("/studio/setup").headers
    assert c.get("/studio/authz").status_code == 401


def test_setup_creates_admin_and_vault():
    c = C["admin"]
    r = c.post("/studio/api/auth/setup", json={"username": ADMIN, "password": ADMIN_PW, "master": MASTER})
    assert r.status_code == 403, "CSRF header required"
    r = c.post("/studio/api/auth/setup", json={"username": ADMIN, "password": ADMIN_PW, "master": MASTER}, headers=H)
    assert r.status_code == 200, r.text
    cookie = r.headers["set-cookie"].lower()
    assert "httponly" in cookie and "secure" in cookie and "samesite=lax" in cookie and "max-age=28800" in cookie
    assert store.status() == "unlocked"
    assert users.get(ADMIN)["hash"].startswith("scrypt$")
    assert c.post("/studio/api/auth/setup", json={"username": "x", "password": ADMIN_PW, "master": MASTER}, headers=H).status_code == 409
    assert c.get("/studio/").status_code == 200


def test_authz_contract():
    c = C["admin"]
    r = c.get("/studio/authz")
    assert r.status_code == 204 and r.headers["x-auth-user"] == ADMIN and r.headers["x-auth-role"] == "admin"
    assert r.headers["x-auth-email"] == f"{ADMIN}@local" and r.headers["x-auth-name"] == ADMIN
    assert c.get("/studio/authz", headers={"X-Required-Role": "admin"}).status_code == 204
    anon = client()
    assert anon.get("/studio/authz").status_code == 401
    anon.cookies.set(auth.COOKIE, "forged.cookie.value", domain="testserver")
    assert anon.get("/studio/authz").status_code == 401


def test_viewer_gets_403_for_admin_floor():
    c = C["admin"]
    assert c.post("/studio/api/users", json={"name": "vic", "role": "viewer", "password": "viewer-pass-1"}, headers=H).status_code == 200
    v = C["viewer"] = client()
    assert v.post("/studio/api/auth/login", json={"username": "vic", "password": "viewer-pass-1"}, headers=H).status_code == 200
    assert v.get("/studio/authz").status_code == 204
    assert v.get("/studio/authz", headers={"X-Required-Role": "analyst"}).status_code == 403
    assert v.get("/studio/authz", headers={"X-Required-Role": "admin"}).status_code == 403
    assert v.get("/studio/api/credentials").status_code == 403
    me = v.get("/studio/api/me").json()
    assert {p["id"] for p in me["pages"]} >= {"launchpad", "compose", "services"}
    assert "credentials" not in {p["id"] for p in me["pages"]}
    assert v.post("/studio/api/compose/apply", json={"personas": ["data-pro"]}, headers=H).status_code == 403


def test_role_change_applies_without_relogin():
    C["admin"].post("/studio/api/users/vic/role", json={"role": "analyst"}, headers=H)
    assert C["viewer"].get("/studio/authz", headers={"X-Required-Role": "analyst"}).status_code == 204


def test_password_reset_and_removal_kill_sessions():
    C["admin"].post("/studio/api/users/vic/password", json={"password": "another-pass-2"}, headers=H)
    assert C["viewer"].get("/studio/authz").status_code == 401
    v = client()
    v.post("/studio/api/auth/login", json={"username": "vic", "password": "another-pass-2"}, headers=H)
    assert v.get("/studio/authz").status_code == 204
    assert C["admin"].delete("/studio/api/users/vic", headers=H).status_code == 200
    assert v.get("/studio/authz").status_code == 401


def test_last_admin_protected():
    c = C["admin"]
    assert c.delete(f"/studio/api/users/{ADMIN}", headers=H).status_code == 400
    r = c.post(f"/studio/api/users/{ADMIN}/role", json={"role": "viewer"}, headers=H)
    assert r.status_code == 400


def test_logout_revokes_cookie():
    c = client()
    c.post("/studio/api/auth/login", json={"username": ADMIN, "password": ADMIN_PW}, headers=H)
    token = c.cookies.get(auth.COOKIE)
    assert c.post("/studio/api/auth/logout", headers=H).status_code == 200
    replay = client()
    replay.cookies.set(auth.COOKIE, token, domain="testserver")
    assert replay.get("/studio/authz").status_code == 401, "logged-out session cannot be replayed"


def test_login_rate_limit():
    c = client()
    codes = [c.post("/studio/api/auth/login", json={"username": "bob", "password": "wrong"}, headers=H).status_code for _ in range(6)]
    assert codes[:5] == [400] * 5 and codes[5] == 429, codes
    auth.user_limiter.reset("u:bob")
    auth.ip_limiter.reset("ip:testclient")


def test_step_up_gates_reveal():
    c = C["admin"]
    assert c.post("/studio/api/compose/plan", json={"personas": ["data-pro"]}, headers=H).status_code == 200
    store.set("services.code.CODE_PASSWORD", "code-secret-value")
    r = c.post("/studio/api/credentials/reveal", json={"service": "code", "env": "CODE_PASSWORD"}, headers=H)
    assert r.status_code == 428 and r.json()["step_up"] is True
    assert c.post("/studio/api/auth/step-up", json={"password": "nope"}, headers=H).status_code == 400
    r = c.post("/studio/api/auth/step-up", json={"password": MASTER}, headers=H)
    assert r.status_code == 200 and r.json()["step_up_seconds"] > 290
    r = c.post("/studio/api/credentials/reveal", json={"service": "code", "env": "CODE_PASSWORD"}, headers=H)
    assert r.json() == {"value": "code-secret-value"}
    assert c.post("/studio/api/credentials/reveal", json={"service": "code", "env": "NOPE"}, headers=H).status_code == 400


def test_credentials_listing():
    slots = C["admin"].get("/studio/api/credentials").json()["slots"]
    code = next(s for s in slots if s["env"] == "CODE_PASSWORD")
    assert code["set"] and code["user"] == "analyst" and "value" not in code
    key = next(s for s in slots if s["env"] == "SUPERSET_SECRET_KEY")
    assert key["service"] == "superset" and key["hidden"] and key["length"] == 48 and not key["set"]
    assert sorted(s["env"] for s in slots if s["hidden"]) == ["SUPERSET_SECRET_KEY", "WB_STUDIO_TOKEN"]
    assert {s["env"] for s in slots if s["kind"] == "service" and not s["shared"]} == {
        "PG_PASS", "JUPYTER_TOKEN", "CODE_PASSWORD", "VNC_PASSWORD", "SUPERSET_ADMIN_PASS", "SUPERSET_SECRET_KEY",
        "WB_STUDIO_TOKEN"}
    ext = {s["env"] for s in slots if s.get("external")}
    assert ext == {"GCP_SA_JSON", "M365_CLIENT_SECRET", "ATLAN_API_TOKEN", "DATAHUB_TOKEN", "SAP_BRIM_USER", "SAP_BRIM_PASSWORD", "ANTHROPIC_API_KEY"}
    assert all(s["service"] == "shared" for s in slots if s.get("external"))


def test_locked_vault_maps_to_423_and_unlock():
    c = C["admin"]
    assert c.post("/studio/api/vault/lock", headers=H).json()["vault"] == "locked"
    assert c.get("/studio/api/credentials").status_code == 423
    assert c.post("/studio/api/vault/unlock", json={"password": "bad"}, headers=H).status_code == 400
    assert c.post("/studio/api/vault/unlock", json={"password": MASTER}, headers=H).json()["vault"] == "unlocked"


def test_compose_endpoints_without_docker():
    c = C["admin"]
    cat = c.get("/studio/api/compose/catalog").json()
    assert [p["id"] for p in cat["personas"]] == ["data-pro", "ml-scientist", "ai-engineer"] and cat["can_apply"]
    assert cat["default_persona"] == "data-pro" and cat["appliance_name"] == "Data Professional"
    assert cat["personas"][0]["capabilities"] == ["workspace", "vscode", "trino", "bi", "connectors"]
    assert cat["personas"][0]["missing"] == ["canvas"], "persona capability without a fragment is reported, not fatal"
    plan = c.post("/studio/api/compose/plan", json={"capabilities": ["bi"]}, headers=H).json()
    assert plan["capabilities"]["workspace"]["state"] == "implied"
    assert set(plan["start"]) == {"superset", "appliance", "postgres"} and plan["stop"] == []
    assert c.post("/studio/api/compose/plan", json={"personas": ["nope"]}, headers=H).status_code == 400
    plan = c.post("/studio/api/compose/plan", json={"personas": ["ai-engineer"]}, headers=H).json()
    assert {"ai", "rag", "chatui", "disputes", "ccai"} <= {h.get("capability") for h in plan["hints"]}
    state = c.get("/studio/api/auth/state").json()
    assert state["appliance"]["name"] == "Data Professional"
    lp = c.get("/studio/api/launchpad").json()
    assert {a["id"] for a in lp["apps"]} >= {"jupyter", "superset"}
    jup = next(a for a in lp["apps"] if a["id"] == "jupyter")
    assert jup["cred"]["service"] == "appliance" and jup["capability"] == "workspace"
    assert lp["persona"] == "data-pro" and [g["id"] for g in lp["groups"]][:3] == ["data", "analyse", "report"]
    desk = next(a for a in lp["apps"] if a["id"] == "browser")
    assert desk["title"] == "Linux Desktop" and desk["alt"]["url"].endswith("/vnc.html?autoconnect=1&resize=scale&reconnect=1")
    states = {h["id"]: h["state"] for h in lp["health"] if "id" in h}
    assert states["vault"] == "ok" and states["tls"] == "ok"
    svcs = {s["name"]: s for s in c.get("/studio/api/services").json()["services"]}
    assert list(svcs)[0] == "nginx"
    assert any(e["value"].startswith("jdbc:postgresql://") for e in svcs["postgres"]["endpoints"])
    assert "rstudio" not in svcs
    assert lp["connect"] == [], "endpoints only for running services"


def test_backup_round_trip(project):
    c = C["admin"]
    (settings.cfg.data / "egress").mkdir(parents=True, exist_ok=True)
    (settings.cfg.data / "egress" / "allowlist.yaml").write_text("mode: enforce\nrules: []\n")
    assert c.post("/studio/api/backup/export", json={"passphrase": "short"}, headers=H).status_code == 400
    r = c.post("/studio/api/backup/export", json={"passphrase": "backup passphrase 42"}, headers=H)
    assert r.status_code == 200, r.text
    assert r.headers["content-disposition"].endswith('.bspconfig"')
    blob = r.content
    env = json.loads(blob)
    assert env["format"] == "bsp-appliance-config" and env["v"] == 1 and env["kdf"] == "scrypt"
    assert b"code-secret-value" not in blob and b"enforce" not in blob

    # mutate, then restore
    store.set("services.code.CODE_PASSWORD", "changed-after-backup")
    store.set("services.extra.NEW_ONE", "x")
    bad = c.post("/studio/api/backup/preview", files={"file": ("b.bspconfig", blob)}, data={"passphrase": "wrong passphrase!"}, headers=H)
    assert bad.status_code == 400
    r = c.post("/studio/api/backup/preview", files={"file": ("b.bspconfig", blob)}, data={"passphrase": "backup passphrase 42"}, headers=H)
    assert r.status_code == 200, r.text
    diff = r.json()["diff"]
    assert "services.code.CODE_PASSWORD" in diff["vault"]["changed"]
    assert "services.extra.NEW_ONE" in diff["vault"]["removed"]
    assert diff["packages"] == [] and diff["users"]["keeps_you"], "workspace not running → no manifest"
    assert "egress" in diff["plugins"] and diff["egress"]["present"] is False, "allowlist exported once, by the egress plugin"
    assert "changed-after-backup" not in r.text and "code-secret-value" not in r.text, "preview never returns values"
    token = r.json()["token"]
    r = c.post("/studio/api/backup/apply", json={"token": token, "sections": ["vault", "packages"]}, headers=H)
    assert r.status_code == 409, "unlocked vault needs explicit confirmation"
    r = c.post("/studio/api/backup/apply", json={"token": token, "sections": ["vault", "packages", "selection", "users", "egress"], "confirm_overwrite": True}, headers=H)
    assert r.status_code == 200, r.text
    assert store.get("services.code.CODE_PASSWORD") == "code-secret-value"
    assert store.get("services.extra") is None
    assert c.post("/studio/api/backup/apply", json={"token": token, "sections": ["vault"], "confirm_overwrite": True}, headers=H).status_code == 400, "token single-use"


def test_certificates_dev():
    c = C["admin"]
    info = c.get("/studio/api/certs").json()
    assert info["issued_by_appliance_ca"] and "localhost" in info["server"]["sans"] and "127.0.0.1" in info["server"]["sans"]
    assert 390 <= info["server"]["days_left"] <= 397
    key = settings.cfg.vault_dir / "ca-key.pem"
    assert key.exists() and oct(key.stat().st_mode & 0o777) == "0o600"
    assert oct((settings.cfg.data / "tls" / "server.key").stat().st_mode & 0o777) == "0o600"
    pem = client().get("/studio/ca.pem")
    assert pem.status_code == 200 and pem.text.startswith("-----BEGIN CERTIFICATE-----"), "CA download is public"
    r = c.post("/studio/api/certs/reissue", json={"hostnames": ["localhost", "appliance.example.com", "10.1.2.3"]}, headers=H)
    assert r.status_code == 200 and "appliance.example.com" in r.json()["server"]["sans"]
    assert "reload" in r.json()


def test_certificate_upload_validation(project):
    import certs
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    import datetime as dt
    key = ec.generate_private_key(ec.SECP256R1())
    other = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(x509.oid.NameOID.COMMON_NAME, "corp.example")])
    now = dt.datetime.now(dt.timezone.utc)
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(1).not_valid_before(now).not_valid_after(now + dt.timedelta(days=90))
            .add_extension(x509.SubjectAlternativeName([x509.DNSName("corp.example")]), False).sign(key, hashes.SHA256()))
    pem = cert.public_bytes(serialization.Encoding.PEM)
    kpem = lambda k: k.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())  # noqa: E731
    c = C["admin"]
    r = c.post("/studio/api/certs/upload", files={"cert": ("c.pem", pem), "key": ("k.pem", kpem(other))}, headers=H)
    assert r.status_code == 400 and "does not match" in r.json()["detail"]
    r = c.post("/studio/api/certs/upload", files={"cert": ("c.pem", b"junk"), "key": ("k.pem", kpem(key))}, headers=H)
    assert r.status_code == 400
    r = c.post("/studio/api/certs/upload", files={"cert": ("c.pem", pem), "key": ("k.pem", kpem(key))}, headers=H)
    assert r.status_code == 200, r.text
    assert any("localhost" in w for w in r.json()["warnings"]), "SAN mismatch is a warning"
    assert certs.describe()["server"]["sans"] == ["corp.example"]


def test_audit_log_records_and_filters():
    c = C["admin"]
    entries = c.get("/studio/api/audit").json()["entries"]
    events = {e["event"] for e in entries}
    assert {"auth.setup", "auth.login", "auth.step_up", "credential.reveal", "backup.export", "backup.import", "vault.lock", "user.add"} <= events
    assert all(e["event"].startswith("auth.") for e in c.get("/studio/api/audit?event=auth.").json()["entries"])
    assert all("vic" in json.dumps(e) for e in c.get("/studio/api/audit?q=vic").json()["entries"])
    assert MASTER not in (settings.cfg.vault_dir / "audit.log").read_text()


def test_prod_disables_local_login():
    os.environ["APPLIANCE_ENV"] = "prod"
    settings.reload()
    try:
        assert C["admin"].get("/studio/authz").status_code == 401, "existing local sessions stop working"
        r = client().post("/studio/api/auth/login", json={"username": ADMIN, "password": ADMIN_PW}, headers=H)
        assert r.status_code == 403
        assert client().get("/studio/api/auth/state").json()["local_login"] is False
        os.environ["STUDIO_BREAK_GLASS"] = "1"
        settings.reload()
        bg = client()
        assert bg.post("/studio/api/auth/login", json={"username": ADMIN, "password": ADMIN_PW}, headers=H).status_code == 200
        assert bg.get("/studio/api/auth/state").json()["break_glass"] is True
    finally:
        os.environ["APPLIANCE_ENV"] = "dev"
        os.environ.pop("STUDIO_BREAK_GLASS", None)
        settings.reload()


def test_safe_next():
    from routes_auth import safe_next
    assert safe_next("/superset/") == "/superset/"
    for bad in ("//evil.com", "https://evil.com", "/\\evil.com", "", None, "/studio/login?x"):
        assert safe_next(bad) == "/studio/", bad
