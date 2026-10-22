"""Executive Story endpoint: auth, payload shape, graceful degradation when
components are missing or failing, aggregation from (fake) live services,
no personal data in the payload, and the one-minute cache."""

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from fastapi.testclient import TestClient

import app as studio
import story
from vault import store

H = {"X-Studio": "1"}
C: dict[str, TestClient] = {}
CHAPTERS = ["ask", "connect", "model", "see", "predict", "automate", "ai", "govern", "own"]
TILE_KEYS = {"id", "label", "value", "detail", "state"}
SECRET_EMAIL = "jane.customer@example.org"   # must never reach the payload

# ── a fake in-network service farm ───────────────────────────────────────
ROUTES = {
    "/canvas/api/pipelines": {"pipelines": [
        {"id": "p1", "sources": ["source.database"], "last_run": {"status": "success"}},
        {"id": "p2", "sources": ["source.outlook"], "last_run": {"status": "failed"}}]},
    "/canvas/api/runs": {"runs": [
        {"status": "success", "loaded": [{"target": "bsp_edw.landing.a", "rows": 1200}]},
        {"status": "success", "loaded": [{"target": "bsp_edw.landing.b", "rows": 34}]}]},
    "/canvas/api/connections": {"connections": [
        {"name": "warehouse", "type": "postgres", "builtin": True},
        {"name": "ora", "type": "oracle", "params": {"host": "secret-host", "user": "scott"}}]},
    "/mlops/api/overview": {"aliases": {"champion": "7", "challenger": "8"},
                            "gateway": {"mode": "canary"}, "traffic": {"total": 4321},
                            "drift": {"breached": False, "max_psi": 0.02}, "retrain": {},
                            "frozen": {"n": 2}, "scored": {"n": 999, "last_month": "2026-09-01"},
                            "settings": {"drift_interval_min": 5}},
    "/mlops/api/journeys": {"models": [{"name": "bsp_churn", "versions": [
        {"version": "7", "test_auc": 0.8123}, {"version": "8", "test_auc": 0.80}]}], "journeys": [{}, {}, {}]},
    "/disputes/api/cases": [
        {"status": "approved", "auto_approved": True, "sap_dispute_id": "D1", "sender_email": SECRET_EMAIL},
        {"status": "needs_review", "auto_approved": False, "sap_dispute_id": None, "sender_email": SECRET_EMAIL},
        {"status": "not_dispute", "auto_approved": False, "sap_dispute_id": None, "sender_email": SECRET_EMAIL}],
    "/ccai/api/status": {"counts": {"calls": {"calls": 50, "tagged": 48},
                                    "tags": {"avg_conf": 0.9, "needs_review": 3, "churn": 7, "reviewed": 10}},
                         "taxonomy": {"leaves": 104}},
    "/ccai/api/quality": {"summary": {"accuracy": 0.9}},
    "/ai/api/mcp/tools": {"groups": {"bigquery": 5, "atlan": 4}, "tools": []},
    "/ai/api/models": {"data": [{"id": "chat-default"}, {"id": "vertex/gemini-2.5-flash"}, {"id": "ollama/llama3.1:8b"}]},
    "/ai/api/telemetry": {"total": {"calls": 100, "ok": 99, "p95_ms": 1500, "cost_usd": 0.5,
                                    "prompt_tokens": 1000, "completion_tokens": 50}},
    "/superset/api/v1/dashboard/": {"count": 4, "result": [{"dashboard_title": "Churn"}, {"dashboard_title": "Revenue"}]},
    "/superset/api/v1/chart/": {"count": 40},
    "/superset/api/v1/dataset/": {"count": 12},
}
BEHAVIOUR: dict[str, str] = {}   # path → "500" | "slow"


class Fake(BaseHTTPRequestHandler):
    def log_message(self, *a):  # quiet
        pass

    def _send(self, code, body):
        raw = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        path = self.path.split("?")[0]
        mode = BEHAVIOUR.get(path)
        if mode == "slow":
            time.sleep(story.TIMEOUT + 1)
        if mode == "500":
            return self._send(500, {"detail": "boom"})
        if path.startswith("/superset/") and self.headers.get("Authorization") != "Bearer tok":
            return self._send(401, {"msg": "no"})
        if path in ROUTES:
            return self._send(200, ROUTES[path])
        self._send(404, {"detail": "not found"})

    def do_POST(self):
        if self.path == "/superset/api/v1/security/login":
            body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
            ok = body.get("password") == "superset-admin-pw"
            return self._send(200 if ok else 401, {"access_token": "tok"} if ok else {"msg": "bad"})
        self._send(404, {})


def _farm() -> str:
    srv = ThreadingHTTPServer(("127.0.0.1", 0), Fake)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return f"http://127.0.0.1:{srv.server_address[1]}"


def _shape(body):
    assert set(body["chapters"]) == set(CHAPTERS), body["chapters"].keys()
    for cid, ch in body["chapters"].items():
        assert ch["state"] in ("live", "partial", "absent"), (cid, ch["state"])
        assert isinstance(ch["links"], list)
        assert 1 <= len(ch["tiles"]) <= 4, (cid, len(ch["tiles"]))
        for t in ch["tiles"]:
            assert set(t) >= TILE_KEYS and t["state"] in ("ok", "absent", "error"), (cid, t)


def _tiles(body, cid):
    return {t["id"]: t for t in body["chapters"][cid]["tiles"]}


# ── tests ────────────────────────────────────────────────────────────────
def test_setup_and_auth():
    c = C["admin"] = TestClient(studio.app, base_url="https://testserver")
    c.__enter__()
    assert c.get("/studio/api/story").status_code == 401
    r = c.post("/studio/api/auth/setup", json={"username": "presenter", "password": "Tr0ub4dour-and-3",
                                               "master": "vault master passphrase 1"}, headers=H)
    assert r.status_code == 200, r.text
    me = c.get("/studio/api/me").json()
    assert me["pages"][0]["id"] == "story", "Story leads the nav"


def test_degrades_when_nothing_runs():
    story._cache.update(at=0.0, data=None)
    story._status = lambda: {}   # no docker / nothing running
    r = C["admin"].get("/studio/api/story")
    assert r.status_code == 200, r.text
    body = r.json()
    _shape(body)
    for cid in ("connect", "see", "predict", "automate", "ai"):
        ch = body["chapters"][cid]
        assert ch["state"] == "absent", (cid, ch)
        assert all(t["detail"] == "Not running in this appliance" for t in ch["tiles"]), ch["tiles"]
        assert all(not link["up"] for link in ch["links"])
    gov = _tiles(body, "govern")
    assert gov["sso"]["value"] == "local accounts" and gov["sso"]["state"] == "ok"
    assert gov["audit"]["state"] == "ok"


def test_dbt_artifacts(project: Path):
    dbt = project / "dbt"
    (dbt / "target").mkdir(parents=True)
    (dbt / "logs").mkdir()
    nodes = {f"model.p.m{i}": {"resource_type": "model", "config": {"schema": s}}
             for i, s in enumerate(["staging", "staging", "intermediate", "marts"])}
    nodes.update({f"test.p.t{i}": {"resource_type": "test"} for i in range(5)})
    (dbt / "target" / "manifest.json").write_text(json.dumps({"nodes": nodes, "sources": {"s1": {}},
                                                              "parent_map": {"model.p.m3": ["model.p.m2", "model.p.m1"]}}))
    (dbt / "logs" / "dbt.log").write_text("Done. PASS=9 WARN=0 ERROR=0 SKIP=0 NO-OP=0 TOTAL=9\n"
                                          "Done. PASS=1 WARN=0 ERROR=0 SKIP=0 TOTAL=1\n")
    import os
    os.environ["STORY_DBT_DIR"] = str(dbt)
    body = C["admin"].get("/studio/api/story?refresh=1").json()
    t = _tiles(body, "model")
    assert t["models"]["value"] == 4 and t["models"]["detail"] == "2 staging · 1 intermediate · 1 marts"
    assert t["passing"]["value"] == "9 passed" and t["passing"]["state"] == "ok", "last real build, not the 1-node seed"
    assert t["tests"]["value"] == 5


def test_live_numbers_from_services():
    base = _farm()
    for k, prefix in (("canvas", "/canvas"), ("mlops", "/mlops"), ("disputes", "/disputes"), ("ccai", "/ccai"),
                      ("harness", "/ai"), ("superset", "/superset"), ("rag", "/rag")):
        story.URLS[k] = base + prefix
    running = {"state": "running"}
    story._status = lambda: {s: dict(running) for s in ("canvas", "mlops", "disputes", "ccai", "harness", "superset")}
    store.set("services.superset.SUPERSET_ADMIN_PASS", "superset-admin-pw")
    r = C["admin"].get("/studio/api/story?refresh=1")
    raw = r.text
    body = r.json()
    _shape(body)
    assert SECRET_EMAIL not in raw and "secret-host" not in raw and "superset-admin-pw" not in raw, "aggregates only"

    con = _tiles(body, "connect")
    assert con["pipelines"]["value"] == 2 and con["pipelines"]["detail"].startswith("1 healthy")
    assert con["rows"]["value"] == "1,234" and "2 landing tables" in con["rows"]["detail"]
    assert "Oracle" in con["sources"]["detail"] and "Outlook" in con["sources"]["detail"]

    pre = _tiles(body, "predict")
    assert pre["champion"]["value"] == "v7" and "AUC 0.812" in pre["champion"]["detail"]
    assert pre["serving"]["value"] == "canary" and pre["drift"]["value"] == "stable"

    aut = _tiles(body, "automate")
    assert aut["disputes"]["value"] == 3 and aut["sap"]["value"] == 1
    assert aut["calls"]["value"] == "48" and "104-leaf" in aut["calls"]["detail"]
    assert aut["accuracy"]["value"] == "90%"

    see = _tiles(body, "see")
    assert (see["dashboards"]["value"], see["charts"]["value"], see["datasets"]["value"]) == (4, 40, 12)

    ai = _tiles(body, "ai")
    assert ai["tools"]["value"] == 9 and ai["models"]["value"] == 1
    assert ai["rag"]["state"] == "absent", "rag not running → degraded tile, rest of the chapter live"
    assert body["chapters"]["ai"]["state"] == "partial"

    own = _tiles(body, "own")
    assert own["spend"]["value"] == "$0.50"


def test_failing_and_slow_services_degrade():
    BEHAVIOUR.update({"/mlops/api/overview": "500", "/disputes/api/cases": "slow"})
    try:
        t0 = time.time()
        body = C["admin"].get("/studio/api/story?refresh=1").json()
        assert time.time() - t0 < story.BUDGET + 3, "bounded by the fan-out budget"
        _shape(body)
        pre = body["chapters"]["predict"]
        assert pre["state"] == "absent" and pre["tiles"][0]["state"] == "error"
        assert body["chapters"]["automate"]["tiles"][0]["state"] == "error"
        assert body["chapters"]["connect"]["state"] == "live", "one failure never takes the page down"
    finally:
        BEHAVIOUR.clear()


def test_cache():
    c = C["admin"]
    first = c.get("/studio/api/story?refresh=1").json()
    assert first["cached"] is False
    calls = []
    real = story.collect
    story.collect = lambda: calls.append(1) or real()
    try:
        again = c.get("/studio/api/story").json()
        assert again["cached"] is True and not calls and again["generated_at"] == first["generated_at"]
        c.get("/studio/api/story?refresh=1")
        assert calls == [1]
    finally:
        story.collect = real
