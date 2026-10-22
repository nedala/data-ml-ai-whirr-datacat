"""Test fixtures: an isolated fixture tree + state dir per session, mock mode forced.

Run (Python 3.11, in a container):
    PYTHONPATH=build/common:build/connectors pytest -q build/connectors/tests
"""

from __future__ import annotations

import os
import shutil
import socket
import sys
import threading
import time
from email.message import EmailMessage
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

HERE = Path(__file__).resolve().parent
REPO_FIX = HERE.parent / "fixtures"
TEST_MAILBOX = "test-disputes@example.net"

for k in ("M365_TENANT_ID", "M365_CLIENT_ID", "M365_CLIENT_SECRET", "ATLAN_BASE_URL", "ATLAN_API_TOKEN",
          "GCP_SA_JSON", "GOOGLE_APPLICATION_CREDENTIALS", "GCP_BQ_JSON", "GCP_BQ_PROJECT", "GCP_BQ_EXEC_PROJECT",
          "GCP_BQ_DATA_PROJECTS"):
    os.environ.pop(k, None)


def _mail(i: int, subject: str, body: str, attach: bytes | None, when: datetime) -> bytes:
    m = EmailMessage()
    m["From"] = f"Customer {i} <bsp01000{i:02d}@example.net>"
    m["To"] = TEST_MAILBOX
    m["Subject"] = subject
    m["Date"] = format_datetime(when)
    m.set_content(body)
    if attach is not None:
        m.add_attachment(attach, maintype="text", subtype="csv", filename=f"invoice_{i}.csv")
    return bytes(m)


@pytest.fixture(scope="session", autouse=True)
def isolated(tmp_path_factory):
    root = tmp_path_factory.mktemp("bsp")
    fx = root / "fixtures"
    for sub in ("atlan", "powerbi", "m365/sites/default/Finance", "m365/lists",
                "m365/mailboxes/network-ops@example.net"):
        shutil.copytree(REPO_FIX / sub, fx / sub)
    inbox = fx / "m365" / "mailboxes" / TEST_MAILBOX / "inbox"
    inbox.mkdir(parents=True)
    t0 = datetime(2026, 10, 1, 9, 0, tzinfo=timezone.utc)
    for i in range(1, 6):
        att = f"invoice_id,amount\n{3100000000 + i * 7 + i % 5}-2609,{50 + i}.00\n".encode() if i % 2 else None
        (inbox / f"{i:03d}.eml").write_bytes(_mail(i, f"Billing dispute #{i}", f"I was overcharged on invoice {i}.",
                                                   att, t0 + timedelta(days=i)))
    lake = root / "lake" / "samples" / "bsp"
    lake.mkdir(parents=True)
    import pandas as pd
    pd.DataFrame({"ban": [f"{3100000000 + i * 7 + i % 5}" for i in range(10)],
                  "state": ["OH", "NC"] * 5, "autopay": [True, False] * 5,
                  "signup_date": pd.to_datetime(["2025-01-01"] * 10).date}).to_parquet(lake / "customers.parquet")
    os.environ.update(CONNECTORS_FIXTURES_DIR=str(fx), CONNECTORS_STATE_DIR=str(root / "state"),
                      LAKE_DIR=str(root / "lake"), ATLAN_MOCK_SOURCE="static", BIGQUERY_MOCK_SOURCE="parquet",
                      PG_HOST="127.0.0.1", PG_PORT="1",  # no warehouse: exercise the offline fallbacks
                      UI_KIT_DIR=str(HERE.parents[1] / "ui-kit"))
    yield root


@pytest.fixture(scope="session")
def live_server(isolated):
    """The connectors app on a real port (for live-client ↔ mock parity tests)."""
    import uvicorn

    from bsp_connectors.app import app
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    yield f"http://127.0.0.1:{port}/connectors"
    server.should_exit = True
    th.join(5)


sys.path.insert(0, str(HERE.parents[1] / "common"))
sys.path.insert(0, str(HERE.parent))
