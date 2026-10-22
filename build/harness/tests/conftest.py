import os
import sys
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE.parent), str(HERE.parents[1] / "common")]

CFG = """
version: 1
defaults: {timeout_s: 5, retries: 1, backoff_s: 0.01, max_tokens: 64}
providers:
  local: {type: ollama, local: true, base_url: "${TEST_OLLAMA:-http://nowhere:11434}", models: [{id: m1}, {id: e1, kind: embed}]}
  cloud: {type: vertex, project: p, region: us-central1, models: [{id: gemini-2.5-flash, cost_in_per_1k: 1, cost_out_per_1k: 1}]}
  anth: {type: anthropic, enabled: false, models: [{id: c}]}
aliases:
  chat-default: {routes: [local/m1, cloud/gemini-2.5-flash], params: {temperature: 0.1}}
  extract-default: {routes: [local/m1], params: {json: true}}
  cloud-first: {routes: [cloud/gemini-2.5-flash, local/m1]}
  embed-default: {kind: embed, routes: [local/e1]}
policies:
  data_classification: {default: internal, rules: {restricted: {local_only: true}}}
  budgets: {per_provider_daily_usd: {cloud: 0.000001}}
"""


@pytest.fixture(autouse=True)
def harness_env(tmp_path, monkeypatch):
    cfg = tmp_path / "providers.yaml"
    cfg.write_text(CFG)
    monkeypatch.setenv("HARNESS_CONFIG", str(cfg))
    monkeypatch.setenv("HARNESS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("HARNESS_TELEMETRY_DB", "0")
    monkeypatch.delenv("LLM_GATEWAY_TOKEN", raising=False)
    from bsp_harness import telemetry
    from bsp_harness.config import CONFIG
    CONFIG._sig = ()
    telemetry._spend.clear()
    telemetry.RING.clear()
    yield cfg
