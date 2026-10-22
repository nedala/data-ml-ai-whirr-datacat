"""Catalog: fragment merge, persona resolution (tolerant of missing
components), dependency closure and ordering.

Runs against the real core catalog.yaml plus the fixture fragments in
tests/fixtures/catalog.d (superset/bi, connectors, trino) — see _runone.py."""

import os

import catalog
import settings

# with the fixture fragments: canvas, mlops, ai, rag, chatui, disputes, ccai are "not installed"
EXPECTED = {
    "data-pro": {"postgres", "appliance", "code", "trino", "superset", "connectors"},
    "ml-scientist": {"postgres", "appliance", "code", "trino", "superset", "connectors"},
    "ai-engineer": {"postgres", "appliance", "code", "connectors", "browser"},
}
MISSING = {
    "data-pro": {"canvas"},
    "ml-scientist": {"mlops"},
    "ai-engineer": {"ai", "rag", "chatui", "disputes", "ccai"},
}


def test_fragments_are_merged():
    cat = catalog.load()
    assert sorted(cat["fragments"]) == ["connectors", "superset", "trino"]
    assert {"postgres", "appliance", "code", "browser", "superset", "connectors", "trino"} <= set(cat["services"])
    assert {"workspace", "vscode", "browser", "bi", "connectors", "trino"} == set(cat["capabilities"])
    assert any(a["id"] == "superset" for a in cat["apps"]), "fragment tiles are appended"
    groups = {g["id"]: g for g in cat["app_groups"]}
    assert groups["integrate"]["title"].endswith("(fragment override)"), "app_groups merge by id"
    assert list(groups)[:2] == ["analyse", "data"], "core order kept"
    assert set(cat["personas"]) == {"data-pro", "ml-scientist", "ai-engineer"}, "personas from the core file only"


def test_merge_is_pure():
    core = {"services": {"a": {"title": "A", "ram_mb": 1}}, "capabilities": {}, "personas": {"p": {"capabilities": ["x"]}},
            "apps": [{"id": "t1", "title": "one"}], "app_groups": [{"id": "g", "title": "G"}]}
    frag = {"services": {"a": {"ram_mb": 5}, "b": {"title": "B"}}, "capabilities": {"x": {"services": ["b"]}},
            "personas": {"evil": {"capabilities": ["x"]}}, "apps": [{"id": "t1", "title": "uno"}, {"id": "t2"}],
            "shared_credentials": {"K": {"label": "k"}}}
    out = catalog.merge(core, [("f", frag)])
    assert out["services"]["a"] == {"title": "A", "ram_mb": 5}
    assert set(out["services"]) == {"a", "b"} and "x" in out["capabilities"]
    assert "evil" not in out["personas"]
    assert [a["title"] if "title" in a else a["id"] for a in out["apps"]] == ["uno", "t2"]
    assert out["shared_credentials"] == {"K": {"label": "k"}}
    assert core["services"]["a"]["ram_mb"] == 1, "inputs not mutated"


def test_personas_match_the_contract():
    cat = catalog.load()
    assert cat["personas"]["data-pro"]["capabilities"] == ["workspace", "vscode", "trino", "bi", "canvas", "connectors"]
    assert cat["personas"]["ml-scientist"]["capabilities"] == ["workspace", "vscode", "trino", "mlops", "bi", "connectors"]
    assert cat["personas"]["ai-engineer"]["capabilities"] == ["workspace", "vscode", "connectors", "ai", "rag", "chatui",
                                                              "disputes", "ccai", "browser"]


def test_every_persona_resolves_skipping_missing_components():
    for persona, want in EXPECTED.items():
        res = catalog.resolve([persona])
        assert set(res.services) == want, (persona, set(res.services) ^ want)
        missing = {h["capability"] for h in res.hints if h.get("missing") == "capability"}
        assert missing == MISSING[persona], (persona, missing)
        assert all("not installed" in h["detail"] for h in res.hints if h.get("missing"))
        assert res.ram_mb == sum(catalog.load()["services"][s]["ram_mb"] for s in want)


def test_implied_capabilities_explain_themselves():
    res = catalog.resolve(capabilities=["bi"])
    assert res.capabilities["bi"] == {"state": "selected", "required_by": []}
    assert res.capabilities["workspace"] == {"state": "implied", "required_by": ["bi"]}
    assert set(res.services) == {"superset", "appliance", "postgres"}
    assert res.services["superset"]["capabilities"] == ["bi"]
    assert "superset" in res.services["postgres"]["needed_by"]
    assert set(res.profiles) == {"bi", "workspace", "base"}


def test_dependency_first_order():
    order = list(catalog.resolve(["data-pro"]).services)
    for svc in order:
        for dep in catalog.load()["services"][svc].get("needs", []):
            assert order.index(dep) < order.index(svc), (dep, svc, order)


def test_workspace_brings_the_warehouse():
    res = catalog.resolve(capabilities=["workspace"])
    assert set(res.services) == {"appliance", "postgres"}
    assert set(res.profiles) == {"workspace", "base"}


def test_explicit_capabilities_override_personas():
    res = catalog.resolve(["data-pro"], capabilities=["workspace", "bi"])
    assert "code" not in res.services and "browser" not in res.services


def test_unknown_names():
    try:
        catalog.resolve(personas=["nope"])
        raise AssertionError("unknown persona accepted")
    except ValueError:
        pass
    res = catalog.resolve(capabilities=["nope", "vscode"])   # a stale selection never crashes
    assert set(res.services) == {"code"} and res.hints[0]["capability"] == "nope"
    try:
        catalog.with_needs(["nope"])
        raise AssertionError("unknown service accepted")
    except ValueError:
        pass


def test_with_needs_and_composable():
    assert catalog.with_needs(["superset"]) == ["postgres", "superset"]
    assert catalog.composable_services() == {"postgres", "appliance", "code", "superset", "connectors", "trino", "browser"}


def test_default_persona_follows_appliance_env():
    for persona in ("ml-scientist", "ai-engineer", "data-pro"):
        os.environ["APPLIANCE_PERSONA"] = persona
        settings.reload()
        assert catalog.default_persona() == persona
    os.environ["APPLIANCE_PERSONA"] = "unknown"
    settings.reload()
    assert catalog.default_persona() == "data-pro", "first persona as fallback"
    os.environ.pop("APPLIANCE_PERSONA")
    settings.reload()
    assert settings.appliance_name() == "Data Professional"


def test_shared_credentials_are_slots_of_the_shared_pseudo_service():
    slots = {s["env"]: s for s in catalog.cred_slots(catalog.SHARED)}
    assert set(slots) == {"GCP_SA_JSON", "M365_CLIENT_SECRET", "ATLAN_API_TOKEN", "DATAHUB_TOKEN", "SAP_BRIM_USER", "SAP_BRIM_PASSWORD",
                          "ANTHROPIC_API_KEY"}
    assert slots["GCP_SA_JSON"]["multiline"] and all(s["external"] and s["policy"] == "any" for s in slots.values())
    assert "BigQuery" in slots["GCP_SA_JSON"]["label"]


def test_service_needs_without_capability_are_auto_included(project):
    path = project / "cat.yaml"
    path.write_text("""
services:
  db: {title: DB, ram_mb: 100}
  api: {title: API, ram_mb: 50, needs: [db, ghost], profile: api}
capabilities:
  web: {title: Web, services: [api, phantom]}
personas: {}
apps: []
""")
    old = os.environ.get("STUDIO_CATALOG")
    os.environ["STUDIO_CATALOG"] = str(path)
    settings.reload()
    try:
        res = catalog.resolve(capabilities=["web"])
        assert list(res.services) == ["db", "api"]
        assert res.services["db"] == {"capabilities": [], "needed_by": ["api"]}
        assert res.ram_mb == 150
        details = " ".join(h["detail"] for h in res.hints)
        assert "ghost" in details and "phantom" in details, "unknown services are skipped with a hint"
    finally:
        os.environ["STUDIO_CATALOG"] = old or ""
        settings.reload()


def test_flow_mappings_have_no_stray_keys():
    # an unquoted comma inside a `{… blurb: a, b}` flow mapping silently cuts
    # the text and invents a key — every tile / panel / capability must parse
    # to exactly its known fields
    cat = catalog.load()
    for app in cat["apps"]:
        assert set(app) <= {"id", "group", "title", "service", "path", "cred", "icon", "blurb", "alt", "requires_env",
                            "probe", "cred_file"}, app
    for group in cat.get("app_groups", []):
        assert set(group) <= {"id", "title", "icon", "blurb"}, group
    for cap, spec in cat["capabilities"].items():
        assert set(spec) <= {"title", "tone", "icon", "services", "requires", "blurb"}, cap
    known = {g["id"] for g in cat.get("app_groups", [])}
    assert all(app.get("group") in known for app in cat["apps"]), "every tile belongs to a panel"


def test_endpoints_interpolate_ports():
    os.environ["NOVNC_PORT"] = "36083"
    try:
        eps = {e["label"]: e["value"] for e in catalog.endpoints("browser")}
        assert any("36083/vnc.html" in v for v in eps.values()), eps
        assert "http://browser:9223" in eps.values()
    finally:
        os.environ.pop("NOVNC_PORT")


def test_repository_catalog_files_parse(project):
    """Every real catalog file (core + every component fragment in the repo)
    parses with yaml.safe_load and merges; a broken fragment is skipped with
    an error instead of failing the load."""
    import yaml
    from pathlib import Path
    repo = Path(os.environ.get("STUDIO_TEST_REPO", "/repo")) / "build" / "studio"
    if not (repo / "catalog.yaml").is_file():
        repo = Path(catalog.__file__).resolve().parent
    files = [repo / "catalog.yaml", *sorted((repo / "catalog.d").glob("*.yaml"))]
    docs = []
    for f in files:
        try:
            docs.append((f.stem, yaml.safe_load(f.read_text(encoding="utf-8")) or {}))
        except yaml.YAMLError as exc:
            raise AssertionError(f"{f} does not parse: {exc}") from exc
    merged = catalog.merge(docs[0][1], docs[1:])
    assert {"data-pro", "ml-scientist", "ai-engineer"} == set(merged["personas"])
    # a broken fragment is skipped, reported, and never breaks the load
    bad_dir = project / "badcat"
    (bad_dir / "catalog.d").mkdir(parents=True)
    (bad_dir / "catalog.yaml").write_text((repo / "catalog.yaml").read_text())
    (bad_dir / "catalog.d" / "broken.yaml").write_text("apps:\n  - {id: x, path: /a?b, title: : }\n")
    (bad_dir / "catalog.d" / "good.yaml").write_text("capabilities:\n  extra: {title: Extra, services: [code]}\n")
    old = os.environ.get("STUDIO_CATALOG")
    os.environ["STUDIO_CATALOG"] = str(bad_dir / "catalog.yaml")
    settings.reload()
    try:
        cat = catalog.load()
        assert "extra" in cat["capabilities"] and cat["fragments"] == ["good"]
        assert cat["fragment_errors"] and cat["fragment_errors"][0].startswith("broken.yaml")
    finally:
        os.environ["STUDIO_CATALOG"] = old or ""
        settings.reload()
