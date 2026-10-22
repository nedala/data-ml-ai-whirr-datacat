"""Import & Export (/ingress/) without a database: SQL read-only guard, type
inference, column mapping, URL handling (incl. the egress-proxy 403
message), sample-data wiring and the API's validation paths."""
from tests import _env  # noqa: F401  (must be first: sets env)
from tests._env import H, TMP, FakeJob, wait_job

import os
import sys
import textwrap
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from fastapi.testclient import TestClient

import app as wbapp
from wb import config, dataio, tabular
from wb import warehouse as W
from wb.jobs import JobError

LAKE = TMP / "lake"


class ReadOnlyGuard(unittest.TestCase):
    def test_accepts_reads(self):
        for sql in ("SELECT 1", "select * from gold.x;", "  WITH a AS (SELECT 1) SELECT * FROM a",
                    "-- comment\nSELECT ';' AS semi", "/* x; y */ SELECT 1", "SELECT $$a;b$$",
                    "(SELECT 1) UNION (SELECT 2)", "TABLE landing.t", "VALUES (1),(2)", 'SELECT 1 AS ";"'):
            self.assertTrue(W.check_select(sql), sql)

    def test_rejects_writes_and_multi_statements(self):
        for sql in ("", "   ", "DELETE FROM landing.t", "SELECT 1; DROP TABLE landing.t", "select 1;commit;drop table x",
                    "INSERT INTO t VALUES (1)", "CREATE TABLE x (a int)", "COPY t TO '/tmp/x'",
                    "SELECT 1 -- ok\n; DELETE FROM t", "SET default_transaction_read_only = off",
                    "SELECT $q$;$q$; DROP TABLE t", "BEGIN; SELECT 1"):
            with self.assertRaises(W.WarehouseError, msg=sql):
                W.check_select(sql)


class Tabular(unittest.TestCase):
    def test_sql_name(self):
        used = set()
        self.assertEqual(tabular.sql_name("Policy No.", used), "policy_no")
        self.assertEqual(tabular.sql_name("policy-no", used), "policy_no_2")
        self.assertEqual(tabular.sql_name("2024 Premium (GBP)"), "c_2024_premium_gbp")
        self.assertEqual(tabular.sql_name("€€€"), "column")
        self.assertEqual(len(tabular.sql_name("x" * 100)), 63)
        self.assertEqual(tabular.table_name("Q3 Invoices.xlsx"), "q3_invoices")

    def test_friendly_types(self):
        for duck, fr in (("BIGINT", "integer"), ("DECIMAL(18,2)", "number"), ("DOUBLE", "number"), ("DATE", "date"),
                         ("TIMESTAMP WITH TIME ZONE", "timestamp"), ("BOOLEAN", "boolean"), ("VARCHAR", "text"),
                         ("STRUCT(a INTEGER)", "json"), ("INTEGER[]", "json"), ("TIME", "text")):
            self.assertEqual(tabular.friendly_type(duck), fr, duck)

    def test_inspect_options(self):
        p = LAKE / "landing" / "semi.csv"
        p.write_text("Line;Amount\nA;1,5\nB;2\n")
        out = tabular.inspect(p, delimiter="semicolon")
        self.assertEqual([c["name"] for c in out["columns"]], ["Line", "Amount"])
        p2 = LAKE / "landing" / "latin.csv"
        p2.write_bytes("Invoice,Ville\n3100000008-2609,Montr\xe9al\n".encode("latin-1"))
        with self.assertRaises(tabular.TabularError) as cm:
            tabular.inspect(p2)
        self.assertIn("encoding", str(cm.exception))
        out = tabular.inspect(p2, encoding="latin-1")
        self.assertEqual(out["rows"], [["3100000008-2609", "Montréal"]])
        p3 = LAKE / "landing" / "noheader.csv"
        p3.write_text("junk line\n1,2\n3,4\n")
        out = tabular.inspect(p3, header=False, skip_rows=1)
        self.assertEqual(out["total_rows"], 2)

    def test_select_list_validation(self):
        types = {"A": "VARCHAR", "B": "BIGINT"}
        sql, chosen = tabular.select_list([{"name": "A", "target": "a", "type": "boolean"}, {"name": "B", "target": "b", "type": "text", "include": False}], types)
        self.assertEqual(chosen, [("A", "a", "boolean")])
        self.assertIn("lower(trim", sql)
        for cols in ([{"name": "A", "target": "Bad Name", "type": "text"}],
                     [{"name": "A", "target": "x", "type": "text"}, {"name": "B", "target": "x", "type": "text"}],
                     [{"name": "Z", "target": "z", "type": "text"}],
                     [{"name": "A", "target": "a", "type": "money"}],
                     [{"name": "A", "target": "a", "type": "text", "include": False}]):
            with self.assertRaises(tabular.TabularError, msg=cols):
                tabular.select_list(cols, types)


class UrlFetch(unittest.TestCase):
    def test_check_url(self):
        for bad in ("http://example.com/a.csv", "ftp://x/y", "https://user:pw@example.com/a.csv", "javascript:alert(1)", "https:///x"):
            with self.assertRaises(tabular.TabularError, msg=bad):
                dataio.check_url(bad)
        self.assertEqual(dataio.check_url("https://data.gov.uk/x.csv").hostname, "data.gov.uk")

    def test_proxy_403_is_explained(self):
        class Deny(BaseHTTPRequestHandler):
            def do_CONNECT(self):  # noqa: N802
                self.send_response(403, "Forbidden")
                self.end_headers()

            def log_message(self, *a):
                pass

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Deny)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        os.environ["HTTPS_PROXY"] = f"http://127.0.0.1:{srv.server_address[1]}"
        try:
            fn = dataio.fetch_url_job("https://not-allowed.example.com/data.csv")
            with self.assertRaises(JobError) as cm:
                fn(FakeJob())
            self.assertIn("allowlist", str(cm.exception))
            self.assertIn("not-allowed.example.com", str(cm.exception))
            self.assertEqual(list((LAKE / "landing").glob(".download-*")), [])
        finally:
            os.environ.pop("HTTPS_PROXY", None)
            srv.shutdown()

    def test_url_filename(self):
        import urllib.parse as up
        u = up.urlsplit("https://h.example/a/b/Q3%20claims.csv?x=1")
        self.assertEqual(dataio._url_filename(u, None), "Q3 claims.csv")
        self.assertEqual(dataio._url_filename(u, 'attachment; filename="../../evil.sh"'), "evil.sh")


class ExportNames(unittest.TestCase):
    def test_export_name(self):
        self.assertEqual(dataio.export_name("losses", "excel"), "losses.xlsx")
        self.assertEqual(dataio.export_name("losses.csv", "parquet"), "losses.parquet")
        self.assertEqual(dataio.export_name("../../x", "csv"), "x.csv")
        self.assertTrue(dataio.export_name(None, "csv").startswith("export-"))

    def test_export_token_validation(self):
        from wb.fsroots import PathError
        for tok in ("../../etc/passwd", "abc", "g" * 32):
            with self.assertRaises(PathError):
                dataio.export_file(tok)


class IngressApi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(wbapp.app)
        (LAKE / "landing" / "claims.csv").write_text("Claim ID,Paid,Loss Date\nC1,10.5,2024-01-03\nC2,,2024-02-04\n")

    def test_status_without_warehouse(self):
        s = self.c.get("/ingress/api/status").json()
        self.assertFalse(s["warehouse"]["ok"])
        self.assertIn("error", s["warehouse"])
        self.assertFalse(s["bigquery"]["configured"])

    def test_inspect(self):
        r = self.c.post("/ingress/api/inspect", json={"root": "lake", "path": "landing/claims.csv"}, headers=H)
        self.assertEqual(r.status_code, 200, r.text)
        j = r.json()
        self.assertEqual(j["suggested_table"], "claims")
        self.assertEqual([(c["target"], c["type"]) for c in j["columns"]], [("claim_id", "text"), ("paid", "number"), ("loss_date", "date")])
        self.assertIsNone(j["table_exists"])  # warehouse is down in this test

    def test_inspect_rejects_bad_paths(self):
        r = self.c.post("/ingress/api/inspect", json={"root": "lake", "path": "../etc/passwd"}, headers=H)
        self.assertEqual(r.status_code, 400)
        r = self.c.post("/ingress/api/inspect", json={"root": "lake", "path": "landing"}, headers=H)
        self.assertEqual(r.status_code, 400)

    def test_load_validation(self):
        r = self.c.post("/ingress/api/load", json={"root": "lake", "path": "landing/claims.csv", "table": "Bad Name"}, headers=H)
        self.assertEqual(r.status_code, 400)
        r = self.c.post("/ingress/api/load", json={"root": "lake", "path": "landing/claims.csv", "table": "ok", "mode": "merge"}, headers=H)
        self.assertEqual(r.status_code, 422)

    def test_load_job_reports_unreachable_warehouse(self):
        r = self.c.post("/ingress/api/load", json={"root": "lake", "path": "landing/claims.csv", "table": "claims"}, headers=H)
        self.assertEqual(r.status_code, 200, r.text)
        s = wait_job(self.c, "/ingress", r.json()["job"]["id"])
        self.assertEqual(s["status"], "failed")
        self.assertIn("warehouse", s["error"])

    def test_query_guard(self):
        r = self.c.post("/ingress/api/query", json={"sql": "DROP TABLE landing.claims"}, headers=H)
        self.assertEqual(r.status_code, 400)
        self.assertIn("read queries", r.json()["detail"])

    def test_export_validation(self):
        r = self.c.post("/ingress/api/export", json={"format": "csv"}, headers=H)
        self.assertEqual(r.status_code, 422)
        r = self.c.post("/ingress/api/export", json={"sql": "SELECT 1", "format": "xml"}, headers=H)
        self.assertEqual(r.status_code, 422)
        self.assertEqual(self.c.get("/ingress/api/export/" + "0" * 32).status_code, 404)

    def test_url_endpoint_validation(self):
        r = self.c.post("/ingress/api/url", json={"url": "http://example.com/x.csv"}, headers=H)
        self.assertEqual(r.status_code, 422)

    def test_bigquery_not_configured(self):
        r = self.c.post("/ingress/api/bigquery/load", json={"sql": "SELECT 1", "table": "x"}, headers=H)
        self.assertEqual(r.status_code, 409)


class Samples(unittest.TestCase):
    """The sample-data tab drives `python -m bsp_seed --target postgres|parquet`."""

    @classmethod
    def setUpClass(cls):
        pkg = TMP / "fakeseed" / "bsp_seed"
        pkg.mkdir(parents=True, exist_ok=True)
        (pkg / "__init__.py").write_text("")
        (pkg / "__main__.py").write_text(textwrap.dedent(f"""
            import sys
            t = sys.argv[sys.argv.index("--target") + 1]
            open({str(TMP / 'seed.log')!r}, "a").write(f"isp:{{t}}\\n")
            print(f"loaded isp into {{t}}")
        """))
        cls.old = config.PYTHON, os.environ.get("PYTHONPATH", "")
        config.PYTHON = sys.executable
        os.environ["PYTHONPATH"] = f"{TMP / 'fakeseed'}:{cls.old[1]}"
        sys.path.insert(0, str(TMP / "fakeseed"))
        cls.c = TestClient(wbapp.app)

    @classmethod
    def tearDownClass(cls):
        config.PYTHON = cls.old[0]
        os.environ["PYTHONPATH"] = cls.old[1]

    def test_list_and_load(self):
        for base in ("/pkg/api/samples", "/ingress/api/samples"):
            ds = self.c.get(base).json()["datasets"]
            self.assertEqual([d["id"] for d in ds], ["isp"])
            self.assertEqual(ds[0]["targets"], ["warehouse", "lake"])
        r = self.c.post("/ingress/api/samples/load", json={"ids": ["isp"], "target": "warehouse"}, headers=H)
        s = wait_job(self.c, "/ingress", r.json()["job"]["id"])
        self.assertEqual(s["status"], "succeeded", s)
        r = self.c.post("/pkg/api/samples/hydrate", json={"ids": ["isp"], "target": "lake"}, headers=H)
        s = wait_job(self.c, "/pkg", r.json()["job"]["id"])
        self.assertEqual(s["status"], "succeeded", s)
        self.assertEqual((TMP / "seed.log").read_text().split(), ["isp:postgres", "isp:parquet"])
        ds = self.c.get("/ingress/api/samples").json()["datasets"]
        self.assertTrue(ds[0]["hydrated"]["warehouse"])

    def test_bad_targets(self):
        r = self.c.post("/ingress/api/samples/load", json={"ids": ["isp"], "target": "workspace"}, headers=H)
        self.assertEqual(r.status_code, 422)
        r = self.c.post("/ingress/api/samples/load", json={"ids": ["nope"], "target": "warehouse"}, headers=H)
        self.assertEqual(r.status_code, 422)
        r = self.c.post("/ingress/api/samples/load", json={"ids": ["../x"], "target": "warehouse"}, headers=H)
        self.assertEqual(r.status_code, 422)


class PackagesApi(unittest.TestCase):
    def test_python_only(self):
        c = TestClient(wbapp.app)
        s = c.get("/pkg/api/status").json()
        self.assertEqual(list(s), ["python"])
        self.assertEqual(c.get("/pkg/api/r/packages").status_code, 404)
        self.assertEqual(c.get("/pkg/api/julia/packages").status_code, 404)
        self.assertEqual(set(c.get("/pkg/api/manifest").json()), {"python"})


if __name__ == "__main__":
    unittest.main()


class BigQueryPrincipal(unittest.TestCase):
    """The card's project comes from the BigQuery · read assignment (not
    GCP_PROJECT_ID), and "Acting as" a named identity goes through Studio for
    the signed-in user only."""
    ENV = {"GCP_BQ_JSON": '{"type": "authorized_user", "client_id": "x", "client_secret": "y", "refresh_token": "z"}',
           "GCP_BQ_PROJECT": "da-one-proj", "GCP_BQ_DATA_PROJECTS": "da-one-proj,da-two-proj", "GCP_BQ_EXEC_PROJECT": "da-jobs-proj",
           "GCP_PROJECT_ID": "wrong-default-proj", "GCP_ASSIGNMENTS": '{"bigquery_read": "Seshu"}',
           "GCP_IDENTITIES": '{"Seshu": {"id": "seshu", "type": "authorized_user", "principal": "seshu@example.com", "project": "da-one-proj"},'
                             ' "Other": {"id": "other", "type": "service_account", "principal": "sa@p.iam.gserviceaccount.com", "project": "other-proj"}}'}

    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(wbapp.app)
        cls.saved = {k: os.environ.get(k) for k in cls.ENV}
        os.environ.update(cls.ENV)

    @classmethod
    def tearDownClass(cls):
        for k, v in cls.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_params_follow_the_assignment(self):
        p = config.bigquery_params()
        self.assertEqual(p["project"], "da-one-proj")
        self.assertEqual(p["data_projects"], ["da-one-proj", "da-two-proj"])
        self.assertEqual(p["exec_project"], "da-jobs-proj")
        self.assertEqual(p["identity"], "Seshu")
        self.assertEqual(p["principal"], "seshu@example.com")
        self.assertNotIn("wrong-default-proj", str(p))

    def test_named_identity_needs_signed_in_user(self):
        r = self.c.get("/ingress/api/bigquery/datasets?identity=other")
        self.assertEqual(r.status_code, 403)
        r = self.c.get("/ingress/api/bigquery/datasets?identity=other", headers={"X-Auth-User": "mallory"})
        self.assertEqual(r.status_code, 403, "a gate header without the Studio session is not enough")
        r = self.c.post("/ingress/api/bigquery/load", json={"sql": "SELECT 1", "table": "x", "identity": "other"}, headers=H)
        self.assertEqual(r.status_code, 403)

    def _fake_bq(self):
        seen = {}

        class DS:
            def __init__(self, i):
                self.dataset_id = i

        class Client:
            def __init__(self, project, credentials, location):
                seen.update(exec=project, token=getattr(credentials, "token", None), location=location)

            def list_datasets(self, project, max_results):
                seen["data"] = project
                return [DS("raw"), DS("marts")]
        return seen, Client

    def test_studio_refusal_is_a_403(self):
        from bsp_common import gcp

        def refuse(*a, **k):
            raise gcp.StudioTokenError("mallory may not act as Other", 403)
        real = gcp.studio_token
        gcp.studio_token = refuse
        try:
            r = self.c.get("/ingress/api/bigquery/datasets?identity=other", headers={"X-Auth-User": "mallory", "Cookie": "studio_session=s1"})
        finally:
            gcp.studio_token = real
        self.assertEqual(r.status_code, 403)
        self.assertIn("may not act as", r.json()["detail"])

    def test_switch_lists_the_chosen_project_as_the_user(self):
        from google.cloud import bigquery
        from bsp_common import gcp
        calls = []

        def token(identity, user=None, scopes=None, consumer=""):
            calls.append((identity, gcp.acting_user(), dict(gcp._acting.get() or {}).get("session"), consumer))
            return {"access_token": "ya29.test", "expires_in": 600, "identity": identity, "principal_email": "sa@p.iam.gserviceaccount.com",
                    "default_project": "other-proj", "quota_project": ""}
        seen, Fake = self._fake_bq()
        real_tok, real_client = gcp.studio_token, bigquery.Client
        gcp.studio_token, bigquery.Client = token, Fake
        try:
            r = self.c.get("/ingress/api/bigquery/datasets?identity=other&project=da-tsa-proj&exec_project=other-proj",
                           headers={"X-Auth-User": "anna", "Cookie": "studio_session=sess-anna"})
            r2 = self.c.get("/ingress/api/bigquery/datasets?project=da-two-proj")
        finally:
            gcp.studio_token, bigquery.Client = real_tok, real_client
        self.assertEqual(r.status_code, 200, r.text)
        j = r.json()
        self.assertEqual(j["datasets"], ["marts", "raw"])
        self.assertEqual(j["project"], "da-tsa-proj")
        self.assertEqual(j["acting"]["principal"], "sa@p.iam.gserviceaccount.com")
        self.assertEqual(j["acting"]["exec_project"], "other-proj")
        self.assertNotIn("ya29", r.text)
        self.assertEqual(calls[0], ("other", "anna", "sess-anna", "bigquery_read"))
        # the assigned default: data project from the picker, jobs in the assignment's execution project
        self.assertEqual(r2.status_code, 200, r2.text)
        self.assertEqual(r2.json()["project"], "da-two-proj")
        self.assertEqual(seen["exec"], "da-jobs-proj")
        self.assertTrue(r2.json()["acting"]["default"])

    def test_bad_project_rejected(self):
        r = self.c.get("/ingress/api/bigquery/datasets?project=Bad%20Proj")
        self.assertEqual(r.status_code, 400)
