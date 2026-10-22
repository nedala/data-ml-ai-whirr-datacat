import yaml

from bsp_harness.config import CONFIG, deep_merge, interpolate, validate


def test_interpolation_and_defaults(monkeypatch):
    monkeypatch.setenv("X1", "val")
    assert interpolate("a ${X1} ${NOPE:-dflt} ${NOPE2}") == "a val dflt "
    assert CONFIG.provider("local")["base_url"] == "http://nowhere:11434"


def test_deep_merge_and_delete():
    assert deep_merge({"a": {"b": 1, "c": 2}, "l": [1]}, {"a": {"b": 3, "c": None}, "l": [2]}) == {"a": {"b": 3}, "l": [2]}


def test_validate_catches_errors():
    errs = validate({"providers": {"p": {"type": "bogus"}}, "aliases": {"a": {"routes": ["q/m", "nomodel"]}}})
    assert any("type" in e for e in errs) and any("unknown provider 'q'" in e for e in errs) and any("provider/model" in e for e in errs)


def test_override_write_and_hot_reload():
    assert CONFIG.alias("new-alias") is None
    assert CONFIG.write_override({"aliases": {"new-alias": {"routes": ["nope/x"]}}})   # invalid → errors, not written
    assert CONFIG.write_override({"aliases": {"new-alias": {"routes": ["local/m1"]}}}) == []
    assert CONFIG.alias("new-alias")["routes"] == ["local/m1"]
    from bsp_harness.config import override_path
    assert yaml.safe_load(override_path().read_text())["aliases"]["new-alias"]["routes"] == ["local/m1"]
    CONFIG.reset_override()
    assert CONFIG.alias("new-alias") is None


def test_repo_providers_yaml_is_valid(monkeypatch):
    from pathlib import Path
    repo = Path(__file__).resolve().parents[3] / "config/ai/providers.yaml"
    cfg = interpolate(yaml.safe_load(repo.read_text()))
    assert validate(cfg) == []
    for a in ("chat-default", "extract-default", "classify-default", "vlm-default", "embed-default"):
        assert a in cfg["aliases"]
    assert cfg["providers"]["ollama"]["base_url"] == "http://ollama:11434"
