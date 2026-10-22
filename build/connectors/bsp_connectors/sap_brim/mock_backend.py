"""State + business rules of the in-appliance MOCK SAP BRIM system.

Mutable state lives in ${CONNECTORS_STATE_DIR:-/data/connectors}/sap_brim/
state.json. Business partners / contract accounts come from the warehouse
(raw.customers, keyed by the 10-digit BAN) when it is reachable, else from
the BAN convention (contract account = BAN).
"""

from __future__ import annotations

import os
import re
import secrets
import threading
import time
from datetime import datetime, timezone
from typing import Any

from .._common import read_json, state_dir, write_json
from .odata import account_keys

LOCK = threading.RLock()

CASE_TYPES = {"FIN1": "Billing dispute", "PAY1": "Payment dispute", "SRV1": "Service credit"}
REASONS = {
    "FIN1": {"LF01": "Late fee charged in error", "PR01": "Promotion expired", "PI01": "Price increase dispute",
             "EQ01": "Equipment non-return fee", "FB01": "Final bill after cancellation",
             "IN01": "Installation fee", "OT01": "Other billing dispute"},
    "PAY1": {"DU01": "Duplicate charge / payment", "PA01": "Payment not applied"},
    "SRV1": {"SC01": "Outage service credit", "SP01": "Speed below plan"},
}
REQUIRED = ("CaseType", "CaseReason", "ContractAccount")
PROPS = [  # (name, edm type, nullable)
    ("CaseID", "Edm.String", False), ("CaseUUID", "Edm.Guid", False), ("CaseType", "Edm.String", False),
    ("CaseReason", "Edm.String", False), ("CaseTitle", "Edm.String", True),
    ("CaseExternalReference", "Edm.String", True), ("CaseStatus", "Edm.String", True),
    ("CasePriority", "Edm.String", True), ("BusinessPartner", "Edm.String", True),
    ("ContractAccount", "Edm.String", False), ("DisputedAmount", "Edm.Decimal", True),
    ("DisputedAmountCurrency", "Edm.String", True), ("InvoiceReference", "Edm.String", True),
    ("CustomerContactEmail", "Edm.String", True), ("CustomerContactPhone", "Edm.String", True),
    ("CreatedByUser", "Edm.String", True), ("CreationDateTime", "Edm.DateTimeOffset", True),
    ("Channel", "Edm.String", True),
]


def _path():
    return state_dir("sap_brim") / "state.json"


def load() -> dict[str, Any]:
    st = read_json(_path(), None) or {}
    st.setdefault("seq", 0)
    st.setdefault("disputes", {})
    return st


def save(st: dict) -> None:
    write_json(_path(), st)


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def latency(lo: float = 0.3, hi: float = 1.2) -> float:
    """Realistic think time (seconds); SAP_BRIM_MOCK_LATENCY=0 disables."""
    scale = float(os.environ.get("SAP_BRIM_MOCK_LATENCY", "1"))
    return (lo + (hi - lo) * secrets.randbelow(1000) / 1000) * scale


def fail_rate() -> float:
    return float(os.environ.get("SAP_BRIM_MOCK_FAILRATE", "0.03"))


# ── accounts ────────────────────────────────────────────────────────────
_acct_cache: dict[str, dict | None] = {}


def _pg_lookup(where: str, params: tuple) -> list[dict]:
    if not os.environ.get("PG_HOST"):
        return []
    try:
        import psycopg2

        dsn = (f"host={os.environ.get('PG_HOST')} port={os.environ.get('PG_PORT', '5432')} "
               f"dbname={os.environ.get('PG_DB', 'bsp_edw')} user={os.environ.get('PG_USER', 'analyst')} "
               f"password={os.environ.get('PG_PASS', '')} connect_timeout=3")
        with psycopg2.connect(dsn) as conn, conn.cursor() as cur:
            cur.execute("select ban, customer_name, city, state, zip, segment, clli from raw.customers "
                        f"where {where} order by ban limit 20", params)
            cols = [d[0] for d in cur.description]
            return [dict(zip(cols, r)) for r in cur.fetchall()]
    except Exception:
        return []


def _acct(row: dict) -> dict:
    return {**row, **account_keys(row["ban"])}


def search_accounts(q: str) -> list[dict]:
    """By BAN (10 digits, = contract account), business partner (1 + 9
    digits) or customer name."""
    q = (q or "").strip()
    if not q:
        return []
    digits = re.sub(r"[\s-]", "", q)
    if re.fullmatch(r"3\d{9}", digits):        # BAN / contract account
        ban = digits
    elif re.fullmatch(r"1\d{9}", digits):      # business partner
        ban = "3" + digits[1:]
    else:
        rows = _pg_lookup("customer_name ilike %s", (f"%{q}%",))
        return [_acct(r) for r in rows]
    if ban not in _acct_cache:
        rows = _pg_lookup("ban = %s", (ban,))
        _acct_cache[ban] = rows[0] if rows else (
            None if os.environ.get("PG_HOST") and os.environ.get("SAP_BRIM_MOCK_STRICT") == "1" else
            {"ban": ban, "customer_name": f"Customer {ban}", "city": "", "state": "", "zip": "",
             "segment": "residential", "clli": ""})
    r = _acct_cache[ban]
    return [_acct(r)] if r else []


def get_account(ca: str) -> dict | None:
    hits = search_accounts(ca)
    return hits[0] if hits else None


# ── disputes ────────────────────────────────────────────────────────────
class MockError(ValueError):
    pass


def validate(rec: dict) -> None:
    missing = [k for k in REQUIRED if not rec.get(k)]
    if missing:
        raise MockError(f"Required field(s) missing: {', '.join(missing)}")
    if rec["CaseType"] not in CASE_TYPES:
        raise MockError(f"Case type {rec['CaseType']} does not exist")
    if rec["CaseReason"] not in REASONS[rec["CaseType"]]:
        raise MockError(f"Reason {rec['CaseReason']} is not allowed for case type {rec['CaseType']}")
    if rec.get("DisputedAmount") not in (None, ""):
        try:
            if float(rec["DisputedAmount"]) < 0:
                raise MockError("Disputed amount must not be negative")
        except (TypeError, ValueError):
            raise MockError("Disputed amount is not a number") from None
    if rec.get("InvoiceReference") and not re.fullmatch(r"\d{10}-\d{4}", str(rec["InvoiceReference"])):
        raise MockError(f"Invoice reference {rec['InvoiceReference']} has an invalid format")


def create(rec: dict, user: str, channel: str) -> dict:
    validate(rec)
    with LOCK:
        st = load()
        ref = rec.get("CaseExternalReference")
        if ref:
            for d in st["disputes"].values():
                if d.get("CaseExternalReference") == ref:
                    raise MockError(f"FDM_DUPLICATE: external reference {ref} already used by dispute case {d['CaseID']}")
        st["seq"] += 1
        cid = f"40{st['seq']:08d}"
        import uuid
        full = {p: None for p, _, _ in PROPS}
        full.update({k: v for k, v in rec.items() if not k.startswith("_")})
        full.update({"CaseID": cid, "CaseUUID": str(uuid.uuid4()), "CaseStatus": "OPEN",
                     "CreatedByUser": user, "CreationDateTime": now(), "Channel": channel})
        if full.get("DisputedAmount") is not None:
            full["DisputedAmount"] = f"{float(full['DisputedAmount']):.2f}"
        full["_DisputedObject"] = rec.get("_DisputedObject") or []
        full["_Note"] = rec.get("_Note") or []
        st["disputes"][cid] = full
        save(st)
        return full


def find(field: str, value: str) -> list[dict]:
    return [d for d in load()["disputes"].values() if str(d.get(field)) == value]


def for_account(ca: str) -> list[dict]:
    return sorted(find("ContractAccount", ca), key=lambda d: d["CaseID"], reverse=True)


def metadata_xml() -> str:
    props = "\n".join(
        f'        <Property Name="{n}" Type="{t}" Nullable="{str(nl).lower()}"'
        + (' Precision="15" Scale="2"' if t == "Edm.Decimal" else "") + "/>" for n, t, nl in PROPS)
    return f"""<?xml version="1.0" encoding="utf-8"?>
<edmx:Edmx Version="4.0" xmlns:edmx="http://docs.oasis-open.org/odata/ns/edmx">
  <edmx:DataServices>
    <Schema Namespace="com.sap.gateway.srvd_a2x.api_cadisputecase.v0002" xmlns="http://docs.oasis-open.org/odata/ns/edm">
      <EntityType Name="DisputeCaseType">
        <Key><PropertyRef Name="CaseID"/></Key>
{props}
        <NavigationProperty Name="_DisputedObject" Type="Collection(com.sap.gateway.srvd_a2x.api_cadisputecase.v0002.DisputedObjectType)"/>
        <NavigationProperty Name="_Note" Type="Collection(com.sap.gateway.srvd_a2x.api_cadisputecase.v0002.DisputeCaseNoteType)"/>
      </EntityType>
      <EntityType Name="DisputedObjectType">
        <Key><PropertyRef Name="ObjectType"/><PropertyRef Name="ObjectKey"/></Key>
        <Property Name="ObjectType" Type="Edm.String" Nullable="false"/>
        <Property Name="ObjectKey" Type="Edm.String" Nullable="false"/>
      </EntityType>
      <EntityType Name="DisputeCaseNoteType">
        <Key><PropertyRef Name="NoteType"/></Key>
        <Property Name="NoteType" Type="Edm.String" Nullable="false"/>
        <Property Name="NoteText" Type="Edm.String"/>
      </EntityType>
      <EntityContainer Name="Container">
        <EntitySet Name="DisputeCase" EntityType="com.sap.gateway.srvd_a2x.api_cadisputecase.v0002.DisputeCaseType"/>
      </EntityContainer>
    </Schema>
  </edmx:DataServices>
</edmx:Edmx>
"""


def sleep(lo: float = 0.3, hi: float = 1.2) -> None:
    time.sleep(latency(lo, hi))
