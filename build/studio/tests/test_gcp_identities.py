"""Cloud identities: CRUD never leaks a key, validation (service_account and
authorized_user), migration of GCP_SA_JSON, consumer registry + fallback,
assignment → secret_env vars, live checks against a mocked Google, and the
Sign in with Google session plumbing."""

import importlib.util
import json
import os
import re
from pathlib import Path

from fastapi.testclient import TestClient

import app as studio
import composefile
import dockerops
import gcp_identities as gi
import orchestrator
import settings
from vault import store

H = {"X-Studio": "1"}
ADMIN, ADMIN_PW, MASTER = "seshu", "Tr0ub4dour-and-3", "vault master passphrase 1"
C: dict[str, TestClient] = {}
SECRET_MARKERS = ("PRIVATE KEY", "refresh-token-SECRET", "client-secret-SECRET", "private_key", "refresh_token",
                  "client_secret")


def _rsa_pem() -> str:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric import rsa

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption()).decode()


PEM = _rsa_pem()
SA = {"type": "service_account", "project_id": "proj-sa", "private_key_id": "abc123", "private_key": PEM,
      "client_email": "runner@proj-sa.iam.gserviceaccount.com", "client_id": "1234567890",
      "token_uri": "https://oauth2.googleapis.com/token"}
USER = {"type": "authorized_user", "client_id": "764086051850-x.apps.googleusercontent.com",
        "client_secret": "client-secret-SECRET", "refresh_token": "refresh-token-SECRET", "quota_project_id": "proj-user"}


def no_leak(text: str) -> None:
    for m in SECRET_MARKERS:
        assert m not in text, f"response leaks {m!r}"


# ── a fake Google ────────────────────────────────────────────────────────
CALLS: list[tuple[str, str, dict]] = []
DENY = {"bq_query", "gcs_buckets"}      # these answer 403


def fake_http(method, url, *, headers=None, data=None, json_body=None, params=None, timeout=10, raw=None):
    CALLS.append((method, url, data or {}))
    if url.endswith("/token"):
        if data.get("grant_type") == "refresh_token":
            if data.get("refresh_token") != USER["refresh_token"]:
                return 400, {"error": "invalid_grant", "error_description": "Token has been expired or revoked."}
            return 200, {"access_token": "ya29.user", "expires_in": 3599}
        assert data["grant_type"].endswith("jwt-bearer") and data["assertion"].count(".") == 2
        return 200, {"access_token": "ya29.sa", "expires_in": 3599}
    if "/projects/nobq/" in url:
        return 400, {"error": {"code": 400, "message": "The project nobq has not enabled BigQuery."}}
    if "tokeninfo" in url:
        return 200, {"email": "seshu@example.com", "scope": "openid email cloud-platform"}
    if "/datasets" in url and method == "GET":
        return 200, {"datasets": [{"datasetReference": {"datasetId": "raw"}}]}
    if "projects:search" in url:
        return 200, {"projects": [{"projectId": "biz-a", "displayName": "Business A", "state": "ACTIVE"},
                                  {"projectId": "dev-proj", "displayName": "dev", "state": "ACTIVE"}]}
    if url.endswith("/jobs"):
        if "/projects/dev-proj/" in url:
            return 200, {"status": {"state": "DONE"}}
        if "bq_query" in DENY:
            return 403, {"error": {"code": 403, "status": "PERMISSION_DENIED", "message":
                                   "Access Denied: User does not have bigquery.jobs.create permission in project proj-sa."}}
        return 200, {"status": {"state": "DONE"}}
    if url.endswith("/storage/v1/b"):
        if "gcs_buckets" in DENY:
            return 403, {"error": {"code": 403, "message": "x does not have storage.buckets.list access to the Google Cloud project."}}
        return 200, {"items": [{"name": "lake"}]}
    if ":generateContent" in url:
        return 200, {"candidates": [{"content": {"parts": [{"text": "OK"}]}}]}
    if "testIamPermissions" in url:
        return 200, {"permissions": ["bigquery.jobs.create"]}
    if "artifactregistry" in url:
        return 403, {"error": {"code": 403, "message": "Permission 'artifactregistry.repositories.list' denied on resource"}}
    if "run.googleapis.com" in url:
        return 403, {"error": {"code": 403, "message": "Run Admin API has not been used in project proj-sa before or it is "
                                                         "disabled.", "status": "PERMISSION_DENIED",
                               "details": [{"reason": "SERVICE_DISABLED"}]}}
    return 404, {"error": {"code": 404, "message": "not found"}}


def test_setup():
    settings.reload()
    gi._http = fake_http
    dockerops.status = lambda max_age=1.5: {}
    os.environ["GCP_REGION"] = "us-east4"
    c = C["admin"] = TestClient(studio.app, base_url="https://testserver")
    c.__enter__()
    r = c.post("/studio/api/auth/setup", json={"username": ADMIN, "password": ADMIN_PW, "master": MASTER}, headers=H)
    assert r.status_code == 200, r.text


def test_registry_matches_compose_and_bsp_common():
    """Every consumer env reaches every workload (compose.base.yml) and
    bsp_common.gcp knows the same consumers / fallbacks."""
    base = (Path(os.environ.get("STUDIO_TEST_REPO", "/repo")) / "compose.base.yml").read_text()
    for c in gi.registry()["consumers"]:
        for var in (c["env"], c["project_env"]):
            assert re.search(rf"^\s+{var}: \$\{{{var}:-\}}", base, re.M), f"{var} missing from compose.base.yml"
        if c["fallback"]:
            assert c["fallback"] in gi.consumers()
    assert "GCP_IDENTITIES: ${GCP_IDENTITIES:-}" in base
    path = Path(os.environ.get("STUDIO_TEST_REPO", "/repo")) / "build/common/bsp_common/gcp.py"
    spec = importlib.util.spec_from_file_location("bsp_gcp", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert {k: v for k, v in mod.CONSUMERS.items()} == {c["key"]: (c["env"], c["fallback"]) for c in gi.registry()["consumers"]}
    C["bsp_gcp"] = mod


def test_validation():
    gi.parse_json(json.dumps(SA))
    gi.parse_json(json.dumps(USER))
    import base64
    assert gi.parse_json(base64.b64encode(json.dumps(USER).encode()).decode())["type"] == "authorized_user"
    bad = [("", "Paste"), ("not json", "Not JSON"), ("[1]", "object"),
           (json.dumps({"type": "external_account"}), "workload identity"),
           (json.dumps({"type": "authorized_user", "client_id": "x"}), "missing client_secret, refresh_token"),
           (json.dumps({**SA, "private_key": "nope"}), "PEM"),
           (json.dumps({**USER, "token_uri": "http://169.254.169.254/token"}), "token_uri"),
           (json.dumps({**USER, "token_uri": "https://evil.example.com/token"}), "token_uri")]
    for text, why in bad:
        try:
            gi.parse_json(text)
        except gi.IdentityError as exc:
            assert why in str(exc), (why, str(exc))
        else:
            raise AssertionError(f"accepted {text[:40]!r}")


def test_migration_imports_shared_key_once():
    store.set("services.shared.GCP_SA_JSON", json.dumps(SA, indent=2))
    store.delete("meta.gcp_identities_migrated")
    notes = gi.migrate(store)
    assert notes and "default service account" in notes[0]
    ids = gi.identities()
    assert list(ids) == ["default-service-account"]
    rec = ids["default-service-account"]
    assert rec["type"] == "service_account" and rec["principal"] == SA["client_email"] and rec["project"] == "proj-sa"
    assert set(gi.assignments().values()) == {"default-service-account"}, "every consumer → the migrated key"
    assert gi.migrate(store) == [], "runs once"
    store.delete("gcp_identities.default-service-account")
    assert gi.migrate(store) == [], "a deleted migrated identity is not re-imported"
    store.set("gcp_identities", {"default-service-account": rec})


def test_list_never_leaks():
    c = C["admin"]
    r = c.get("/studio/api/gcp/identities")
    assert r.status_code == 200, r.text
    no_leak(r.text)
    body = r.json()
    assert body["identities"][0]["principal"] == SA["client_email"]
    assert {g["id"] for g in body["groups"]} == {"ai", "data", "ml", "workspace"}
    keys = [x["key"] for x in body["consumers"]]
    assert keys[:3] == ["vertex", "bigquery_read", "bigquery_write"] and "trino_bigquery" in keys and "cloud_run" in keys
    assert "json" not in body["identities"][0]
    assert C["admin"].get("/studio/api/gcp/identities", headers={}).status_code == 200
    assert c.post("/studio/api/gcp/identities", json={"name": "x", "json": "{}"}).status_code == 403, "CSRF header required"


def test_add_authorized_user_resolves_principal():
    c = C["admin"]
    CALLS.clear()
    r = c.post("/studio/api/gcp/identities", json={"name": "Seshu sign-in", "json": json.dumps(USER)}, headers=H)
    assert r.status_code == 200, r.text
    no_leak(r.text)
    ident = r.json()["identity"]
    assert ident["type"] == "authorized_user" and ident["principal"] == "seshu@example.com"
    assert ident["project"] == "proj-user", "quota_project_id is the default project"
    assert any("tokeninfo" in u for _, u, _ in CALLS)
    stored = store.get(f"gcp_identities.{ident['id']}")
    assert json.loads(stored["json"])["refresh_token"] == USER["refresh_token"], "kept in the vault"
    assert c.post("/studio/api/gcp/identities", json={"name": "seshu SIGN-IN", "json": json.dumps(USER)}, headers=H).status_code == 400
    r = c.post("/studio/api/gcp/identities", json={"name": "bad", "json": json.dumps({"type": "authorized_user"})}, headers=H)
    assert r.status_code == 400 and "missing" in r.json()["detail"]
    # multipart upload path
    r = c.post("/studio/api/gcp/identities", data={"name": "uploaded sa", "project": "other-proj"},
               files={"file": ("key.json", json.dumps(SA).encode(), "application/json")}, headers=H)
    assert r.status_code == 200, r.text
    no_leak(r.text)
    assert r.json()["identity"]["project"] == "other-proj"
    C["user_id"], C["sa2_id"] = ident["id"], r.json()["identity"]["id"]


def test_assignments_and_fallback_to_secret_env():
    c = C["admin"]
    uid, sa2 = C["user_id"], C["sa2_id"]
    # Vertex stays on the migrated key; BigQuery read uses the personal sign-in;
    # BigQuery write inherits read; Trino accepts it too (shared by every
    # appliance user) with its own data-project list + execution project
    r = c.put("/studio/api/gcp/assignments", json={"bigquery_read": uid, "bigquery_write": ""}, headers=H)
    assert r.status_code == 200, r.text
    r = c.put("/studio/api/gcp/assignments", json={"trino_bigquery": {
        "identity": uid, "data_projects": "biz-a, biz-b", "exec_project": "jobs-p"}}, headers=H)
    assert r.status_code == 200, r.text
    env = orchestrator.secret_env()
    assert env["GCP_TRINO_JSON"] == store.get(f"gcp_identities.{uid}.json")
    assert env["GCP_TRINO_DATA_PROJECTS"] == "biz-a,biz-b" and env["GCP_TRINO_PROJECT"] == "biz-a"
    assert env["GCP_TRINO_EXEC_PROJECT"] == "jobs-p"
    assert c.put("/studio/api/gcp/assignments", json={"nope": uid}, headers=H).status_code == 400
    assert c.put("/studio/api/gcp/assignments", json={"gcs": "missing"}, headers=H).status_code == 400
    c.put("/studio/api/gcp/assignments", json={"trino_bigquery": "", "gcs": sa2, "workspace": uid}, headers=H)

    env = orchestrator.secret_env()
    sa_json = store.get("gcp_identities.default-service-account.json")
    user_json = store.get(f"gcp_identities.{uid}.json")
    assert env["GCP_VERTEX_JSON"] == sa_json
    assert env["GCP_BQ_JSON"] == user_json and env["GCP_BQ_PROJECT"] == "proj-user"
    assert env["GCP_BQ_WRITE_JSON"] == user_json, "bigquery_write inherits bigquery_read"
    assert env["GCP_TRINO_JSON"] == user_json, "Trino (unassigned) inherits BigQuery · read, personal sign-ins included"
    assert json.loads(env["GCP_GCS_JSON"])["client_email"] == SA["client_email"] and env["GCP_GCS_PROJECT"] == "other-proj"
    assert env["GCP_MLFLOW_JSON"] == env["GCP_MLFLOW_JSON"] and env["GCP_WORKSPACE_JSON"] == user_json
    assert env["GCP_SA_JSON"] == json.dumps(SA, indent=2), "workspace is personal → the stored shared key is kept"
    ids = json.loads(env["GCP_IDENTITIES"])
    assert set(ids) == {"default service account", "Seshu sign-in", "uploaded sa"} and ids["Seshu sign-in"]["type"] == "authorized_user"
    no_leak(env["GCP_IDENTITIES"])   # names + metadata only: other identities go through Studio's token endpoint
    assert ids["uploaded sa"]["principal"] == SA["client_email"] and ids["Seshu sign-in"]["id"] == uid
    names = json.loads(env["GCP_ASSIGNMENTS"])
    assert names["vertex"] == "default service account" and names["bigquery_write"] == "Seshu sign-in"
    # workspace → a service account ⇒ GCP_SA_JSON follows it
    c.put("/studio/api/gcp/assignments", json={"workspace": sa2}, headers=H)
    assert orchestrator.secret_env()["GCP_SA_JSON"] == store.get(f"gcp_identities.{sa2}.json")
    # names compose reads are all present, and none is in .env
    for name in gi.env_names():
        assert name in env
    assert not (settings.cfg.project / ".env").exists() or "GCP_BQ_JSON" not in (settings.cfg.project / ".env").read_text()
    # bsp_common resolves the same way from that environment
    mod = C["bsp_gcp"]
    saved = {k: os.environ.get(k) for k in env}
    try:
        os.environ.update({k: v for k, v in env.items() if k.startswith("GCP_")})
        os.environ["GCP_BQ_WRITE_JSON"] = ""       # outside Studio: fallback chain in bsp_common
        assert mod.info("bigquery_write")["type"] == "authorized_user"
        assert mod.info("bigquery")["type"] == "authorized_user", "alias"
        assert mod.project("bigquery_write") == "proj-user"
        assert mod.sa_info("bigquery") is None and mod.sa_info("vertex")["client_email"] == SA["client_email"]
        assert mod.identity_names() == ["Seshu sign-in", "default service account", "uploaded sa"]
        assert mod.assigned_name("vertex") == "default service account"
        assert mod.identity_meta("Seshu sign-in")["principal"] == "seshu@example.com" and mod.identities() == {}
        try:
            mod.identity("Seshu sign-in")          # no key here and no acting user → refused, nothing leaks
        except mod.StudioTokenError as exc:
            assert "signed-in user" in str(exc)
        else:
            raise AssertionError("named identity without a user")
        os.environ["GCP_IDENTITIES"] = json.dumps({"Seshu sign-in": USER})      # legacy env (outside Studio)
        creds = mod.identity("Seshu sign-in")
        assert type(creds).__module__ == "google.oauth2.credentials" and creds.refresh_token == USER["refresh_token"]
        assert type(mod.credentials("vertex")).__module__ == "google.oauth2.service_account"
        try:
            mod.credentials("nope")
        except ValueError:
            pass
        else:
            raise AssertionError("unknown purpose accepted")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_projects_data_vs_execution():
    c = C["admin"]
    uid, sa2 = C["user_id"], C["sa2_id"]
    r = c.put("/studio/api/gcp/assignments", json={
        "bigquery_read": {"identity": uid, "data_projects": "biz-a, biz-b", "exec_project": "dev-proj"},
        "bigquery_write": {"identity": sa2, "project": "dev-proj"}}, headers=H)
    assert r.status_code == 200, r.text
    assert c.put("/studio/api/gcp/assignments", json={"gcs": {"identity": sa2, "project": "Bad Project!"}}, headers=H).status_code == 400
    env = orchestrator.secret_env()
    assert env["GCP_BQ_PROJECT"] == "biz-a" and env["GCP_BQ_DATA_PROJECTS"] == "biz-a,biz-b"
    assert env["GCP_BQ_EXEC_PROJECT"] == "dev-proj"
    assert env["GCP_BQ_WRITE_PROJECT"] == "dev-proj" and env["GCP_BQ_WRITE_EXEC_PROJECT"] == "dev-proj"
    assert env["GCP_VERTEX_PROJECT"] == "proj-sa", "unset project → the identity's default"
    view = next(x for x in c.get("/studio/api/gcp/identities").json()["consumers"] if x["key"] == "bigquery_read")
    assert view["projects"] == {"project": "biz-a", "exec_project": "dev-proj", "data_projects": ["biz-a", "biz-b"]}
    # the consumer test uses those projects: datasets in each data project, the dry-run in the execution project
    CALLS.clear()
    t = c.post("/studio/api/gcp/consumers/bigquery_read/test", json={}, headers=H).json()["result"]
    res = {x["id"]: x for x in t["results"]}
    assert "biz-a" in res["bq_list"]["detail"] and "biz-b" in res["bq_list"]["detail"]
    assert res["bq_query"]["ok"] and "dev-proj" in res["bq_query"]["detail"]
    assert any("/projects/dev-proj/jobs" in u for _, u, _ in CALLS) and not any("/projects/biz-a/jobs" in u for _, u, _ in CALLS)
    assert t["exec_project"] == "dev-proj" and t["data_projects"] == ["biz-a", "biz-b"]
    # bsp_common reads them
    mod = C["bsp_gcp"]
    saved = {k: os.environ.get(k) for k in env}
    try:
        os.environ.update({k: v for k, v in env.items() if k.startswith("GCP_")})
        assert mod.project("bigquery") == "biz-a" and mod.exec_project("bigquery") == "dev-proj"
        assert mod.data_projects("bigquery") == ["biz-a", "biz-b"] and mod.exec_project("vertex") == "proj-sa"
    finally:
        for k, v in saved.items():
            os.environ.pop(k, None) if v is None else os.environ.__setitem__(k, v)
    # identity default project + known projects + picker + probe
    r = c.patch(f"/studio/api/gcp/identities/{uid}", json={"project": "dev-proj", "known_projects": "nobq  No BigQuery\nbiz-b"}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["identity"]["project"] == "dev-proj"
    pj = c.get(f"/studio/api/gcp/identities/{uid}/projects").json()
    assert pj["available"] and [p["id"] for p in pj["projects"]] == ["biz-a", "dev-proj", "nobq", "biz-b"]
    assert pj["projects"][0]["name"] == "Business A" and pj["projects"][2]["known"]
    pr = c.post(f"/studio/api/gcp/identities/{uid}/probe", json={"projects": ["biz-a", "nobq", "dev-proj"]}, headers=H).json()
    rows = {x["id"]: x for x in pr["projects"]}
    assert rows["biz-a"]["datasets"] == 1 and rows["biz-a"]["jobs_create"] is False
    assert rows["nobq"]["datasets"] is None and "not enabled BigQuery" in rows["nobq"]["datasets_error"]
    assert rows["dev-proj"]["jobs_create"] is True
    assert pr["proposal"]["data_projects"] == ["biz-a", "dev-proj"] and pr["proposal"]["exec_project"] == "dev-proj"
    no_leak(json.dumps(pr))
    r = c.put("/studio/api/gcp/assignments", json={"bigquery_write": ""}, headers=H)
    assert r.status_code == 200 and gi.assignments()["bigquery_write"] == "", r.text


def test_compose_consumers_cover_workloads():
    for var in ("GCP_BQ_JSON", "GCP_IDENTITIES", "GCP_WORKSPACE_JSON"):
        assert set(composefile.consumers(var)) >= {"connectors", "superset"}, var


def test_live_checks_with_role_hints():
    c = C["admin"]
    r = c.post("/studio/api/gcp/identities/default-service-account/test", json={"bucket": "lake"}, headers=H)
    assert r.status_code == 200, r.text
    no_leak(r.text)
    t = r.json()["result"]
    res = {x["id"]: x for x in t["results"]}
    assert res["token"]["ok"] and res["vertex"]["ok"] and res["bq_list"]["ok"]
    assert not res["bq_query"]["ok"] and res["bq_query"]["status"] == 403
    assert "roles/bigquery.jobUser" in res["bq_query"]["hint"] and res["bq_query"]["permission"] == "bigquery.jobs.create"
    assert "roles/storage.bucketViewer" in res["gcs_buckets"]["hint"]
    assert res["gcs_objects"]["status"] == 404 and "gs://lake" in res["gcs_objects"]["hint"]
    assert "roles/artifactregistry.reader" in res["artifact_registry"]["hint"]
    assert "Enable the Cloud Run Admin API" in res["cloud_run"]["hint"]
    assert "bigquery.tables.create" in res["bq_write"]["detail"] and "roles/bigquery.dataEditor" in res["bq_write"]["hint"]
    assert t["capabilities"]["vertex"] == "ok" and t["capabilities"]["bigquery"] == "partial"
    assert t["consumers"]["vertex"]["verdict"] == "ok" and t["consumers"]["bigquery_read"]["verdict"] == "partial"
    listed = c.get("/studio/api/gcp/identities").json()
    rec = next(i for i in listed["identities"] if i["id"] == "default-service-account")
    assert rec["last_test"]["capabilities"]["vertex"] == "ok"


def test_authorized_user_test_and_expired_token():
    c = C["admin"]
    DENY.clear()
    r = c.post(f"/studio/api/gcp/consumers/bigquery_write/test", json={}, headers=H)
    assert r.status_code == 200, r.text
    t = r.json()["result"]
    assert r.json()["identity"] == C["user_id"] and r.json()["via"] == "bigquery_read", (r.json()["identity"], r.json()["via"], gi.assignment_details())
    assert t["consumers"]["bigquery_write"]["verdict"] in ("partial", "failed")   # testIamPermissions: tables.create missing
    assert t["principal"] == "seshu@example.com"
    # revoked refresh token → clean hint, other checks skipped
    bad = {**USER, "refresh_token": "revoked"}
    r = c.post("/studio/api/gcp/identities", json={"name": "revoked", "json": json.dumps(bad)}, headers=H)
    assert r.status_code == 200 and r.json()["identity"]["principal"] == ""
    rid = r.json()["identity"]["id"]
    t = c.post(f"/studio/api/gcp/identities/{rid}/test", json={}, headers=H).json()["result"]
    assert not t["results"][0]["ok"] and "sign in with Google again" in t["results"][0]["hint"]
    assert all(not x["ok"] for x in t["results"]) and t["capabilities"]["token"] == "failed"
    assert t["consumers"]["trino_bigquery"]["verdict"] == "failed"
    C["revoked"] = rid


def test_delete_refuses_assigned_unless_forced():
    c = C["admin"]
    uid = C["user_id"]
    r = c.delete(f"/studio/api/gcp/identities/{uid}", headers=H)
    assert r.status_code == 409 and "BigQuery · read" in r.json()["detail"]
    r = c.delete(f"/studio/api/gcp/identities/{uid}?force=true", headers=H)
    assert r.status_code == 200, r.text
    assert "bigquery_read" in r.json()["deleted"]["unassigned"]
    assert gi.assignments()["bigquery_read"] == ""
    env = orchestrator.secret_env()
    assert env["GCP_BQ_JSON"] == "" and "Seshu sign-in" not in env["GCP_IDENTITIES"]
    assert c.delete(f"/studio/api/gcp/identities/{C['revoked']}", headers=H).status_code == 200
    assert c.delete("/studio/api/gcp/identities/nope", headers=H).status_code == 404


def test_apply_marks_applied_when_nothing_runs():
    c = C["admin"]
    imp = c.get("/studio/api/gcp/assignments").json()["impact"]
    assert imp["pending"] and imp["recreate"] == []
    r = c.post("/studio/api/gcp/apply", headers=H)
    assert r.status_code == 200 and r.json()["job"] is None
    assert not c.get("/studio/api/gcp/assignments").json()["impact"]["pending"]
    c.put("/studio/api/gcp/assignments", json={"vertex": C["sa2_id"]}, headers=H)
    imp = c.get("/studio/api/gcp/assignments").json()["impact"]
    # same key, other project: only the project + name change
    assert imp["pending"] and imp["changed"] == ["GCP_VERTEX_PROJECT", "GCP_ASSIGNMENTS"], imp


def test_admin_only():
    c = C["admin"]
    assert c.post("/studio/api/users", json={"name": "vic", "role": "analyst", "password": "viewer-pass-1"}, headers=H).status_code == 200
    v = TestClient(studio.app, base_url="https://testserver")
    assert v.post("/studio/api/auth/login", json={"username": "vic", "password": "viewer-pass-1"}, headers=H).status_code == 200
    for method, path in (("get", "/studio/api/gcp/identities"), ("get", "/studio/api/gcp/assignments"),
                         ("post", "/studio/api/gcp/google-login/start"), ("post", "/studio/api/gcp/apply")):
        assert getattr(v, method)(path, headers=H).status_code == 403, path
    assert "identities" not in {p["id"] for p in v.get("/studio/api/me").json()["pages"]}
    assert "identities" in {p["id"] for p in c.get("/studio/api/me").json()["pages"]}


def test_google_login_session_plumbing():
    """Fake `docker run`: prints the consent URL, reads the code, emits the
    credential between markers (or gcloud's error for a bad code)."""
    import gcloud_login as gl

    script = Path(settings.cfg.project) / "fake_gcloud.sh"
    script.write_text("""#!/bin/sh
echo "Go to the following link in your browser, and complete the sign-in prompts:"
echo ""
echo "    https://accounts.google.com/o/oauth2/auth?response_type=code&client_id=764086051850-x&scope=openid"
echo ""
printf "Once finished, enter the verification code provided in your browser: "
read code
if [ "$code" = "good-code" ]; then
  echo '<<<ADC-JSON-BEGIN>>>'
  echo '{"type": "authorized_user", "client_id": "764086051850-x.apps.googleusercontent.com", "client_secret": "client-secret-SECRET", "refresh_token": "refresh-token-SECRET"}'
  echo '<<<ADC-JSON-END>>>'
else
  echo "ERROR: There was a problem with web authentication."
  echo "ERROR: (gcloud.auth.application-default.login) (invalid_grant) Malformed auth code."
  exit 1
fi
""")
    script.chmod(0o755)
    gl.argv = lambda name: [str(script)]
    killed: list[str] = []
    real_run = gl.subprocess.run
    gl.subprocess.run = lambda argv, **kw: killed.append(argv[-1]) or real_run(["true"])
    c = C["admin"]
    r = c.post("/studio/api/gcp/google-login/start", headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["auth_url"].startswith("https://accounts.google.com/o/oauth2/auth?")
    sid = r.json()["session"]
    r = c.post(f"/studio/api/gcp/google-login/{sid}/code", json={"code": "bogus"}, headers=H)
    assert r.status_code == 400 and "invalid or expired verification code" in r.json()["detail"], r.text
    assert sid not in gl._sessions, "session cleaned up"
    assert c.post(f"/studio/api/gcp/google-login/{sid}/code", json={"code": "x"}, headers=H).status_code == 404
    # good code → identity named after the Google e-mail, no secret in the response
    sid = c.post("/studio/api/gcp/google-login/start", headers=H).json()["session"]
    r = c.post(f"/studio/api/gcp/google-login/{sid}/code", json={"code": "good-code"}, headers=H)
    assert r.status_code == 200, r.text
    no_leak(r.text)
    ident = r.json()["identity"]
    assert ident["name"] == "seshu@example.com" and ident["type"] == "authorized_user" and ident["source"] == "google-login"
    # cancel
    sid = c.post("/studio/api/gcp/google-login/start", headers=H).json()["session"]
    assert c.delete(f"/studio/api/gcp/google-login/{sid}", headers=H).json()["cancelled"]
    assert sid not in gl._sessions
    gl.subprocess.run = real_run
    # the real docker argv: non-root, memory-limited, proxy + CA, tmpfs config, cleanup label
    import importlib
    importlib.reload(gl)
    argv = gl.argv("bsp-gcloud-login-test")
    joined = " ".join(argv)
    for frag in ("--rm", "-i", "--user 1000:1000", "--memory 512m", "--cap-drop ALL", "HTTPS_PROXY=http://px-proxy:3128",
                 "CLOUDSDK_CORE_CUSTOM_CA_CERTS_FILE=/trust/ca-bundle.pem", "--tmpfs /gcloud", "CLOUDSDK_CONFIG=/gcloud",
                 "--no-launch-browser", "userinfo.email", "timeout 600", "/trust:ro"):
        assert frag in joined, frag
    audit_log = (settings.cfg.vault_dir / "audit.log").read_text()
    no_leak(audit_log)
    assert "good-code" not in audit_log and "bogus" not in audit_log


# ── principal switch: access lists, my-identities, internal token endpoint ──
class _FakeRefresh:
    n = 0

    def __call__(self, creds):
        import datetime as dt
        type(self).n += 1
        creds.token = f"ya29.fake-{type(self).n}"
        creds.expiry = dt.datetime.utcnow() + dt.timedelta(seconds=3599)


def test_principal_switch():
    import hashlib
    import hmac
    import time

    c = C["admin"]
    r = c.post("/studio/api/gcp/identities", json={"name": "Seshu sign-in", "json": json.dumps(USER)}, headers=H)
    assert r.status_code == 200, r.text
    uid, sa2 = r.json()["identity"]["id"], C["sa2_id"]
    gi._refresh = _FakeRefresh()
    toks = {"mcp": "m" * 40, "appliance": "w" * 48, "canvas": "c" * 48}
    store.mutate(lambda d: (d.setdefault("services", {}).setdefault("mcp", {}).update(MCP_TOKEN=toks["mcp"], MCP_DELEGATION_KEY="k" * 48),
                            d["services"].setdefault("appliance", {}).update(WB_STUDIO_TOKEN=toks["appliance"]),
                            d["services"].setdefault("canvas", {}).update(CANVAS_STUDIO_TOKEN=toks["canvas"])))
    v = TestClient(studio.app, base_url="https://testserver")
    assert v.post("/studio/api/auth/login", json={"username": "vic", "password": "viewer-pass-1"}, headers=H).status_code == 200
    vic_session = v.cookies.get("studio_session")
    admin_session = c.cookies.get("studio_session")
    assert vic_session and admin_session

    # default: owner + admins — vic (analyst, not owner) sees none, the admin sees all
    r = v.get("/studio/api/gcp/my-identities")
    assert r.status_code == 200, r.text
    assert r.json()["identities"] == [] and "bigquery_read" in r.json()["defaults"]
    no_leak(r.text)
    mine = c.get("/studio/api/gcp/my-identities").json()
    assert {i["id"] for i in mine["identities"]} >= {uid, sa2}
    ent = next(i for i in mine["identities"] if i["id"] == uid)
    assert ent["principal"] == "seshu@example.com" and ent["type"] == "authorized_user" and ent["default_project"]
    assert "json" not in ent and "access_token" not in json.dumps(mine)

    # access list: admin only; audited
    assert v.put(f"/studio/api/gcp/identities/{uid}/access", json={"mode": "everyone"}, headers=H).status_code == 403
    assert c.put(f"/studio/api/gcp/identities/{uid}/access", json={"mode": "list", "users": ""}, headers=H).status_code == 400
    assert c.put(f"/studio/api/gcp/identities/{uid}/access", json={"mode": "bogus"}, headers=H).status_code == 400
    r = c.put(f"/studio/api/gcp/identities/{uid}/access", json={"mode": "list", "users": "VIC, anna@example.com"}, headers=H)
    assert r.status_code == 200, r.text
    assert next(i for i in r.json()["identities"] if i["id"] == uid)["access"]["users"] == ["vic", "anna@example.com"]
    assert [i["id"] for i in v.get("/studio/api/gcp/my-identities").json()["identities"]] == [uid]

    url = "/studio/internal/gcp-token"
    bearer = lambda svc: {"Authorization": f"Bearer {toks[svc]}"}  # noqa: E731
    # transport guard
    assert c.post(url, json={"identity": uid, "user": "vic"}).status_code == 401
    assert c.post(url, json={"identity": uid, "user": "vic"}, headers={"Authorization": "Bearer nope"}).status_code == 401
    assert c.post(url, json={"identity": uid, "user": "vic", "session": vic_session},
                  headers={**bearer("appliance"), "X-Forwarded-For": "10.0.0.9"}).status_code == 404
    # a service token alone never names a user
    for svc in toks:
        r = c.post(url, json={"identity": uid, "user": "vic"}, headers=bearer(svc))
        assert r.status_code == 403, (svc, r.text)
    # session proof: must match the user
    r = c.post(url, json={"identity": uid, "user": "seshu", "session": vic_session}, headers=bearer("appliance"))
    assert r.status_code == 403
    r = c.post(url, json={"identity": uid, "user": "vic", "session": vic_session, "consumer": "bigquery_read"},
               headers=bearer("appliance"))
    assert r.status_code == 200, r.text
    d = r.json()
    assert d["access_token"].startswith("ya29.fake-") and d["expires_in"] > 3000 and d["principal_email"] == "seshu@example.com"
    assert d["identity"] == uid and d["default_project"] and "refresh_token" not in r.text and "client_secret" not in r.text
    n = _FakeRefresh.n
    r2 = c.post(url, json={"identity": "Seshu sign-in", "user": "vic", "session": vic_session}, headers=bearer("canvas"))
    assert r2.status_code == 200 and r2.json()["access_token"] == d["access_token"] and _FakeRefresh.n == n, "cached; name works"
    # not on sa2's list → refused; admin may use anything
    assert c.post(url, json={"identity": sa2, "user": "vic", "session": vic_session}, headers=bearer("appliance")).status_code == 403
    r = c.post(url, json={"identity": sa2, "user": ADMIN, "session": admin_session, "scopes": ["bigquery.readonly"]},
               headers=bearer("appliance"))
    assert r.status_code == 200 and r.json()["scopes"] == ["https://www.googleapis.com/auth/bigquery.readonly"], r.text
    assert c.post(url, json={"identity": sa2, "user": ADMIN, "session": admin_session, "scopes": ["gmail.send"]},
                  headers=bearer("appliance")).status_code == 400
    assert c.post(url, json={"identity": "nope", "user": "vic", "session": vic_session}, headers=bearer("appliance")).status_code == 404
    # MCP: the harness caller signature, re-verified with MCP_DELEGATION_KEY
    ts = str(int(time.time()))
    sig = hmac.new(b"k" * 48, f"vic|{ts}".encode(), hashlib.sha256).hexdigest()
    assert c.post(url, json={"identity": uid, "user": "vic", "caller_ts": ts, "caller_sig": sig}, headers=bearer("mcp")).status_code == 200
    assert c.post(url, json={"identity": uid, "user": "vic", "caller_ts": ts, "caller_sig": "0" * 64}, headers=bearer("mcp")).status_code == 403
    old = str(int(time.time()) - 900)
    sig_old = hmac.new(b"k" * 48, f"vic|{old}".encode(), hashlib.sha256).hexdigest()
    assert c.post(url, json={"identity": uid, "user": "vic", "caller_ts": old, "caller_sig": sig_old}, headers=bearer("mcp")).status_code == 403
    assert c.post(url, json={"identity": uid, "user": "vic", "caller_ts": ts, "caller_sig": sig}, headers=bearer("appliance")).status_code == 403
    # grants (scheduled canvas runs): need a session to issue, bound to service + identity, re-checked on use
    g = c.post("/studio/internal/gcp-grant", json={"identity": uid, "user": "vic"}, headers=bearer("canvas"))
    assert g.status_code == 403
    g = c.post("/studio/internal/gcp-grant", json={"identity": uid, "user": "vic", "session": vic_session}, headers=bearer("canvas"))
    assert g.status_code == 200, g.text
    grant = g.json()["grant"]
    assert c.post(url, json={"identity": uid, "user": "vic", "grant": grant}, headers=bearer("canvas")).status_code == 200
    assert c.post(url, json={"identity": uid, "user": "vic", "grant": grant}, headers=bearer("appliance")).status_code == 403
    assert c.post(url, json={"identity": sa2, "user": "vic", "grant": grant}, headers=bearer("canvas")).status_code == 403
    assert c.post("/studio/internal/gcp-grant", json={"identity": sa2, "user": "vic", "session": vic_session},
                  headers=bearer("canvas")).status_code == 403
    # taking vic off the list revokes token issuance and the grant at once
    assert c.put(f"/studio/api/gcp/identities/{uid}/access", json={"mode": "owner"}, headers=H).status_code == 200
    assert c.post(url, json={"identity": uid, "user": "vic", "grant": grant}, headers=bearer("canvas")).status_code == 403
    assert c.post(url, json={"identity": uid, "user": "vic", "session": vic_session}, headers=bearer("appliance")).status_code == 403
    # everyone
    assert c.put(f"/studio/api/gcp/identities/{uid}/access", json={"mode": "everyone"}, headers=H).status_code == 200
    assert c.post(url, json={"identity": uid, "user": "vic", "session": vic_session}, headers=bearer("appliance")).status_code == 200
    c.put(f"/studio/api/gcp/identities/{uid}/access", json={"mode": "owner"}, headers=H)
    # audit: who / which identity / which service, never a token
    log = (settings.cfg.vault_dir / "audit.log").read_text()
    no_leak(log)
    assert "ya29" not in log
    assert f"gcp_token.issued" in log and f"{uid} for vic via appliance/bigquery_read (session)" in log
    assert "gcp_token.denied" in log and "gcp_identity.access" in log and "gcp_grant.issued" in log
