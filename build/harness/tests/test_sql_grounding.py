from bsp_harness.agent import _norm_sql, sql_playbook, unexecuted_sql


def test_unexecuted_sql_detects_shown_but_not_run_query():
    ans = "Fiber has 2 orders.\n```sql\nSELECT technology, count(*)\nFROM t GROUP BY 1\n```"
    assert unexecuted_sql(ans, set()) == "SELECT technology, count(*)\nFROM t GROUP BY 1"
    assert unexecuted_sql(ans, {_norm_sql("select technology,  count(*) from t group by 1;")}) is None


def test_answer_without_sql_passes():
    assert unexecuted_sql("No SQL here.", set()) is None


def test_playbook_prefers_trino_when_available():
    assert "trino_sources" in sql_playbook({"trino", "warehouse"})
    assert "warehouse_list_tables" in sql_playbook({"warehouse"})
    assert sql_playbook({"browser"}) == ""


def test_playbook_searches_the_catalog_first_and_guards_bigquery():
    pb = sql_playbook({"trino", "catalog"})
    assert pb.index("catalog_search") < pb.index("trino_sources")
    assert "bq_" in pb and "PII (suspected)" in pb and "never list rows" in pb
    assert "catalog_search" not in sql_playbook({"trino"})
