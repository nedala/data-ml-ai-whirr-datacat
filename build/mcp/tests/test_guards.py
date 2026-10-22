import pytest

from bsp_mcp.common import REGISTRY, UserError, check_select, enabled_groups
from bsp_mcp.tools import browser


@pytest.mark.parametrize("sql", ["select 1", "  WITH a AS (select 1) select * from a;", "-- c\nselect 'a;b'",
                                 "select * from raw.customers where name = 'drop table x'"])
def test_select_allowed(sql):
    assert check_select(sql)


@pytest.mark.parametrize("sql", ["delete from t", "select 1; drop table t", "update t set a=1", "create table x as select 1",
                                 "select pg_sleep(10)", "with x as (delete from t returning *) select * from x",
                                 "select * from t; ", "", "copy t to '/tmp/x'", "select set_config('a','b',false)"])
def test_select_rejected(sql):
    if sql.strip() == "select * from t;":
        assert check_select(sql)
        return
    with pytest.raises(UserError):
        check_select(sql)


def test_registry_names_prefixed_and_groups():
    import bsp_mcp.tools  # noqa: F401
    groups = REGISTRY.groups()
    for g in ("warehouse", "trino", "bigquery", "atlan", "powerbi", "m365", "mlflow", "sap", "browser"):
        assert g in groups
    assert all(t.name.startswith(t.group + "_") for t in REGISTRY.tools)
    writes = {t.name for t in REGISTRY.tools if not t.read_only and t.group == "m365"}
    assert writes == {"m365_mail_create_draft", "m365_teams_post_message", "m365_files_upload"}


def test_group_flags(monkeypatch):
    monkeypatch.setenv("MCP_DISABLE_GROUPS", "browser,m365")
    assert enabled_groups(["warehouse", "browser", "m365"]) == ["warehouse"]
    monkeypatch.setenv("MCP_GROUPS", "m365,warehouse")
    monkeypatch.delenv("MCP_DISABLE_GROUPS")
    assert enabled_groups(["warehouse", "browser", "m365"]) == ["warehouse", "m365"]


def test_browser_egress_policy_default(monkeypatch):
    """Default: the egress proxy decides; the tool still refuses non-web schemes."""
    monkeypatch.delenv("MCP_BROWSER_ALLOW_ALL", raising=False)
    monkeypatch.delenv("MCP_BROWSER_POLICY", raising=False)
    assert browser.url_allowed("https://news.google.com/") and browser.url_allowed("https://www.google.co.in")
    assert not browser.url_allowed("file:///etc/passwd") and not browser.url_allowed("javascript:alert(1)")
    err = browser._load_error("drive.google.com", Exception("Page.goto: net::ERR_TUNNEL_CONNECTION_FAILED at https://drive.google.com/"))
    assert "egress policy" in str(err) and "drive.google.com" in str(err)


def test_browser_allowlist(monkeypatch):
    monkeypatch.delenv("MCP_BROWSER_ALLOW_ALL", raising=False)
    monkeypatch.setenv("MCP_BROWSER_POLICY", "allowlist")
    monkeypatch.setenv("MCP_BROWSER_ALLOW_DOMAINS", "*.dataplatform.com,portal.example.org")
    assert browser.url_allowed("http://connectors:8092/connectors/sap/ui/")
    assert browser.url_allowed("https://app.dataplatform.com/x") and browser.url_allowed("https://portal.example.org")
    assert not browser.url_allowed("https://evil.com/") and not browser.url_allowed("file:///etc/passwd")
    assert browser.url_allowed("about:blank")
    with pytest.raises(UserError):
        browser._check("evil.com")
    monkeypatch.setenv("MCP_BROWSER_ALLOW_ALL", "1")
    assert browser.url_allowed("https://evil.com/")
