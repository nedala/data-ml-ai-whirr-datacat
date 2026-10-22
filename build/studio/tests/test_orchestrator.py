"""Composer engine: secrets, dirs, planning diff, compose argv/env and a full
apply job driven through a fake `docker compose`."""

import json
import os
import stat
import string
import time

import catalog
import composefile
import dockerops
import jobs
import orchestrator
import settings
from vault import store

STATE: dict[str, str] = {}


def fake_status(max_age: float = 1.5):
    out = {}
    for name in [*catalog.CONTROL_PLANE, *catalog.load()["services"]]:
        st = STATE.get(name, "missing")
        out[name] = {"state": st, "health": None, "started_at": "2026-01-01T00:00:00Z" if st == "running" else None,
                     "image": "", "exit_code": 0}
    return out


def test_setup(project):
    settings.reload()
    store.initialize("orchestrator test passphrase")
    dockerops.status = fake_status
    dockerops.host_memory = lambda: 16 * 1024 ** 3
    # treat every image as already present: this suite exercises the
    # orchestration logic, not registry access (CI has no appliance image).
    dockerops.missing_images = lambda refs: []
    os.environ["HOST_PROJECT_ROOT"] = "/HOST/proj"
    os.environ["APPLIANCE_MASTER_PASSWORD"] = "must-not-leak"
    settings.reload()


def test_generate_secret_policies():
    assert orchestrator.generate_secret(40, "alnum").isalnum()
    assert set(orchestrator.generate_secret(64)) <= set(string.ascii_letters + string.digits + "-_")
    assert len(orchestrator.generate_secret(5)) == 12, "minimum length enforced"
    assert orchestrator.policy_error("not alnum!", "alnum")
    assert orchestrator.policy_error("has$dollar1", "default")
    assert orchestrator.policy_error("short", "default")
    for _ in range(50):   # SQL Server / Oracle friendly
        v = orchestrator.generate_secret(16, "strong")
        assert not orchestrator.policy_error(v, "strong") and v[0].isalpha() and v.isalnum()
    assert orchestrator.policy_error("alllowercase123", "strong")


def test_ensure_dirs(project):
    created = orchestrator.ensure_dirs()
    data = settings.cfg.data
    for rel in ("analyst/work", "analyst/hf_cache", "analyst/pyuser", "analyst/ssh", "analyst/logs",
                "analyst/code-server", "analyst/browser", "analyst/desktop", "lake", "lake/landing",
                "lake/reference", "lake/exports", "lake/samples", "warehouse", "egress", "tls", "superset", "mlops",
                "connectors", "disputes", "ccai", "rag"):
        assert (data / rel).is_dir(), rel
    for private in ("analyst/ssh", "analyst/browser"):
        assert stat.S_IMODE(os.stat(data / private).st_mode) == 0o700, private
    assert stat.S_IMODE(os.stat(data / "analyst/work").st_mode) == 0o755
    assert "analyst/work" in created and "superset" in created
    assert orchestrator.ensure_dirs() == [], "idempotent"


def test_ensure_credentials_generates_and_recovers(project):
    (project / ".env").write_text('USER_HANDLE=analyst\nPG_PASS="from-v1-env"\nSUPERSET_ADMIN_PASS=changeme\n')
    made = orchestrator.ensure_credentials(["postgres", "superset"])
    assert store.get("services.postgres.PG_PASS") == "from-v1-env", "stateful volume value reused"
    admin = store.get("services.superset.SUPERSET_ADMIN_PASS")
    assert admin not in ("", None, "changeme"), "placeholders in .env are never reused"
    assert orchestrator.policy_error(admin, "default") == ""
    assert len(store.get("services.superset.SUPERSET_SECRET_KEY")) == 48, "catalog length honoured"
    assert store.get("meta.rotated.postgres.PG_PASS") and store.get("meta.rotated.superset.SUPERSET_SECRET_KEY")
    assert any("prehydrated from .env" in m for m in made)
    assert len(made) == 3
    assert store.get("services.code") is None, "only the requested services get credentials"
    assert orchestrator.ensure_credentials(["postgres", "superset"]) == [], "existing values kept"


def test_external_credentials_are_never_generated(project):
    # a key the user obtains (enterprise keys) stays empty until set
    import catalog as _cat
    real = _cat.load()["services"]
    real["_ext"] = {"title": "Ext", "creds": [{"env": "EXT_API_KEY", "label": "Ext key", "external": True},
                                            {"env": "EXT_PASS", "label": "Ext password"}]}
    try:
        made = orchestrator.ensure_credentials(["_ext"])
        assert made == ["Ext password"]
        assert not store.get("services._ext.EXT_API_KEY") and store.get("services._ext.EXT_PASS")
    finally:
        real.pop("_ext", None)

def test_secret_env():
    env = orchestrator.secret_env()
    assert env["PG_PASS"] == "from-v1-env"
    assert env["SUPERSET_SECRET_KEY"] == store.get("services.superset.SUPERSET_SECRET_KEY")
    assert "CODE_PASSWORD" not in env, "not generated yet"
    assert "meta" not in env and all(k.isupper() for k in env)
    # shared enterprise keys: always present (empty until set) so nothing stale leaks
    assert env["GCP_SA_JSON"] == "" and env["M365_CLIENT_SECRET"] == "" and env["ANTHROPIC_API_KEY"] == ""


def test_shared_credentials_reach_every_compose_start():
    key = '{\n  "type": "service_account",\n  "private_key": "-----BEGIN TEST KEY-----\\nMIIE$x\\n-----END TEST KEY-----\\n",\n  "client_email": "svc@proj.iam.gserviceaccount.com"\n}'
    assert orchestrator.policy_error(key, "any") == "", "multi-line JSON with $ and quotes is accepted verbatim"
    store.set("services.shared.GCP_SA_JSON", key)
    store.set("services.shared.ATLAN_API_TOKEN", "atlan-token")
    env = orchestrator.secret_env()
    assert env["GCP_SA_JSON"] == key and env["ATLAN_API_TOKEN"] == "atlan-token"
    assert "shared" not in env
    sub = dockerops.subprocess_env(env)
    assert sub["GCP_SA_JSON"] == key, "passed verbatim through the process environment"
    import credentials
    slot = next(s for s in credentials.slots() if s["env"] == "GCP_SA_JSON")
    assert slot["shared"] and slot["set"] and slot["multiline"] and slot["external"]
    assert set(composefile.consumers("GCP_SA_JSON")) >= {"appliance", "connectors", "trino"}
    store.delete("services.shared.GCP_SA_JSON")
    store.delete("services.shared.ATLAN_API_TOKEN")


def test_plan_diff():
    STATE.clear()
    STATE.update({"postgres": "running", "superset": "running", "connectors": "exited", "studio": "running"})
    p = orchestrator.plan([], ["workspace", "vscode"])
    assert set(p["start"]) == {"appliance", "code"}
    assert p["keep"] == ["postgres"]
    assert p["stop"] == ["superset"], "running but no longer selected; connectors already down; control plane never composable"
    assert set(p["profiles"]) == {"base", "workspace", "vscode"}
    assert p["ram"]["host_mb"] == 16384
    assert p["credentials_to_generate"] == 3   # JUPYTER_TOKEN, WB_STUDIO_TOKEN, CODE_PASSWORD (PG_PASS already in the vault)
    code = next(s for s in p["services"] if s["name"] == "code")
    assert code["capabilities"] == ["vscode"]
    pg = next(s for s in p["services"] if s["name"] == "postgres")
    assert pg["capabilities"] == ["workspace"] and pg["needed_by"] == ["appliance"]

    # persona preset: every installed capability; canvas (no fragment) is a hint
    full = orchestrator.plan(["data-pro"], None)
    assert set(full["capabilities"]) == {"workspace", "vscode", "trino", "bi", "connectors"}
    assert {s["name"] for s in full["services"]} == {"postgres", "appliance", "code", "trino", "superset", "connectors"}
    assert full["stop"] == [] and set(full["keep"]) == {"postgres", "superset"}
    assert any(h.get("capability") == "canvas" for h in full["hints"])

    # hybrid: an explicit capability set wins over the persona (a la carte)
    alc = orchestrator.plan(["data-pro"], ["connectors"])
    assert list(alc["capabilities"]) == ["connectors"]
    assert alc["start"] == ["connectors"] and set(alc["stop"]) == {"postgres", "superset"}
    conn = next(s for s in alc["services"] if s["name"] == "connectors")
    assert not conn["auto"] and conn["capabilities"] == ["connectors"]


def test_deselect_never_stops_shared_dependency():
    STATE.clear()
    STATE.update({s: "running" for s in catalog.resolve(["data-pro"], ["workspace", "vscode", "trino", "bi", "connectors", "browser"]).services})
    p = orchestrator.plan([], ["workspace", "bi"])
    assert set(p["stop"]) == {"code", "trino", "connectors", "browser"}, p["stop"]
    # bi alone still implies workspace, and superset needs postgres
    p = orchestrator.plan([], ["bi"])
    assert p["capabilities"]["workspace"] == {"state": "implied", "required_by": ["bi"]}
    assert set(p["stop"]) == {"code", "trino", "connectors", "browser"}
    assert {"postgres", "appliance", "superset"} == set(p["keep"])
    pg = next(s for s in p["services"] if s["name"] == "postgres")
    assert "superset" in pg["needed_by"] and pg["action"] == "keep"


def test_compose_argv_uses_host_path(project):
    (project / ".env").write_text("USER_HANDLE=analyst\n")
    argv = dockerops.compose_argv(["up", "-d", "superset"], ["base", "bi"], cli=["docker", "compose"])
    assert argv[:2] == ["docker", "compose"]
    assert argv[argv.index("-f") + 1] == str(project / "docker-compose.yml"), "compose file read from the container path"
    assert argv[argv.index("--project-directory") + 1] == "/HOST/proj", "bind mounts resolve on the host"
    assert argv[argv.index("-p") + 1] == "bsp_data"
    assert argv[argv.index("--env-file") + 1] == str(project / ".env")
    assert argv[-3:] == ["up", "-d", "superset"] and argv.count("--profile") == 2
    # an appliance overlay: same project name, env files (later wins) and DATA_ROOT as `make up`
    (project / "appliances").mkdir(exist_ok=True)
    (project / "appliances" / "ml.env").write_text("COMPOSE_PROJECT_NAME=bsp_ml\nDATA_ROOT=./data/ml\n")
    os.environ.update({"APPLIANCE": "ml", "COMPOSE_PROJECT_NAME": "bsp_ml", "DATA_ROOT": "./data/ml",
                       "APPLIANCE_PERSONA": "ml-scientist"})
    settings.reload()
    try:
        argv = dockerops.compose_argv(["up", "-d", "code"], ["vscode"], cli=["docker", "compose"])
        files = [argv[i + 1] for i, a in enumerate(argv) if a == "--env-file"]
        assert files == [str(project / ".env"), str(project / "appliances" / "ml.env")]
        assert argv[argv.index("-p") + 1] == "bsp_ml"
        env = dockerops.subprocess_env({"X_SECRET": "v"})
        assert env["DATA_ROOT"] == "./data/ml" and env["COMPOSE_PROJECT_NAME"] == "bsp_ml"
        assert env["APPLIANCE_PERSONA"] == "ml-scientist" and env["X_SECRET"] == "v"
        assert "DATA_DIR" not in env and "VAULT_DIR" not in env, "container-only paths never reach compose"
        assert settings.dotenv()["COMPOSE_PROJECT_NAME"] == "bsp_ml"
    finally:
        for k in ("APPLIANCE", "COMPOSE_PROJECT_NAME", "DATA_ROOT", "APPLIANCE_PERSONA"):
            os.environ.pop(k, None)
        settings.reload()


def test_derive_host_root():
    from pathlib import Path
    proj = Path("/host-project")
    mounts = [{"Destination": "/var/run/docker.sock", "Source": "/var/run/docker.sock"},
              {"Destination": "/host-project", "Source": "/srv/bsp/appliance"}]
    assert dockerops.derive_host_root("/explicit/", mounts, proj) == "/explicit"
    assert dockerops.derive_host_root("", mounts, proj) == "/srv/bsp/appliance"
    assert dockerops.derive_host_root("", [], proj) == "/host-project"
    assert dockerops.derive_host_root("", None, proj) == "/host-project"


def test_compose_env_consumers():
    pg = composefile.consumers("PG_PASS")
    assert {"postgres", "appliance", "superset"} <= set(pg)
    assert composefile.consumers("CODE_PASSWORD") == ["code"]
    assert composefile.consumers("JUPYTER_TOKEN") == ["appliance"]
    assert composefile.consumers("VNC_PASSWORD") == ["browser"]
    assert "PG_HOST" not in composefile.env_refs().get("appliance", set()), "literal values are not references"
    assert composefile.images()["browser"] == "bsp/browser:latest" and composefile.images()["appliance"] == "bsp/appliance:latest"
    assert composefile.consumers("NO_SUCH_VARIABLE") == []


def test_compose_output_parsing():
    job = jobs.Job("x", "apply", "t", "u", steps=[jobs.Step("superset", "Superset", "running"),
                                                  jobs.Step("appliance", "Workspace", "running"),
                                                  jobs.Step("connectors", "Connectors", "running")])
    cases = {
        " Container bsp_ml-superset-1  Creating": ("superset", "creating"),
        " ✔ Container bsp_data-appliance-1  Started": ("appliance", "started"),
        " Container bsp_data-appliance-1  Healthy": ("appliance", "started"),
        " Container bsp_data-connectors-1  Recreated": ("connectors", "starting"),
        " superset Pulling ": ("superset", "pulling"),
        " appliance Pulled": ("appliance", "creating"),
        " Container bsp_data-postgres-1  Started": ("", "started"),   # not a step of this job
    }
    for line, (svc, phase) in cases.items():
        m = orchestrator._LINE.search(line)
        assert m, line
        assert orchestrator._step_for(job, m.group("ctr") or "", m.group("svc") or "") == svc, line
        assert orchestrator.PHASE_WORDS[m.group("word").lower()] == phase, line
    assert not orchestrator._LINE.search(" Network bsp-data-net  Created") or \
        orchestrator._step_for(job, "", "Network") == ""


def test_apply_job_through_fake_compose(project):
    script = project / "fake-compose"
    calls = project / "calls.jsonl"
    script.write_text(f"""#!/usr/bin/env python3
import json, os, sys
args = sys.argv[1:]
with open({str(calls)!r}, "a") as fh:
    fh.write(json.dumps({{"args": args, "PG_PASS": os.environ.get("PG_PASS"), "JUPYTER_TOKEN": os.environ.get("JUPYTER_TOKEN"),
                         "leak": os.environ.get("APPLIANCE_MASTER_PASSWORD")}}) + "\\n")
op = next(a for a in args if a in ("up", "stop"))
names = [a for a in args[args.index(op) + 1:] if not a.startswith("-")]
for n in names:
    print(f" Container bsp_data-{{n}}-1  Creating", flush=True)
    print(f" Container bsp_data-{{n}}-1  {{'Started' if op == 'up' else 'Stopped'}}", flush=True)
with open({str(project / 'state.json')!r}, "w") as fh:
    json.dump({{"op": op, "names": names}}, fh)
""")
    script.chmod(0o755)
    dockerops.compose_cli = lambda: [str(script)]
    orchestrator.POLL = 0.05

    def status_from_fake(max_age=1.5):
        try:
            st = json.loads((project / "state.json").read_text())
            for n in st["names"]:
                STATE[n] = "running" if st["op"] == "up" else "exited"
        except (OSError, ValueError):
            pass
        return fake_status()
    dockerops.status = status_from_fake

    STATE.clear()
    STATE.update({"postgres": "running", "superset": "running", "connectors": "running"})
    job = orchestrator.apply(["data-pro"], ["workspace", "vscode"], "tester")
    for _ in range(200):
        if job.state in jobs.TERMINAL:
            break
        time.sleep(0.05)
    assert job.state == "done", (job.state, job.error, list(job.log))
    # wanted-and-already-running services (keep) are re-upped too, so config
    # drift gets recreated; compose leaves unchanged containers alone.
    assert {s.service: s.phase for s in job.steps} == {
        "appliance": "healthy", "code": "healthy", "postgres": "healthy",
        "superset": "stopped", "connectors": "stopped"}
    recorded = [json.loads(l) for l in calls.read_text().splitlines()]
    stop_call, up_call = recorded
    sargs = stop_call["args"]
    assert "stop" in sargs and set(sargs[sargs.index("stop") + 1:]) == {"superset", "connectors"}
    assert "postgres" not in sargs[sargs.index("stop"):], "a shared dependency is never stopped"
    assert stop_call["PG_PASS"] is None, "stop calls carry no secrets"
    args = up_call["args"]
    assert args[args.index("--project-directory") + 1] == "/HOST/proj"
    assert args[args.index("up"):][:3] == ["up", "-d", "--no-build"]
    assert set(args[args.index("--no-build") + 1:]) == {"appliance", "code", "postgres"}
    assert {args[i + 1] for i, a in enumerate(args) if a == "--profile"} == {"base", "workspace", "vscode"}
    assert up_call["PG_PASS"] == "from-v1-env" and up_call["JUPYTER_TOKEN"], "vault secrets in the process env"
    assert up_call["leak"] is None, "master password stripped from the compose env"
    assert store.get("services.code.CODE_PASSWORD"), "credentials generated right before start"
    sel = json.loads((settings.cfg.studio_data / "selection.json").read_text())
    assert sel["personas"] == ["data-pro"] and sel["capabilities"] == ["workspace", "vscode"] and sel["by"] == "tester"
    assert not (project / ".env").read_text().count("PASS"), "secrets never written to .env"


def test_rotation_impact_warnings():
    import credentials
    dockerops.status = fake_status   # drop the apply test's compose-driven status
    STATE.clear()
    STATE.update({s: "running" for s in ("postgres", "appliance", "superset")})
    pg = credentials.impact("postgres", "PG_PASS")
    assert {"postgres", "appliance", "superset"} <= set(pg["recreate"])
    assert "code" not in pg["recreate"], "only running consumers are recreated"
    assert pg["strong"] and "Shared root credential" in pg["warning"] and "superset" in pg["warning"]
    assert not pg["generate_blocked"]

    code = credentials.impact("code", "CODE_PASSWORD")
    assert code["recreate"] == [] and not code["running"] and not code["strong"] and code["warning"] == ""
    STATE["code"] = "running"
    assert credentials.impact("code", "CODE_PASSWORD")["recreate"] == ["code"]

    # init-only slots block regeneration once a value exists
    store.set("services.superset.SUPERSET_ADMIN_PASS", "Old-pass-1")
    credentials.INIT_ONLY.add("SUPERSET_ADMIN_PASS")
    try:
        sup = credentials.impact("superset", "SUPERSET_ADMIN_PASS")
        assert sup["generate_blocked"] and sup["strong"] and "first start" in sup["warning"]
        assert next(s for s in credentials.slots() if s["env"] == "SUPERSET_ADMIN_PASS")["init_only"]
    finally:
        credentials.INIT_ONLY.discard("SUPERSET_ADMIN_PASS")
    assert not any(s["init_only"] for s in credentials.slots() if s["kind"] == "service")

    assert credentials.find_slot("superset", "SUPERSET_SECRET_KEY")["hidden"]
    for svc, env in (("trino", "TRINO_SHARED_SECRET"), ("nosuch", "SECRET_KEY"), ("postgres", "META_PASS")):
        try:
            credentials.impact(svc, env)
            raise AssertionError(f"{svc}.{env} accepted")
        except credentials.CredentialError:
            pass


def test_failed_compose_marks_job_failed(project):
    bad = project / "bad-compose"
    bad.write_text("#!/bin/sh\necho 'Error response from daemon: boom'\nexit 1\n")
    bad.chmod(0o755)
    dockerops.compose_cli = lambda: [str(bad)]
    STATE.pop("superset", None)
    job = orchestrator.start(["superset"], "tester")
    for _ in range(200):
        if job.state in jobs.TERMINAL:
            break
        time.sleep(0.05)
    assert job.state == "failed" and "boom" in job.error


def test_unknown_service_rejected():
    for bad in ("datahub", "rstudio"):
        try:
            orchestrator.start([bad], "tester")
            raise AssertionError(f"started unknown service {bad}")
        except ValueError as exc:
            assert "unknown service" in str(exc)
    # a capability whose component is not installed is skipped with a hint
    p = orchestrator.plan([], ["lakehouse", "vscode"])
    assert [s["name"] for s in p["services"] if s["action"] != "stop"] == ["code"]
    assert any(h.get("capability") == "lakehouse" for h in p["hints"])
    try:
        orchestrator.plan(["python-analyst"], None)
        raise AssertionError("planned unknown persona")
    except ValueError as exc:
        assert "unknown persona" in str(exc)


def test_locked_vault_blocks_start():
    store.lock()
    try:
        orchestrator.start(["code"], "tester")
        raise AssertionError("started with a locked vault")
    except orchestrator.VaultLocked:
        pass
