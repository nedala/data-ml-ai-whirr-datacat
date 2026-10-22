"""Unit tests for the allowlist semantics (stdlib unittest; no pytest in image).

  docker run --rm --user 1000:1000 --entrypoint python -v "$PWD/build/px-proxy:/opt/px-proxy:ro" \
    -w /opt/px-proxy bsp/appliance:latest -m unittest discover -s tests -v
"""

import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from policy import Policy, PolicyError, compile_pattern, normalize_host, parse_target  # noqa: E402

DEFAULT = (HERE.parent / "allowlist.default.yaml").read_text()


class PatternTests(unittest.TestCase):
    def m(self, pattern, host):
        return bool(compile_pattern(pattern).fullmatch(normalize_host(host)))

    def test_exact(self):
        self.assertTrue(self.m("pypi.org", "pypi.org"))
        self.assertTrue(self.m("pypi.org", "PyPI.org."))
        self.assertFalse(self.m("pypi.org", "evilpypi.org"))
        self.assertFalse(self.m("pypi.org", "x.pypi.org"))

    def test_leading_wildcard_is_any_depth_but_not_apex(self):
        self.assertTrue(self.m("*.dataplatform.com", "a.dataplatform.com"))
        self.assertTrue(self.m("*.dataplatform.com", "a.b.c.dataplatform.com"))
        self.assertFalse(self.m("*.dataplatform.com", "dataplatform.com"))
        self.assertFalse(self.m("*.dataplatform.com", "evildataplatform.com"))
        self.assertFalse(self.m("*.dataplatform.com", "dataplatform.com.evil.io"))

    def test_inner_wildcard_stays_in_label(self):
        self.assertTrue(self.m("cdn-lfs*.huggingface.co", "cdn-lfs.huggingface.co"))
        self.assertTrue(self.m("cdn-lfs*.huggingface.co", "cdn-lfs-us-1.huggingface.co"))
        self.assertFalse(self.m("cdn-lfs*.huggingface.co", "cdn-lfs.evil.com.huggingface.co.evil"))
        self.assertFalse(self.m("cdn-lfs*.huggingface.co", "x.cdn-lfs.huggingface.co"))

    def test_regex(self):
        self.assertTrue(self.m(r"regex:^ab[0-9]+\.example\.com$", "ab12.example.com"))
        with self.assertRaises(ValueError):
            compile_pattern("regex:.*")
        with self.assertRaises(ValueError):
            compile_pattern("regex:(")

    def test_too_broad_rejected(self):
        for bad in ("*", "*.com", "com", "*.*.com", "foo.*", "pypi.*", "a..b.com", "pypi.org/simple", ""):
            with self.subTest(bad=bad), self.assertRaises(ValueError):
                compile_pattern(bad)

    def test_idna(self):
        self.assertEqual(normalize_host("Bücher.example"), "xn--bcher-kva.example")


class PolicyTests(unittest.TestCase):
    def setUp(self):
        self.p = Policy.from_yaml(DEFAULT)

    def test_default_loads_cleanly(self):
        self.assertEqual(self.p.mode, "enforce")
        self.assertGreater(len(self.p.rules), 40)
        self.assertEqual(getattr(self.p, "problems", []), [])

    def test_required_defaults(self):
        for host in ("pypi.org", "files.pythonhosted.org", "huggingface.co",
                     "cdn-lfs-us-1.huggingface.co", "github.com", "objects.githubusercontent.com",
                     # Google Cloud: BigQuery, Vertex AI, sign-in, Cloud Storage
                     "bigquery.googleapis.com", "bigquerystorage.googleapis.com", "oauth2.googleapis.com",
                     "aiplatform.googleapis.com", "us-central1-aiplatform.googleapis.com", "accounts.google.com",
                     "storage.googleapis.com", "my-bucket.storage.googleapis.com",
                     # Atlan, Microsoft, Power BI
                     "atlan.example.com", "login.microsoftonline.com", "graph.microsoft.com",
                     "contoso.sharepoint.com", "api.powerbi.com", "app.powerbi.com", "analysis.windows.net",
                     "wabi-us-north-central-redirect.analysis.windows.net", "aadcdn.msauth.net",
                     # object storage (integration canvas)
                     "my-bucket.s3.us-east-1.amazonaws.com", "acct.blob.core.windows.net", "acct.dfs.core.windows.net",
                     # model registries, editors, dbt, AI
                     "registry.ollama.ai", "ollama.com", "open-vsx.org", "openvsx.eclipsecontent.org",
                     "hub.getdbt.com", "api.anthropic.com", "claude.ai", "ghcr.io", "api.github.com",
                     # Antigravity
                     "antigravity.google", "edgedl.me.gvt1.com",
                     # corporate
                     "dataplatform.com", "sap.dataplatform.com"):
            with self.subTest(host=host):
                self.assertTrue(self.p.decide(host, 443).allowed)

    def test_blocked(self):
        for host in ("example.com", "pastebin.com", "dataplatform.com.evil.io", "drive.google.com", "10.0.0.1",
                     "conda.anaconda.org"):
            with self.subTest(host=host):
                d = self.p.decide(host, 443)
                self.assertFalse(d.allowed)
                self.assertEqual(d.action, "block")

    def test_deny_rule_wins(self):
        p = Policy.from_dict({"mode": "audit", "rules": [
            {"pattern": "*.google.com", "ports": [443]},
            {"pattern": "drive.google.com", "action": "deny"}]})
        self.assertTrue(p.decide("news.google.com", 443).allowed)
        d = p.decide("drive.google.com", 443)
        self.assertFalse(d.allowed, "deny wins even in audit mode")
        self.assertEqual((d.action, d.reason), ("block", "denied-by-rule"))
        self.assertEqual(p.to_dict()["rules"][1]["action"], "deny", "round-trips")
        with self.assertRaises(PolicyError):
            Policy.from_dict({"rules": [{"pattern": "x.com", "action": "maybe"}]})

    def test_ports(self):
        self.assertFalse(self.p.decide("pypi.org", 80).allowed)        # ports: [443]
        self.assertEqual(self.p.decide("pypi.org", 22).reason, "port-not-allowed")
        self.assertTrue(self.p.decide("sqlops01.dataplatform.com", 1433).allowed)  # ports: any (TCP tunnels)
        self.assertTrue(self.p.decide("orabill01.dataplatform.com", 1521).allowed)
        self.assertFalse(self.p.decide("api.powerbi.com", 1433).allowed)

    def test_microsoft_rules_stay_narrow(self):
        for host in ("evil.windows.net", "x.core.windows.net", "microsoft.com.evil.io", "sharepoint.com",
                     "evil-office.com", "office.com.evil.io"):
            with self.subTest(host=host):
                self.assertFalse(self.p.decide(host, 443).allowed)

    def test_audit(self):
        self.p.mode = "audit"
        d = self.p.decide("example.com", 443)
        self.assertTrue(d.allowed)
        self.assertEqual(d.action, "audit-allow")

    def test_strict_vs_lenient(self):
        data = {"mode": "enforce", "rules": [{"pattern": "*"}, {"pattern": "ok.example.com"}]}
        with self.assertRaises(PolicyError):
            Policy.from_dict(data, strict=True)
        p = Policy.from_dict(data, strict=False)
        self.assertEqual([r.pattern for r in p.rules], ["ok.example.com"])
        self.assertEqual(len(p.problems), 1)

    def test_tunnels_validation(self):
        good = {"name": "dwh", "listen_port": 14330, "target_host": "sql01.dataplatform.com", "target_port": 1433}
        Policy.from_dict({"tcp_tunnels": [good]})
        for bad in ({**good, "listen_port": 443}, {**good, "listen_port": 3128}, {**good, "name": "bad name"},
                    {**good, "target_host": "not a host"}):
            with self.subTest(bad=bad), self.assertRaises(PolicyError):
                Policy.from_dict({"tcp_tunnels": [bad]})
        with self.assertRaises(PolicyError):
            Policy.from_dict({"tcp_tunnels": [good, {**good, "name": "x"}]})  # port twice

    def test_roundtrip_and_upsert(self):
        p2 = Policy.from_yaml(self.p.to_yaml())
        self.assertEqual(p2.to_dict(), self.p.to_dict())
        from policy import rule_from_dict

        added, updated = p2.upsert_rules([rule_from_dict({"pattern": "new.example.com"}),
                                          rule_from_dict({"pattern": "PYPI.org", "reason": "changed"})])
        self.assertEqual(added, ["new.example.com"])
        self.assertEqual(updated, ["pypi.org"])
        self.assertEqual(p2.remove_rules(["new.example.com"]), ["new.example.com"])

    def test_parse_target(self):
        self.assertEqual(parse_target("https://PyPI.org/simple"), ("pypi.org", 443, "https"))
        self.assertEqual(parse_target("http://x.example.com:8080/"), ("x.example.com", 8080, "http"))
        self.assertEqual(parse_target("sql01.dataplatform.com:1433"), ("sql01.dataplatform.com", 1433, "https"))


class DefaultFileTests(unittest.TestCase):
    def test_rules_have_only_known_keys(self):
        # an unquoted comma inside a flow-mapping reason splits it into a
        # stray key ("reason: a (b, c)," -> {"reason": "a (b", "c)": None})
        import yaml
        known = {"pattern", "category", "reason", "added_by", "ports", "added_at", "action", "tls", "enabled"}
        for rule in yaml.safe_load(DEFAULT)["rules"]:
            self.assertFalse(set(rule) - known, rule)


class EnvRuleTests(unittest.TestCase):
    def test_configured_hosts_are_allowed_in_memory_only(self):
        from policy import ENV_TAG, env_rules
        p = Policy.from_yaml(DEFAULT)
        env = {"SAP_BRIM_BASE_URL": "https://brim.partner-sap.example:8443/sap/opu/odata",
               "ATLAN_BASE_URL": "https://atlan.example.com",              # already allowed → no extra rule
               "EGRESS_EXTRA_HOSTS": "files.vendor.example, db.vendor.example:5432 ,"}
        extra = env_rules(env, existing=p)
        self.assertEqual([r.pattern for r in extra], ["brim.partner-sap.example", "files.vendor.example", "db.vendor.example"])
        p.rules.extend(extra)
        self.assertTrue(p.decide("brim.partner-sap.example", 8443).allowed)
        self.assertTrue(p.decide("brim.partner-sap.example", 443).allowed)
        self.assertTrue(p.decide("db.vendor.example", 5432).allowed)
        self.assertFalse(p.decide("other.partner-sap.example", 443).allowed)
        self.assertTrue(all(r.added_by == ENV_TAG for r in extra))
        self.assertNotIn("partner-sap", p.to_yaml(), "env rules are never written to the allowlist file")

    def test_store_applies_env_rules(self):
        import os
        from store import PolicyStore
        os.environ["SAP_BRIM_BASE_URL"] = "https://brim.partner-sap.example"
        try:
            with tempfile.TemporaryDirectory() as td:
                st = PolicyStore(Path(td) / "a.yaml", HERE.parent / "allowlist.default.yaml")
                self.assertTrue(st.policy.decide("brim.partner-sap.example", 443).allowed)
                self.assertNotIn("partner-sap", (Path(td) / "a.yaml").read_text())
        finally:
            os.environ.pop("SAP_BRIM_BASE_URL")


class StoreTests(unittest.TestCase):
    def test_seed_reload_and_bad_yaml(self):
        from store import PolicyStore

        with tempfile.TemporaryDirectory() as td:
            path = Path(td) / "allowlist.yaml"
            st = PolicyStore(path, HERE.parent / "allowlist.default.yaml", env_mode="audit")
            self.assertTrue(path.exists())
            self.assertEqual(st.policy.mode, "audit")  # EGRESS_MODE seeds a new file
            good_version = st.version
            time.sleep(0.01)
            path.write_text("rules: [oops")
            import os
            os.utime(path, (time.time() + 5, time.time() + 5))
            self.assertFalse(st.reload())
            self.assertEqual(st.version, good_version)  # kept last good policy
            self.assertIsNotNone(st.last_error)

    def test_audit_disallowed(self):
        from store import PolicyStore

        with tempfile.TemporaryDirectory() as td:
            st = PolicyStore(Path(td) / "a.yaml", HERE.parent / "allowlist.default.yaml", allow_audit=False)
            p = Policy.from_dict(st.policy.to_dict())
            p.mode = "audit"
            with self.assertRaises(PolicyError):
                st.save(p)


if __name__ == "__main__":
    unittest.main()
