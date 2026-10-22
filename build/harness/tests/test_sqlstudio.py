"""Federated SQL tab: saved-query storage, shared-collection permissions, import/export, identity, run/explain."""
import json

import pytest
from fastapi.testclient import TestClient

from bsp_harness import identity, sqlstudio as ss


@pytest.fixture()
def client(monkeypatch):
    from bsp_harness.app import create_app
    return TestClient(create_app())


@pytest.fixture()
def as_nginx(monkeypatch):
    """Requests count as coming from the nginx peer, so X-Auth-User is trusted (like production)."""
    monkeypatch.setattr(identity, "_from_facade", lambda request: True)


def hdr(user):
    return {"X-Auth-User": user}


# ── storage ──────────────────────────────────────────────────────────────────
def test_query_crud_and_collections():
    q1 = ss.create_query("ana", {"title": "Churn", "sql": "select 1", "starred": True})
    q2 = ss.create_query("ana", {"title": "ARPU", "sql": "select 2"})
    c = ss.create_collection("ana", {"name": "Revenue"})
    ss.update_query("ana", q1["id"], {"collection_ids": [c["id"]], "note": "first"})
    ss.add_to_collection("ana", c["id"], {"query_id": q2["id"]})
    lib = ss.load_library("ana")
    assert lib["collections"][0]["query_ids"] == [q1["id"], q2["id"]]
    assert lib["queries"][1]["note"] == "first" and lib["queries"][1]["starred"] is True
    # reorder inside the collection and in the list
    ss.update_collection("ana", c["id"], {"query_ids": [q2["id"], q1["id"], "q-unknown"]})
    assert ss.load_library("ana")["collections"][0]["query_ids"] == [q2["id"], q1["id"]]
    ss.reorder_queries("ana", [q1["id"], q2["id"]])
    assert [q["id"] for q in ss.load_library("ana")["queries"]] == [q1["id"], q2["id"]]
    # duplicate keeps collection membership; delete removes it everywhere
    d = ss.duplicate_query("ana", q1["id"])
    assert d["title"] == "Churn (copy)" and d["id"] in ss.load_library("ana")["collections"][0]["query_ids"]
    ss.delete_query("ana", q1["id"])
    lib = ss.load_library("ana")
    assert q1["id"] not in lib["collections"][0]["query_ids"] and all(q["id"] != q1["id"] for q in lib["queries"])
    # a query can sit in several collections
    c2 = ss.create_collection("ana", {"name": "Board pack"})
    ss.update_query("ana", q2["id"], {"collection_ids": [c["id"], c2["id"]]})
    assert all(q2["id"] in x["query_ids"] for x in ss.load_library("ana")["collections"])
    ss.delete_collection("ana", c2["id"])
    assert any(q["id"] == q2["id"] for q in ss.load_library("ana")["queries"])   # queries outlive the collection


def test_libraries_are_per_user_and_written_atomically(tmp_path):
    ss.create_query("ana", {"title": "a", "sql": "select 1"})
    assert ss.load_library("bob")["queries"] == []
    f = ss._user_file("ana")
    assert f.is_file() and not list(f.parent.glob("*.tmp"))
    assert f.parent.parent.name == "users" and f.parent.name.startswith("ana-")


def test_run_stats_recorded_without_rows():
    q = ss.create_query("ana", {"title": "a", "sql": "select 1"})
    ss.record_run("ana", {"qid": q["id"]}, 42, 120, True)
    got = ss.load_library("ana")["queries"][0]
    assert got["row_count"] == 42 and got["ms"] == 120 and got["last_run"]
    ss.ensure_seed()
    ss.record_run("ana", {"cid": ss.SEED_ID, "qid": "gs-1"}, 3, 9, True)   # shared: stats live in the user's own file
    v = ss.library_view("ana")
    seed = next(c for c in v["shared"] if c["id"] == ss.SEED_ID)
    assert seed["queries"][0]["row_count"] == 3
    assert json.loads(ss._shared_path(ss.SEED_ID).read_text())["queries"][0].get("row_count") is None


# ── sharing ──────────────────────────────────────────────────────────────────
def test_shared_collections_owner_only():
    s = ss.create_collection("ana", {"name": "Team", "shared": True})
    mine = ss.create_query("ana", {"title": "q", "sql": "select 1"})
    ss.add_to_collection("ana", s["id"], {"query_id": mine["id"]})
    doc = ss.load_shared(s["id"])
    assert doc["owner"] == "ana" and len(doc["queries"]) == 1 and doc["queries"][0]["id"] != mine["id"]   # a copy
    v = ss.library_view("bob")
    team = next(c for c in v["shared"] if c["id"] == s["id"])
    assert team["editable"] is False and next(c for c in ss.library_view("ana")["shared"] if c["id"] == s["id"])["editable"]
    for fn in (lambda: ss.update_collection("bob", s["id"], {"name": "x"}),
               lambda: ss.delete_collection("bob", s["id"]),
               lambda: ss.add_to_collection("bob", s["id"], {"title": "t", "sql": "select 2"}),
               lambda: ss.update_shared_query("bob", s["id"], doc["queries"][0]["id"], {"sql": "select 3"}),
               lambda: ss.remove_from_collection("bob", s["id"], doc["queries"][0]["id"])):
        with pytest.raises(Exception) as e:
            fn()
        assert getattr(e.value, "status_code", None) == 403
    # others can copy it into their own library
    copy = ss.duplicate_collection("bob", s["id"])
    assert copy["name"] == "Team (copy)" and len(copy["query_ids"]) == 1
    ss.update_shared_query("ana", s["id"], doc["queries"][0]["id"], {"title": "renamed"})
    assert ss.load_shared(s["id"])["queries"][0]["title"] == "renamed"
    ss.delete_collection("ana", s["id"])
    assert ss.load_shared(s["id"]) is None


def test_anonymous_cannot_share_and_seed_is_read_only():
    with pytest.raises(Exception) as e:
        ss.create_collection(ss.ANON, {"name": "x", "shared": True})
    assert e.value.status_code == 403
    ss.ensure_seed()
    seed = ss.load_shared(ss.SEED_ID)
    assert seed["owner"] == "appliance" and 4 <= len(seed["queries"]) <= 6
    with pytest.raises(Exception) as e:
        ss.update_collection("ana", ss.SEED_ID, {"name": "mine now"})
    assert e.value.status_code == 403


def test_share_personal_collection_publishes_a_copy():
    c = ss.create_collection("ana", {"name": "Mine"})
    q = ss.create_query("ana", {"title": "q", "sql": "select 1", "collection_ids": [c["id"]]})
    s = ss.share_collection("ana", c["id"])
    assert s["owner"] == "ana" and s["queries"][0]["sql"] == q["sql"]


# ── import / export ──────────────────────────────────────────────────────────
def test_export_import_roundtrip():
    c = ss.create_collection("ana", {"name": "Churn work", "note": "n"})
    ss.create_query("ana", {"title": "a", "sql": "select 1", "note": "x", "collection_ids": [c["id"]]})
    ss.create_query("ana", {"title": "b", "sql": "select 2", "collection_ids": [c["id"]]})
    exp = ss.export_collection("ana", c["id"])
    assert exp["format"] == "bsp-sql-collection" and [q["title"] for q in exp["queries"]] == ["a", "b"]
    assert all(set(q) == {"title", "sql", "note", "question", "starred", "pinned"} for q in exp["queries"])
    got = ss.import_collection("bob", json.loads(json.dumps(exp)))
    lib = ss.load_library("bob")
    assert got["name"] == "Churn work" and len(got["query_ids"]) == 2 and len(lib["queries"]) == 2
    shared = ss.import_collection("bob", exp, shared=True)
    assert shared["owner"] == "bob" and len(shared["queries"]) == 2


@pytest.mark.parametrize("doc", [{"queries": "nope"}, {"queries": [{"title": "no sql"}]}, {"format": "other", "queries": [{"sql": "x"}]}, 7])
def test_import_rejects_bad_files(doc):
    with pytest.raises(Exception) as e:
        ss.import_collection("ana", doc)
    assert e.value.status_code == 400


# ── endpoints and identity ───────────────────────────────────────────────────
def test_identity_header_ignored_unless_from_nginx(client):
    r = client.post("/ai/api/sql/queries", json={"title": "t", "sql": "select 1"}, headers=hdr("mallory"))
    assert r.status_code == 200
    assert client.get("/ai/api/sql/library", headers=hdr("someone")).json()["user"] == "playground"
    assert len(ss.load_library("playground")["queries"]) == 1 and ss.load_library("mallory")["queries"] == []


def test_endpoints_per_user(client, as_nginx):
    r = client.post("/ai/api/sql/queries", json={"title": "ana q", "sql": "select 1"}, headers=hdr("ana"))
    qid = r.json()["id"]
    assert client.get("/ai/api/sql/library", headers=hdr("bob")).json()["queries"] == []
    assert client.patch(f"/ai/api/sql/queries/{qid}", json={"title": "x"}, headers=hdr("bob")).status_code == 404
    lib = client.get("/ai/api/sql/library", headers=hdr("ana")).json()
    assert lib["signed_in"] and lib["queries"][0]["title"] == "ana q"
    assert any(c["id"] == ss.SEED_ID and not c["editable"] for c in lib["shared"])
    c = client.post("/ai/api/sql/collections", json={"name": "S", "shared": True}, headers=hdr("ana")).json()
    assert client.patch(f"/ai/api/sql/collections/{c['id']}", json={"name": "y"}, headers=hdr("bob")).status_code == 403
    assert client.delete(f"/ai/api/sql/collections/{c['id']}", headers=hdr("bob")).status_code == 403
    assert client.patch(f"/ai/api/sql/collections/{c['id']}", json={"name": "y"}, headers=hdr("ana")).status_code == 200
    assert client.get("/ai/api/sql/search?q=ana", headers=hdr("ana")).json()["results"][0]["id"] == qid
    exp = client.get(f"/ai/api/sql/collections/{ss.SEED_ID}/export", headers=hdr("bob"))
    assert exp.status_code == 200 and "attachment" in exp.headers["content-disposition"]
    imp = client.post("/ai/api/sql/collections/import", json={"doc": exp.json()}, headers=hdr("bob")).json()
    assert len(imp["query_ids"]) == len(exp.json()["queries"])
    assert client.patch("/ai/api/sql/queries/..%2Fetc", json={}, headers=hdr("ana")).status_code in (400, 404)


def test_refresh_needs_sign_in(client):
    r = client.post("/ai/api/sql/catalog/refresh", json={"scope": {"catalog": "warehouse"}})
    assert r.status_code == 403


def test_refresh_rejects_bad_scope(client, as_nginx):
    assert client.post("/ai/api/sql/catalog/refresh", json={"scope": {"catalog": "x; drop"}}, headers=hdr("ana")).status_code == 400
    assert client.post("/ai/api/sql/catalog/refresh", json={"scope": {"catalog": "w", "tables": ["t"]}}, headers=hdr("ana")).status_code == 400


def test_run_goes_through_mcp_as_caller_and_never_stores_rows(client, as_nginx, monkeypatch, harness_env):
    calls = []

    async def fake_tool(name, args, caller, timeout=120.0):
        calls.append((name, args, caller))
        if name == "trino_query" and "system.metadata.catalogs" in args["sql"]:
            return {"rows": [{"catalog_name": "warehouse", "connector_name": "postgresql"},
                             {"catalog_name": "billing_oracle", "connector_name": "oracle"}]}, "", False
        return {"rows": [{"ban": "SENTINEL-ROW-VALUE", "n": 1}], "truncated": False}, "1 row(s)", False

    monkeypatch.setattr(ss, "call_tool", fake_tool)
    ss._cats_cache.update(at=0.0, rows=[])
    q = client.post("/ai/api/sql/queries", json={"title": "t", "sql": "select * from warehouse.marts.x a join billing_oracle.billing.invoices i on i.ban = a.ban"},
                    headers=hdr("ana")).json()
    r = client.post("/ai/api/sql/run", json={"sql": q["sql"], "max_rows": 9999, "ref": {"qid": q["id"]}}, headers=hdr("ana"))
    d = r.json()
    assert r.headers["cache-control"] == "no-store"
    assert d["ok"] and d["row_count"] == 1 and d["systems"] == ["billing_oracle", "warehouse"]
    assert calls[0] == ("trino_query", {"sql": q["sql"], "max_rows": 500}, "ana")   # capped, made as the caller
    assert ss.load_library("ana")["queries"][0]["row_count"] == 1
    data_root = harness_env.parent / "data"
    for f in data_root.rglob("*"):
        if f.is_file():
            assert "SENTINEL-ROW-VALUE" not in f.read_text(errors="ignore"), f


def test_run_error_is_inline(client, monkeypatch):
    async def fake_tool(name, args, caller, timeout=120.0):
        return {}, "Trino error: line 1:8: Column 'nope' cannot be resolved", True
    monkeypatch.setattr(ss, "call_tool", fake_tool)
    d = client.post("/ai/api/sql/run", json={"sql": "select nope"}).json()
    assert d["ok"] is False and "cannot be resolved" in d["error"]


PLAN = """Reads from 1 system(s): warehouse; 3 distributed stage(s).

Full plan (truncated):
```
Fragment 0 [SINGLE]
    Output[columnNames = [_col0]]
Fragment 1 [SOURCE]
       ├─ ScanFilterProject[table = billing_oracle:Query[SELECT "BAN" FROM "BILLING"."INVOICES"], filterPredicate = (x), dynamicFilters = {CAST(ban_0 AS varchar) = #df_264}]
       └─ TableScan[table = warehouse:marts.dim_customer bsp_edw.marts.dim_customer constraint on [technology] columns=[ban:varchar:text]]
Fragment 2 [SOURCE]
       └─ TableScan[table = care_mssql:dbo.care_orders dbo.care_orders limit=10, columns=[ban]]
```"""


def test_plan_summary_reads_all_scan_kinds():
    p = ss.plan_summary(PLAN, {"systems": ["warehouse"], "scans": ["warehouse: marts.dim_customer x"], "stages": 3})
    assert p["systems"] == ["billing_oracle", "care_mssql", "warehouse"] and p["stages"] == 3
    joined = " | ".join(p["pushdowns"])
    assert "join or aggregate pushed down" in joined and "filter: technology" in joined
    assert "dynamic filter (df_264)" in joined and "care_mssql.dbo.care_orders: limit" in joined


def test_referenced_tables_ignores_strings_and_comments():
    sql = "select 'a.b.c' from warehouse.marts.dim_customer c -- lake.x.y\njoin \"billing_oracle\".billing.invoices i on 1=1"
    assert ss.referenced_tables(sql) == ["warehouse.marts.dim_customer", "billing_oracle.billing.invoices"]
    assert ss.referenced_tables(sql, {"warehouse"}) == ["warehouse.marts.dim_customer"]


def test_extract_sql_prefers_answer_block():
    assert ss.extract_sql("Here:\n```sql\nSELECT 1\n```", ["SELECT 2"]) == ("SELECT 1", "answer")
    assert ss.extract_sql("no block", ["SELECT 2"]) == ("SELECT 2", "last query run")
    assert ss.extract_sql("", []) == ("", "")


def test_catalog_status_without_datahub(client, monkeypatch):
    monkeypatch.delenv("DATAHUB_TOKEN", raising=False)
    d = client.get("/ai/api/sql/catalog/status").json()
    assert d["backend"] == "trino" and "DataHub when it is running" in d["message"] and d["can_annotate"] is False


def test_system_schema_filter():
    assert ss.is_system_schema("lbacsys") and ss.is_system_schema("SYSBACKUP") and ss.is_system_schema("db_owner")
    assert not ss.is_system_schema("billing") and not ss.is_system_schema("marts") and not ss.is_system_schema("dbo")


# ── Trino monitor: query id lookup ──────────────────────────────────────────
TRINO_QUERIES = [
    {"queryId": "20261019_000001_00001_aaaaa", "state": "FINISHED", "queryTextPreview": "SELECT technology, count(*) FROM warehouse.marts.dim_customer GROUP BY 1",
     "queryStats": {"createTime": "2026-10-19T00:00:01.123456789Z"}},
    {"queryId": "20261019_000005_00002_bbbbb", "state": "RUNNING", "queryTextPreview": "SELECT technology,\n  count(*) FROM warehouse.marts.dim_customer GROUP BY 1",
     "queryStats": {"createTime": "2026-10-19T00:00:05.000000000Z"}},
    {"queryId": "20261019_000006_00003_ccccc", "state": "FINISHED", "queryTextPreview": "EXPLAIN (TYPE DISTRIBUTED) SELECT 42",
     "queryStats": {"createTime": "2026-10-19T00:00:06.000000000Z"}},
]


def test_match_query_newest_same_text_after_since():
    import datetime as dt
    since = int(dt.datetime(2026, 10, 19, 0, 0, 4, tzinfo=dt.timezone.utc).timestamp() * 1000)
    sql = "-- note\nselect technology, count(*)\nfrom warehouse.marts.dim_customer group by 1;"
    assert ss.match_query(TRINO_QUERIES, sql, 0)["queryId"].endswith("bbbbb")
    assert ss.match_query(TRINO_QUERIES, sql, since)["queryId"].endswith("bbbbb")
    late = int(dt.datetime(2026, 10, 19, 0, 1, 0, tzinfo=dt.timezone.utc).timestamp() * 1000)
    assert ss.match_query(TRINO_QUERIES, sql, late) is None
    assert ss.match_query(TRINO_QUERIES, "SELECT 42", 0, explain=True)["queryId"].endswith("ccccc")
    assert ss.match_query(TRINO_QUERIES, "SELECT 42", 0) is None


def test_trino_find_endpoint(client, monkeypatch):
    import httpx

    class Resp:
        status_code = 200
        def raise_for_status(self): pass
        def json(self): return TRINO_QUERIES

    async def fake_get(self, url, **kw):
        assert url.endswith("/ui/api/query")
        return Resp()

    monkeypatch.setattr(httpx.AsyncClient, "get", fake_get)
    d = client.post("/ai/api/sql/trino/find", json={"sql": "SELECT 42", "explain": True, "since": 0}).json()
    assert d == {"found": True, "query_id": "20261019_000006_00003_ccccc", "state": "FINISHED", "ui": "/trino/ui/#/queries/20261019_000006_00003_ccccc"}
    assert client.post("/ai/api/sql/trino/find", json={"sql": "select nothing like this"}).json() == {"found": False, "ui": "/trino/ui/"}
    assert client.post("/ai/api/sql/trino/find", json={}).status_code == 400


# ── refresh scope defaults and persisted jobs ───────────────────────────────
def test_default_scope_prefers_tables_then_schema_then_system_then_all():
    assert ss.default_scope({"catalog": "warehouse", "schema": "marts", "tables": ["a", "b"]}) == {"catalog": "warehouse", "schema": "marts", "tables": ["a", "b"]}
    assert ss.default_scope({"catalog": "warehouse", "schema": "marts", "tables": []}) == {"catalog": "warehouse", "schema": "marts", "tables": []}
    assert ss.default_scope({"catalog": "warehouse", "tables": ["a"]}) == {"catalog": "warehouse", "schema": None, "tables": []}
    assert ss.default_scope({}) == {"all": True} and ss.default_scope(None) == {"all": True}


@pytest.fixture()
def fresh_jobs(monkeypatch):
    monkeypatch.setattr(ss, "_jobs", {})
    monkeypatch.setattr(ss, "_jobs_loaded", False)
    return ss


def test_jobs_persist_and_running_ones_become_interrupted(fresh_jobs):
    ss.job_save({"job_id": "aaaaaaaaaaaa", "user": "ana", "state": "running", "scope": {"catalog": "w"}, "scopes": [{"catalog": "w"}],
                 "log": ["started"], "counts": {}, "started": 1.0, "finished": None})
    ss.job_save({"job_id": "bbbbbbbbbbbb", "user": "bob", "state": "succeeded", "scope": {"catalog": "w"}, "scopes": [{"catalog": "w"}],
                 "log": [], "counts": {"tables_scanned": 3}, "started": 2.0, "finished": 3.0})
    ss.jobs_init()   # a new process: nothing drives the running job any more
    a = ss._jobs["aaaaaaaaaaaa"]
    assert a["state"] == "interrupted" and a["finished"] and "interrupted" in a["log"][-1]
    on_disk = json.loads((ss._jobs_dir() / "aaaaaaaaaaaa.json").read_text())
    assert on_disk["state"] == "interrupted"
    assert ss._jobs["bbbbbbbbbbbb"]["state"] == "succeeded"
    assert not list(ss._jobs_dir().glob("*.tmp"))


def test_jobs_keep_last_100(fresh_jobs):
    import os
    for i in range(105):
        jid = f"j{i:011d}"
        ss.job_save({"job_id": jid, "user": "ana", "state": "succeeded", "scopes": [], "log": [], "counts": {}, "started": i})
        os.utime(ss._jobs_dir() / f"{jid}.json", (1000 + i, 1000 + i))
    ss._prune_jobs()
    names = sorted(p.stem for p in ss._jobs_dir().glob("*.json"))
    assert len(names) == 100 and names[0] == "j00000000005"


def test_job_endpoints_visibility_retry_and_selection_default(client, as_nginx, fresh_jobs, monkeypatch):
    started = []

    async def fake_run(j):
        started.append(j["scopes"])
        j.update(state="succeeded", finished=2.0)
        ss.job_save(j)

    async def no_datahub():
        return None

    monkeypatch.setattr(ss, "run_job", fake_run)
    monkeypatch.setattr(ss, "datahub", no_datahub)
    r = client.post("/ai/api/sql/catalog/refresh", json={"selection": {"catalog": "warehouse", "schema": "marts", "tables": ["dim_customer"]}}, headers=hdr("ana"))
    assert r.status_code == 200
    jid = r.json()["job_id"]
    assert r.json()["job"]["scope"] == {"catalog": "warehouse", "schema": "marts", "tables": ["dim_customer"]}
    assert client.get(f"/ai/api/sql/catalog/jobs/{jid}", headers=hdr("bob")).status_code == 404
    assert client.get("/ai/api/sql/catalog/jobs", headers=hdr("bob")).json()["jobs"] == []
    admin = {"X-Auth-User": "root", "X-Auth-Role": "admin"}
    assert [j["job_id"] for j in client.get("/ai/api/sql/catalog/jobs", headers=admin).json()["jobs"]] == [jid]
    d = client.get(f"/ai/api/sql/catalog/jobs/{jid}?full=true", headers=hdr("ana")).json()
    assert d["user"] == "ana" and "re-reading structure from Trino" in d["log"]
    # interrupted job can be retried with the same scopes
    ss._jobs[jid]["state"] = "interrupted"
    r2 = client.post(f"/ai/api/sql/catalog/jobs/{jid}/retry", headers=hdr("ana")).json()
    assert r2["job"]["retry_of"] == jid and started[-1] == started[0]
    assert client.post(f"/ai/api/sql/catalog/jobs/{jid}/retry", headers=hdr("bob")).status_code == 404


def test_refresh_all_systems_uses_trino_catalogs(client, as_nginx, fresh_jobs, monkeypatch):
    async def cats(user):
        return [{"catalog_name": n, "connector_name": c} for n, c in
                [("warehouse", "postgresql"), ("system", "system"), ("tpch", "tpch"), ("bq_x", "bigquery")]]

    async def fake_run(j):
        j.update(state="succeeded")

    async def no_datahub():
        return None

    monkeypatch.setattr(ss, "trino_catalogs", cats)
    monkeypatch.setattr(ss, "run_job", fake_run)
    monkeypatch.setattr(ss, "datahub", no_datahub)
    d = client.post("/ai/api/sql/catalog/refresh", json={"selection": {}}, headers=hdr("ana")).json()["job"]
    assert d["scope"] == {"all": True} and [s["catalog"] for s in d["scopes"]] == ["warehouse", "bq_x"]
    assert d["reannotate"] is False


# ── glossary view ────────────────────────────────────────────────────────────
def test_glossary_view_groups_sources_and_link_provenance(client, monkeypatch):
    from bsp_common import catalog as real

    TERMS = [{"urn": "urn:li:glossaryTerm:bsp.ban", "name": "ban",
              "properties": {"name": "BAN", "definition": "Billing *account* number", "termSource": "INTERNAL", "sourceRef": "DataPlatform Data Governance"},
              "parentNodes": {"nodes": [{"urn": "n1", "properties": {"name": "Billing & Revenue"}}]}},
             {"urn": "urn:li:glossaryTerm:cx.churn_flag", "name": "churn_flag",
              "properties": {"name": "Churn flag", "definition": "Imported", "termSource": "EXTERNAL", "sourceRef": "BigQuery vco_cx_out"},
              "parentNodes": {"nodes": []}},
             {"urn": "urn:li:glossaryTerm:vco-cx-out.order-status", "name": "order-status",
              "properties": {"name": "Order status", "definition": "Status of the order. (AI-suggested)"},
              "parentNodes": {"nodes": [{"urn": "urn:li:glossaryNode:vco-cx-out", "properties": {"name": "VCO CX Out (dev BigQuery)", "description": None}}]}}]
    pg = "urn:li:dataset:(urn:li:dataPlatform:postgres,bsp_edw.marts.dim_customer,PROD)"
    tr = "urn:li:dataset:(urn:li:dataPlatform:trino,warehouse.marts.dim_customer,PROD)"
    tr2 = "urn:li:dataset:(urn:li:dataPlatform:trino,care_mssql.dbo.care_orders,PROD)"
    DS = {"glossaryTerms": [
        {"urn": tr, "platform": {"name": "trino"}, "tags": {"tags": [{"tag": {"urn": "urn:li:tag:ai-annotated"}}]},
         "glossaryTerms": {"terms": [{"term": {"urn": "urn:li:glossaryTerm:bsp.ban"}}]}, "schemaMetadata": {"fields": []}},
        {"urn": tr2, "platform": {"name": "trino"}, "tags": {"tags": [{"tag": {"urn": "urn:li:tag:ai-annotated"}}]},
         "glossaryTerms": {"terms": [{"term": {"urn": "urn:li:glossaryTerm:bsp.ban"}}]}, "schemaMetadata": {"fields": []}}],
        "fieldGlossaryTerms": [
        {"urn": pg, "platform": {"name": "postgres"}, "tags": None, "glossaryTerms": None,
         "schemaMetadata": {"fields": [{"glossaryTerms": {"terms": [{"term": {"urn": "urn:li:glossaryTerm:bsp.ban"}}]}}]}}]}

    class Fake:
        name_from_urn = staticmethod(real.name_from_urn)
        is_ai_text = staticmethod(real.is_ai_text)
        platform_from_urn = staticmethod(real.platform_from_urn)

        @staticmethod
        def _gql(q, variables):
            inp = variables["input"]
            if inp["types"] == ["GLOSSARY_TERM"]:
                rows = TERMS
            else:
                rows = DS[inp["orFilters"][0]["and"][0]["field"]]
            return {"searchAcrossEntities": {"total": len(rows), "searchResults": [{"entity": e} for e in rows]}}

    async def fake_dh():
        return Fake

    monkeypatch.setattr(ss, "datahub", fake_dh)
    ss._gl_cache.update(at=0.0, data=None)
    d = client.get("/ai/api/sql/glossary").json()
    assert d["source"] == "datahub" and {g["name"] for g in d["groups"]} == {"Billing & Revenue", "Ungrouped", "VCO CX Out (dev BigQuery)"}
    ban = next(t for t in d["terms"] if t["name"] == "BAN")
    assert ban["source"] == "curated" and ban["definition"] == "Billing *account* number"
    assert ban["datahub_url"].endswith("/glossaryTerm/urn:li:glossaryTerm:bsp.ban")
    rows = {x["trino"]: x for x in ban["tables"]}
    assert ban["table_count"] == 2   # postgres + trino copies of dim_customer merge into one row
    assert rows["warehouse.marts.dim_customer"]["provenance"] == "curated" and rows["warehouse.marts.dim_customer"]["also_ai"]
    assert rows["care_mssql.dbo.care_orders"]["provenance"] == "ai"
    assert next(t for t in d["terms"] if t["name"] == "Churn flag")["source"] == "imported"
    vco = next(t for t in d["terms"] if t["name"] == "Order status")
    assert vco["source"] == "imported" and vco["ai_definition"] is True and vco["group"] == "VCO CX Out (dev BigQuery)"
    assert client.get("/ai/api/sql/glossary").json()["cached"] is True
    assert client.get("/ai/api/sql/glossary?refresh=true").json()["cached"] is False


def test_trino_name_mapping():
    assert ss.trino_name("postgres", "bsp_edw.marts.fct_invoice") == "warehouse.marts.fct_invoice"
    assert ss.trino_name("bigquery", "da-rpt-vco-cn-opq1.cx_datamart.t") == "bq_rpt_vco_cn_opq1.cx_datamart.t"
    assert ss.trino_name("trino", "care_mssql.dbo.care_orders") == "care_mssql.dbo.care_orders"
    assert ss.trino_name("looker", "x.y") is None
