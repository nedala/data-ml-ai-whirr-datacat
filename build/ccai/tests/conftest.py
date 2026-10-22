import os
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parents[2]
for p in (ROOT / "build" / "ccai", ROOT / "build" / "common", ROOT / "build" / "seed", ROOT / "build" / "connectors"):
    sys.path.insert(0, str(p))
os.environ.setdefault("CCAI_DATA_DIR", tempfile.mkdtemp(prefix="ccai-test-"))
os.environ.setdefault("CONNECTORS_STATE_DIR", tempfile.mkdtemp(prefix="ccai-conn-"))
os.environ.setdefault("CCAI_TAXONOMY", str(ROOT / "config" / "taxonomy" / "ccai_taxonomy.yaml"))
os.environ.setdefault("CCAI_SCHEDULER", "0")
os.environ.setdefault("LLM_GATEWAY_URL", "http://127.0.0.1:9/ai/v1")  # unreachable → offline route

FIXTURES = ROOT / "build/connectors/fixtures/m365/sites/default/CCAI/CallLogs"
GROUND_TRUTH = HERE / "ground_truth.json"
