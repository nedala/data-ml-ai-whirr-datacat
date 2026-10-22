"""dbt Studio (/dbt/) — YAML round-trip, wizard SQL generation, path safety
and an API smoke test. Offline: dbt commands go to a fake ``dbt`` that
prints JSON log lines; the lineage test uses the real ``dbt parse`` when dbt
is installed (no database needed).

    docker run --rm --entrypoint bash -v $PWD:/repo:ro bsp/appliance:latest -c \
      'cp -r /repo/build/workbench /tmp/wb && cd /tmp/wb && \
       python -m unittest tests.test_dbt_studio -v'
"""
from tests import _env  # noqa: F401  (must be first: sets env)
from tests._env import H, wait_job

import json
import os
import shutil
import stat
import subprocess
import sys
import textwrap
import unittest
from pathlib import Path

import yaml

WORK = _env.TMP / "work"
WORK.mkdir(parents=True, exist_ok=True)
os.environ["WORKBENCH_DBT_ROOT"] = str(WORK)
os.environ["DBT_TARGET"] = "postgres"
os.environ["PG_HOST"] = "127.0.0.1"
os.environ["PG_PORT"] = "1"

FAKE_DBT = _env.TMP / "bin" / "dbt"
FAKE_DBT.parent.mkdir(parents=True, exist_ok=True)
FAKE_DBT.write_text(textwrap.dedent(f"""\
    #!{sys.executable}
    import json, os, sys
    args = sys.argv[1:]
    def ev(name, msg, data=None):
        print(json.dumps({{"info": {{"name": name, "msg": msg, "ts": "2026-01-01T10:00:00Z"}}, "data": data or {{}}}}), flush=True)
    ev("MainReportVersion", "Running with dbt=1.12.5")
    print("plain stderr-ish line", flush=True)
    ev("Env", "profiles=" + os.environ.get("DBT_PROFILES_DIR", "-") + " target=" + os.environ.get("DBT_TARGET", "-"))
    ev("Args", "args=" + " ".join(args))
    if args[0] == "show":
        ev("ShowNode", "SECRET-ROWS", {{"node_name": "int_tickets", "preview": json.dumps([{{"ticket_id": "C1", "minutes_open": 10.5}}])}})
    if args[0] == "compile":
        ev("CompiledNode", "Compiled", {{"node_name": "int_tickets", "compiled": "select 1"}})
    sys.exit(1 if "fail_me" in " ".join(args) else 0)
"""))
FAKE_DBT.chmod(FAKE_DBT.stat().st_mode | stat.S_IEXEC)
os.environ["WORKBENCH_DBT"] = str(FAKE_DBT)

from fastapi.testclient import TestClient  # noqa: E402

import app as wbapp  # noqa: E402
from wb import dbt_project as dp  # noqa: E402
from wb import dbt_sql, dbt_yaml  # noqa: E402

SRC = Path(os.environ.get("WBTEST_DBT_PROJECT") or Path(__file__).resolve().parent / "fixtures" / "dbt_project")
REAL_DBT = shutil.which("dbt")


def fresh_project(name: str = "dbt_project") -> Path:
    if not (SRC / "dbt_project.yml").is_file():
        raise unittest.SkipTest(f"dbt project not found at {SRC} (set WBTEST_DBT_PROJECT)")
    d = WORK / name
    shutil.rmtree(d, ignore_errors=True)
    shutil.copytree(SRC, d, ignore=shutil.ignore_patterns("target", "logs", ".user.yml"))
    return d


_PARSED: dict = {}


def parsed_project() -> Path:
    """A project copy with target/manifest.json from a real `dbt parse`."""
    if not REAL_DBT:
        raise unittest.SkipTest("dbt not installed")
    if "d" not in _PARSED:
        d = fresh_project("parsed")
        env = {**os.environ, "DBT_PROFILES_DIR": str(d), "DBT_SEND_ANONYMOUS_USAGE_STATS": "false",
               "PYTHONWARNINGS": "ignore"}
        r = subprocess.run([REAL_DBT, "parse"], cwd=d, env=env, capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stdout[-2000:] + r.stderr[-2000:]
        _PARSED["d"] = d
    return _PARSED["d"]


class YamlRoundTrip(unittest.TestCase):
    def setUp(self):
        self.d = fresh_project()

    def test_unchanged_files_round_trip_byte_identical(self):
        projects = [self.d]
        for d in projects:
            for p in dbt_yaml.yml_files(d):
                text = p.read_text()
                self.assertEqual(dbt_yaml.dumps(dbt_yaml.load(p)), text, p.name)

    def test_read_existing_props(self):
        props = dbt_yaml.get_model_props(self.d, "int_tickets")
        self.assertEqual(props["file"], "models/intermediate/_intermediate.yml")
        cols = {c["name"]: c for c in props["columns"]}
        self.assertIn("unique", cols["ticket_id"]["tests"])
        self.assertEqual(cols["is_outage"]["tests"]["accepted_values"]["values"], ["0", "1"])
        rel = cols["ban"]["tests"]["relationships"]
        self.assertEqual((rel["to"], rel["field"], rel["severity"]), ("int_accounts", "ban", "warn"))

    def test_write_props_preserves_everything_else(self):
        path = self.d / "models/intermediate/_intermediate.yml"
        before = yaml.safe_load(path.read_text())
        dbt_yaml.set_model_props(self.d, "int_tickets", {
            "description": "Claims, latest report wins.",
            "materialized": "table", "tags": ["service", "finance"],
            "columns": [
                {"name": "ticket_id", "description": "Customer ticket reference",
                 "tests": {"unique": {}, "not_null": {}}},
                {"name": "status", "description": "", "tests": {"accepted_values": {"values": ["Open", "Closed"]}}},
                {"name": "is_outage", "tests": {"accepted_values": {"values": ["0", "1"]}}},
                {"name": "ban", "tests": {"relationships": {"to": "int_accounts", "field": "ban", "severity": "warn"}}},
                {"name": "minutes_open", "tests": {}},
                {"name": "never_documented", "description": "", "tests": {}},
            ]})
        after = yaml.safe_load(path.read_text())
        # other models untouched
        self.assertEqual([m for m in after["models"] if m["name"] != "int_tickets"],
                         [m for m in before["models"] if m["name"] != "int_tickets"])
        m = next(x for x in after["models"] if x["name"] == "int_tickets")
        self.assertEqual(m["description"], "Claims, latest report wins.")
        self.assertEqual(m["config"], {"materialized": "table", "tags": ["service", "finance"]})
        cols = {c["name"]: c for c in m["columns"]}
        self.assertEqual(cols["ticket_id"]["description"], "Customer ticket reference")
        self.assertEqual(cols["ticket_id"]["tests"], ["unique", "not_null"])
        self.assertEqual(cols["status"]["tests"], [{"accepted_values": {"arguments": {"values": ["Open", "Closed"]}}}])
        self.assertEqual(cols["is_outage"]["tests"], [{"accepted_values": {"arguments": {"values": [0, 1], "quote": False}}}])
        self.assertEqual(cols["ban"]["tests"][0]["relationships"]["config"], {"severity": "warn"})
        self.assertNotIn("tests", cols["minutes_open"])
        self.assertNotIn("never_documented", cols)
        # reading back gives the same answers
        props = dbt_yaml.get_model_props(self.d, "int_tickets")
        self.assertEqual(props["materialized"], "table")
        self.assertEqual(props["tags"], ["service", "finance"])
        # clearing removes config and tests
        dbt_yaml.set_model_props(self.d, "int_tickets", {"materialized": None, "tags": [], "columns": [
            {"name": "status", "tests": {}}]})
        m = next(x for x in yaml.safe_load(path.read_text())["models"] if x["name"] == "int_tickets")
        self.assertNotIn("config", m)
        self.assertNotIn("tests", next(c for c in m["columns"] if c["name"] == "status"))

    def test_comments_survive(self):
        path = self.d / "models/intermediate/_intermediate.yml"
        path.write_text("# keep me\n" + path.read_text())
        dbt_yaml.set_model_props(self.d, "int_accounts", {"description": "Accounts"})
        self.assertTrue(path.read_text().startswith("# keep me\n"))

    def test_new_model_gets_layer_file(self):
        (self.d / "models/marts/my_mart.sql").write_text("select 1 as x\n")
        dbt_yaml.set_model_props(self.d, "my_mart", {"description": "Mine"})
        data = yaml.safe_load((self.d / "models/marts/_marts.yml").read_text())
        self.assertIn({"name": "my_mart", "description": "Mine"}, data["models"])
        (self.d / "models/scratch").mkdir()
        (self.d / "models/scratch/tmp_thing.sql").write_text("select 1 as x\n")
        dbt_yaml.set_model_props(self.d, "tmp_thing", {"description": "Scratch"})
        data = yaml.safe_load((self.d / "models/scratch/_scratch.yml").read_text())
        self.assertEqual(data["version"], 2)
        self.assertEqual(data["models"], [{"name": "tmp_thing", "description": "Scratch"}])

    def test_rejects_jinja_and_bad_names(self):
        with self.assertRaises(dp.StudioError):
            dbt_yaml.set_model_props(self.d, "int_tickets", {"description": "{{ run_query('drop') }}"})
        with self.assertRaises(dp.StudioError):
            dbt_yaml.set_model_props(self.d, "int_tickets", {"columns": [{"name": "x; drop", "description": "y"}]})
        with self.assertRaises(dp.StudioError):
            dbt_yaml.set_model_props(self.d, "int_tickets", {"materialized": "dictionary"})

    def test_add_source(self):
        r = dbt_yaml.add_source(self.d, "raw", "billing", ["invoices", "transactions"])
        self.assertEqual(r["file"], "models/staging/_sources.yml")
        self.assertEqual((r["added"], r["existing"]), (["transactions"], ["invoices"]))
        r = dbt_yaml.add_source(self.d, "landing", "landing", ["my_upload"], "Import & Export uploads")
        data = yaml.safe_load((self.d / r["file"]).read_text())
        land = next(s for s in data["sources"] if s["name"] == "landing")
        self.assertEqual(land, {"name": "landing", "description": "Import & Export uploads", "schema": "landing",
                                "tables": [{"name": "my_upload"}]})
        raw = next(s for s in data["sources"] if s["name"] == "raw")
        self.assertEqual(raw["schema"], "{{ var('raw_schema') }}")
        with self.assertRaises(dp.StudioError):
            dbt_yaml.add_source(self.d, "landing", "landing", ["bad-name"])


class WizardSql(unittest.TestCase):
    def test_select_join_filter(self):
        sql = dbt_sql.generate_sql({
            "layer": "intermediate", "name": "int_open_tickets", "description": "Open tickets with customer",
            "from": {"kind": "ref", "model": "int_tickets"},
            "joins": [{"kind": "ref", "model": "int_accounts", "type": "left", "left": "ban", "right": "ban"}],
            "columns": [{"rel": "base", "column": "ticket_id"}, {"rel": "base", "column": "minutes_open", "alias": "minutes_open_total"},
                        {"rel": "j1", "column": "customer_id"}, {"rel": "j1", "column": "limit"}],
            "filters": [{"rel": "base", "column": "status", "op": "=", "value": "O'Open"},
                        {"rel": "base", "column": "minutes_open", "op": ">=", "value": "1000"},
                        {"rel": "base", "column": "region", "op": "in", "value": "East, West"},
                        {"rel": "base", "column": "outage_id", "op": "is not null"}],
        })
        self.assertIn("{{ ref('int_tickets') }}", sql)
        self.assertIn("j1 as (\n    select * from {{ ref('int_accounts') }}\n)", sql)
        self.assertIn("    base.minutes_open as minutes_open_total,", sql)
        self.assertIn('    j1."limit"', sql)
        self.assertIn("left join j1 on base.ban = j1.ban", sql)
        self.assertIn("where base.status = 'O''Open'\n  and base.minutes_open >= 1000\n  and base.region in ('East', 'West')"
                      "\n  and base.outage_id is not null", sql)
        self.assertTrue(sql.startswith("-- Intermediate: Open tickets with customer\n"))

    def test_aggregate(self):
        sql = dbt_sql.generate_sql({
            "layer": "marts", "name": "mart_minutes_open_by_region", "materialized": "table",
            "from": {"kind": "source", "source": "raw", "table": "service_tickets"},
            "group_by": [{"rel": "base", "column": "region"}],
            "aggregates": [{"fn": "sum", "rel": "base", "column": "minutes_open", "alias": "total_minutes_open"},
                           {"fn": "count"}, {"fn": "count_distinct", "rel": "base", "column": "ban"}],
        })
        self.assertIn("{{ source('raw', 'service_tickets') }}", sql)
        self.assertIn("{{ config(materialized='table') }}", sql)
        self.assertIn("    base.region,\n    sum(base.minutes_open) as total_minutes_open,\n    count(*) as row_count,\n"
                      "    count(distinct base.ban) as count_distinct_ban\nfrom base\ngroup by base.region", sql)

    def test_star_when_no_columns(self):
        sql = dbt_sql.generate_sql({"layer": "staging", "name": "stg_x", "from": {"kind": "source", "source": "landing", "table": "x"}})
        self.assertIn("select\n    base.*\nfrom base\n", sql)

    def test_rejects_injection(self):
        base = {"layer": "intermediate", "name": "x_model", "from": {"kind": "ref", "model": "int_tickets"}}
        bad = [
            {**base, "name": "X-Model"},
            {**base, "layer": "platinum"},
            {**base, "from": {"kind": "ref", "model": "a') }}{{ run_query('x"}},
            {**base, "columns": [{"rel": "base", "column": "a; drop table x"}]},
            {**base, "filters": [{"rel": "base", "column": "a", "op": "=", "value": "{{ run_query('drop') }}"}]},
            {**base, "filters": [{"rel": "base", "column": "a", "op": "; drop", "value": "1"}]},
            {**base, "columns": [{"rel": "j9", "column": "a"}]},
            {**base, "columns": [{"rel": "base", "column": "a"}, {"rel": "base", "column": "b", "alias": "a"}]},
            {**base, "description": "{% do x %}"},
        ]
        for spec in bad:
            with self.assertRaises(dp.StudioError, msg=json.dumps(spec)):
                dbt_sql.generate_sql(spec)


class PathSafety(unittest.TestCase):
    def setUp(self):
        self.d = fresh_project()

    def test_resolve_project(self):
        self.assertEqual(dp.resolve_project("dbt_project"), self.d.resolve())
        for bad in ("", "/etc", "../dbt_project", "dbt_project/..", "dbt_project/../..", ".hidden", "nope",
                    "dbt_project\\..", "dbt_project/models"):
            with self.assertRaises(dp.StudioError, msg=bad):
                dp.resolve_project(bad)

    def test_safe_path(self):
        self.assertEqual(dp.safe_path(self.d, "models/x.sql"), (self.d / "models/x.sql").resolve())
        for bad in ("../x", "/etc/passwd", "models/../../x", "a\x00b"):
            with self.assertRaises(dp.StudioError, msg=bad):
                dp.safe_path(self.d, bad)
        os.symlink("/etc", self.d / "models" / "evil")
        with self.assertRaises(dp.StudioError):
            dp.safe_path(self.d, "models/evil/passwd")

    def test_model_names(self):
        for ok in ("int_tickets", "a", "mart_2024"):
            dp.check_model_name(ok)
        for bad in ("Stg", "1abc", "a-b", "a b", "../x", "x" * 64, ""):
            with self.assertRaises(dp.StudioError, msg=bad):
                dp.check_model_name(bad)

    def test_discover(self):
        nested = WORK / "team" / "sub_proj"
        nested.mkdir(parents=True, exist_ok=True)
        (nested / "dbt_project.yml").write_text("name: sub\nprofile: bsp_edw\n")
        ids = [p["id"] for p in dp.discover_projects()]
        self.assertEqual(ids[0], "dbt_project")
        self.assertIn("team/sub_proj", ids)


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.c = TestClient(wbapp.app)

    def setUp(self):
        self.d = fresh_project()

    def test_page_and_static(self):
        r = self.c.get("/dbt/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("/dbt/static/dbt.js", r.text)
        self.assertEqual(self.c.get("/dbt/static/dbt.js").status_code, 200)
        self.assertEqual(self.c.get("/dbt", follow_redirects=False).status_code, 307)

    def test_projects_and_model(self):
        r = self.c.get("/dbt/api/projects").json()
        self.assertEqual(r["target"], "postgres")
        self.assertIn("dbt_project", [p["id"] for p in r["projects"]])
        m = self.c.get("/dbt/api/model", params={"p": "dbt_project", "name": "int_tickets"}).json()
        self.assertEqual(m["path"], "models/intermediate/int_tickets.sql")
        self.assertIn("ref('stg_service_tickets')", m["sql"])
        self.assertEqual(m["layer"], "intermediate")
        self.assertEqual(self.c.get("/dbt/api/model", params={"p": "../etc", "name": "x"}).status_code, 404)
        self.assertEqual(self.c.get("/dbt/api/model", params={"p": "dbt_project", "name": "../x"}).status_code, 400)

    def test_csrf_and_save(self):
        m = self.c.get("/dbt/api/model", params={"p": "dbt_project", "name": "int_accounts"}).json()
        q = {"p": "dbt_project", "name": "int_accounts"}
        self.assertEqual(self.c.put("/dbt/api/model/sql", params=q, json={"sql": "select 1"}).status_code, 403)
        r = self.c.put("/dbt/api/model/sql", params=q, json={"sql": "select 2 as x", "mtime": m["mtime"]}, headers=H)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual((self.d / "models/intermediate/int_accounts.sql").read_text(), "select 2 as x\n")
        r = self.c.put("/dbt/api/model/sql", params=q, json={"sql": "select 3", "mtime": m["mtime"] - 100}, headers=H)
        self.assertEqual(r.status_code, 409)

    def test_wizard_and_props(self):
        spec = {"layer": "marts", "name": "mart_open_tickets", "description": "Open tickets by region",
                "from": {"kind": "ref", "model": "int_tickets"},
                "group_by": [{"rel": "base", "column": "region"}],
                "aggregates": [{"fn": "sum", "rel": "base", "column": "minutes_open", "alias": "minutes_open"}]}
        r = self.c.post("/dbt/api/wizard/sql", params={"p": "dbt_project"}, json={"spec": spec}, headers=H)
        self.assertIn("sum(base.minutes_open) as minutes_open", r.json()["sql"])
        r = self.c.post("/dbt/api/wizard", params={"p": "dbt_project"}, json={"spec": spec}, headers=H)
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["path"], "models/marts/mart_open_tickets.sql")
        self.assertTrue((self.d / "models/marts/mart_open_tickets.sql").is_file())
        self.assertEqual(self.c.post("/dbt/api/wizard", params={"p": "dbt_project"}, json={"spec": spec}, headers=H).status_code, 409)
        marts = yaml.safe_load((self.d / "models/marts/_marts.yml").read_text())
        self.assertIn({"name": "mart_open_tickets", "description": "Open tickets by region"}, marts["models"])
        body = {"description": "Open tickets", "materialized": "table", "tags": ["service"],
                "columns": [{"name": "region", "description": "Service region",
                             "tests": {"not_null": {}, "accepted_values": {"values": ["East", "West"], "severity": "warn"}}}]}
        r = self.c.put("/dbt/api/model/props", params={"p": "dbt_project", "name": "mart_open_tickets"}, json=body, headers=H)
        self.assertEqual(r.status_code, 200, r.text)
        cols = r.json()["props"]["columns"]
        self.assertEqual(cols[0]["tests"]["accepted_values"], {"values": ["East", "West"], "severity": "warn"})
        bad = dict(spec, name="Bad Name")
        self.assertEqual(self.c.post("/dbt/api/wizard", params={"p": "dbt_project"}, json={"spec": bad}, headers=H).status_code, 400)

    def test_jobs_preview_and_failure(self):
        r = self.c.post("/dbt/api/run", params={"p": "dbt_project"}, json={"action": "show", "select": "int_tickets"}, headers=H)
        self.assertEqual(r.status_code, 200, r.text)
        s = wait_job(self.c, "/dbt", r.json()["id"])
        self.assertEqual(s["status"], "succeeded", s)
        out = self.c.get(f"/dbt/api/output/{s['id']}").json()
        self.assertEqual(out["columns"], ["ticket_id", "minutes_open"])
        self.assertEqual(out["rows"][0]["ticket_id"], "C1")
        log = self.c.get(f"/dbt/api/jobs/{s['id']}/log").json()["lines"]
        text = "\n".join(log)
        self.assertIn("args=show --select int_tickets --limit 100 --output json --log-format json", text)
        self.assertIn(f"profiles={self.d.resolve()} target=postgres", text)
        self.assertIn("Previewed 1 row(s) of int_tickets", text)
        self.assertNotIn("SECRET-ROWS", text)  # preview data never lands in the job log/history
        r = self.c.post("/dbt/api/run", params={"p": "dbt_project"},
                        json={"action": "compile", "inline_sql": "select * from {{ ref('int_tickets') }};"}, headers=H)
        s = wait_job(self.c, "/dbt", r.json()["id"])
        self.assertEqual(self.c.get(f"/dbt/api/output/{s['id']}").json()["compiled"], "select 1")
        r = self.c.post("/dbt/api/run", params={"p": "dbt_project"},
                        json={"action": "build", "select": "int_tickets", "upstream": True, "downstream": True}, headers=H)
        s = wait_job(self.c, "/dbt", r.json()["id"])
        self.assertIn("args=build --select +int_tickets+", "\n".join(self.c.get(f"/dbt/api/jobs/{s['id']}/log").json()["lines"]))
        jobs = self.c.get("/dbt/api/jobs").json()["jobs"]
        self.assertTrue(all(j["queue"] == "dbt" for j in jobs))
        for bad in ({"action": "rm"}, {"action": "run", "select": "a b"}, {"action": "run", "inline_sql": "select 1"},
                    {"action": "parse", "select": "x"}, {"action": "show"}):
            self.assertEqual(self.c.post("/dbt/api/run", params={"p": "dbt_project"}, json=bad, headers=H).status_code, 400, bad)

    def test_graph(self):
        d = parsed_project()
        g = self.c.get("/dbt/api/graph", params={"p": d.name}).json()
        self.assertTrue(g["has_manifest"])
        self.assertFalse(g["stale"])
        by = {n["name"]: n for n in g["nodes"]}
        self.assertEqual(by["stg_service_tickets"]["layer"], "staging")
        self.assertEqual(by["int_tickets"]["layer"], "intermediate")
        self.assertEqual(by["mart_ticket_volume"]["layer"], "marts")
        self.assertEqual(by["service_tickets"]["layer"], "source")
        self.assertIn(by["stg_service_tickets"]["id"], by["int_tickets"]["parents"])
        self.assertGreaterEqual(by["int_tickets"]["tests"]["total"], 4)
        self.assertIn({"from": by["service_tickets"]["id"], "to": by["stg_service_tickets"]["id"]}, g["edges"])
        # run results fold into the per-node status
        (d / "target" / "run_results.json").write_text(json.dumps({
            "metadata": {"generated_at": "2026-01-01T00:00:00Z"}, "args": {"which": "build"},
            "results": [{"unique_id": by["int_tickets"]["id"], "status": "success", "execution_time": 0.1},
                        {"unique_id": by["int_tickets"]["test_list"][0]["id"], "status": "fail", "failures": 3}]}))
        g = self.c.get("/dbt/api/graph", params={"p": d.name}).json()
        n = next(x for x in g["nodes"] if x["name"] == "int_tickets")
        self.assertEqual(n["status"]["status"], "success")
        self.assertEqual(n["tests"]["fail"], 1)
        # a show/compile run_results does not wipe the history
        (d / "target" / "run_results.json").write_text(json.dumps({
            "metadata": {"generated_at": "2026-01-02T00:00:00Z"}, "args": {"which": "show"}, "results": []}))
        n = next(x for x in self.c.get("/dbt/api/graph", params={"p": d.name}).json()["nodes"] if x["name"] == "int_tickets")
        self.assertEqual(n["status"]["status"], "success")
        # editing a model marks the lineage stale
        f = d / "models/intermediate/int_tickets.sql"
        built = (d / "target" / "manifest.json").stat().st_mtime
        os.utime(f, (f.stat().st_atime, built + 60))
        self.assertTrue(self.c.get("/dbt/api/graph", params={"p": d.name}).json()["stale"])


if __name__ == "__main__":
    unittest.main()
