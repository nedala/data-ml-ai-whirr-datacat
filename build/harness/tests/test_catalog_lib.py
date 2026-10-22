"""bsp_common.catalog: name heuristics, scope recipes, and the metadata-only annotator (fake GraphQL + LLM)."""
import json

import pytest

from bsp_common import catalog as cat


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    for k in ("CATALOG_BACKEND", "DATAHUB_TOKEN", "ATLAN_BASE_URL", "ATLAN_API_TOKEN"):
        monkeypatch.delenv(k, raising=False)
    cat._ensured.clear()


def test_backend_selection(monkeypatch):
    assert cat.backend() == "none" and cat.search("churn") == [] and cat.tables("warehouse") == []
    monkeypatch.setenv("ATLAN_BASE_URL", "https://x"); monkeypatch.setenv("ATLAN_API_TOKEN", "t")
    assert cat.backend() == "atlan"
    monkeypatch.setenv("DATAHUB_TOKEN", "tok")
    assert cat.backend() == "datahub"
    monkeypatch.setenv("CATALOG_BACKEND", "none")
    assert cat.backend() == "none"
    with pytest.raises(cat.CatalogError):
        cat.refresh({"catalog": "warehouse"})


def test_name_heuristics():
    for c in ("first_name", "CUST_NAME", "email", "email_address", "phone_num", "service_address", "ssn", "dob",
              "account_holder_name", "zip_code"):
        assert cat.is_pii_name(c), c
    for c in ("ban", "invoice_id", "status", "technology", "amount_due", "clli", "names_count_total"):
        assert not cat.is_pii_name(c), c
    assert cat.is_ban_name("BAN") and cat.is_ban_name("billing_account_number") and not cat.is_ban_name("banner")
    assert set(cat.suggested_terms("fct_invoice", ["ban", "invoice_id", "technology"])) == {"BAN", "Invoice"}
    assert "Fiber" in cat.suggested_terms("ftth_orders", ["ont_serial"]) and "Copper" not in cat.suggested_terms("x", ["potsdam"])
    assert cat.dataset_urn("bq_tsa_dv.etl_control.jobs") == "urn:li:dataset:(urn:li:dataPlatform:trino,bq_tsa_dv.etl_control.jobs,PROD)"
    assert cat.name_from_urn(cat.dataset_urn("a.b.c")) == "a.b.c" and cat.platform_from_urn(cat.dataset_urn("a.b.c")) == "trino"


def test_scope_recipe_limits_schema_and_tables_and_never_profiles():
    r = cat.scope_recipe({"catalog": "bq_tsa_dv", "schema": "etl_control", "tables": ["jobs", "runs"]})["source"]
    assert r["type"] == "trino" and r["config"]["database"] == "bq_tsa_dv"
    assert r["config"]["profiling"] == {"enabled": False}
    assert r["config"]["schema_pattern"]["allow"] == ["^etl_control$"]
    assert r["config"]["table_pattern"]["allow"] == ["^etl_control\\.jobs$", "^etl_control\\.runs$"]
    with pytest.raises(cat.CatalogError):
        cat.scope_recipe({"catalog": "x; drop"})


DS = {
    "urn": cat.dataset_urn("bq_x.sales.customers"), "platform": {"name": "trino"},
    "properties": {"name": "customers", "description": ""}, "editableProperties": None,
    "tags": None, "glossaryTerms": None, "domain": None,
    "schemaMetadata": {"fields": [
        {"fieldPath": "ban", "nativeDataType": "varchar", "description": None},
        {"fieldPath": "email", "nativeDataType": "varchar", "description": None},
        {"fieldPath": "status", "nativeDataType": "varchar", "description": "Account status set by billing"}]},
    "editableSchemaMetadata": None,
}


class FakeGQL:
    def __init__(self, ds):
        self.ds, self.calls = ds, []

    def __call__(self, query, variables=None, timeout=None):
        self.calls.append((query, variables or {}))
        if "dataset(urn:" in query:
            return {"dataset": self.ds}
        if "tag(urn:" in query:
            return {"tag": {"urn": variables["urn"], "properties": {"name": "x"}}}
        if "GLOSSARY_TERM" in json.dumps(variables):
            return {"searchAcrossEntities": {"searchResults": [
                {"entity": {"urn": "urn:li:glossaryTerm:bsp.ban", "properties": {"name": "BAN"}}}]}}
        if "DOMAIN" in json.dumps(variables):
            return {"searchAcrossEntities": {"searchResults": [
                {"entity": {"urn": "urn:li:domain:sales", "properties": {"name": "Sales"}}}]}}
        return {}

    def muts(self, name):
        return [v for q, v in self.calls if name in q]


def test_annotator_sends_metadata_only_marks_ai_and_keeps_human_text(monkeypatch):
    monkeypatch.setenv("DATAHUB_TOKEN", "tok")
    fake = FakeGQL(json.loads(json.dumps(DS)))
    seen = []

    def llm(model, meta):
        seen.append(meta)
        return {"description": "Customer accounts by BAN.", "domain": "Sales",
                "columns": {"ban": "Billing account number", "email": "Contact email", "status": "SHOULD NOT BE USED"}}
    monkeypatch.setattr(cat, "_gql", fake)
    monkeypatch.setattr(cat, "_llm_json", llm)
    c = cat.annotate([DS["urn"]])
    assert c["tables_annotated"] == 1 and c["columns_described"] == 2 and c["pii_columns"] == 1 and c["terms_linked"] == 1
    # the model saw names / types / existing descriptions only
    assert set(seen[0]) == {"table", "platform", "existing_description", "tags", "terms", "columns"}
    assert all(set(col) <= {"name", "type", "description"} for col in seen[0]["columns"])
    descs = fake.muts("updateDescription")
    texts = {v["input"].get("subResource"): v["input"]["description"] for v in descs}
    assert texts[None] == "Customer accounts by BAN. (AI-suggested)"
    assert texts["ban"].endswith("(AI-suggested)") and "status" not in texts, "human column text is never overwritten"
    tags = [v["input"] for v in fake.muts("addTags")]
    assert {"tagUrns": ["urn:li:tag:pii-suspected"], "resourceUrn": DS["urn"], "subResourceType": "DATASET_FIELD",
            "subResource": "email"} in tags
    assert {"tagUrns": ["urn:li:tag:ai-annotated"], "resourceUrn": DS["urn"]} in tags
    assert fake.muts("setDomain")


def test_annotator_is_idempotent_and_respects_human_table_docs(monkeypatch):
    monkeypatch.setenv("DATAHUB_TOKEN", "tok")
    done = json.loads(json.dumps(DS))
    done["tags"] = {"tags": [{"tag": {"urn": "urn:li:tag:ai-annotated", "properties": {"name": "ai-annotated"}}}]}
    fake = FakeGQL(done)
    monkeypatch.setattr(cat, "_gql", fake)
    monkeypatch.setattr(cat, "_llm_json", lambda m, meta: pytest.fail("no LLM call for an annotated table"))
    assert cat.annotate([DS["urn"]])["skipped"] == 1
    human = json.loads(json.dumps(DS))
    human["editableProperties"] = {"description": "Curated by the billing team."}
    for f in human["schemaMetadata"]["fields"]:
        f["description"] = "human"
    fake = FakeGQL(human)
    monkeypatch.setattr(cat, "_gql", fake)
    assert cat.annotate([DS["urn"]], force=True)["skipped"] == 1 and not fake.muts("updateDescription")


def test_cancel_marks_job_cancelled_and_iso_times():
    with pytest.raises(cat.CatalogError):
        cat.cancel("nope")
    cat._jobs["j1"] = {"job_id": "j1", "state": "running", "phase": "annotate", "log_tail": "", "counts": {},
                       "scope": {"catalog": "warehouse"}, "started": 0, "finished": None}
    assert cat.cancel("j1")["state"] == "cancelled"
    assert cat._iso_ms(0) is None and cat._iso_ms(1760832000000) == "2025-10-19T00:00:00Z"


def test_graphql_errors_with_partial_data_raise(monkeypatch):
    monkeypatch.setenv("DATAHUB_TOKEN", "tok")

    class R:
        status_code = 200
        text = ""

        def json(self):
            return {"data": {"addTags": None}, "errors": [{"message": "Failed to update urn:li:tag:x"}]}
    monkeypatch.setattr(cat.httpx, "post", lambda *a, **k: R())
    with pytest.raises(cat.CatalogError, match="Failed to update"):
        cat._gql("mutation { addTags }")


def test_rerank_prefers_table_names_with_the_query_words():
    hits = [{"name": "bq_x.a.event_cube_agg", "description": "per account churn events"},
            {"name": "bq_x.b.days_until_churn_eis", "description": ""},
            {"name": "warehouse.marts.dim_customer", "description": ""}]
    assert cat.rerank("days-until-churn per account", hits)[0]["name"].endswith("days_until_churn_eis")
    assert cat.rerank("", hits) == hits
