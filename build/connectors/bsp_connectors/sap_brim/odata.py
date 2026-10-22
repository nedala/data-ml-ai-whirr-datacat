"""OData V4 client for the SAP S/4HANA "Contract Accounting Dispute Case" API
(and the in-appliance mock that mirrors it).

* Basic auth with a technical (communication) user.
* CSRF handshake: GET the service root with `X-CSRF-Token: Fetch`, keep the
  session cookies, send the token on every modifying request; on a 403 with
  `X-CSRF-Token: Required` the token is refetched once.
* $metadata-driven: the payload is built from field_map.yaml and filtered to
  properties / navigation properties the target $metadata declares.
* Idempotent create: search by external reference (the case ref) first.
"""

from __future__ import annotations

import re
import threading
import xml.etree.ElementTree as ET
from decimal import Decimal
from typing import Any

import httpx

from .config import cfg, field_map, mode

EDM = "{http://docs.oasis-open.org/odata/ns/edm}"
EDMX = "{http://docs.oasis-open.org/odata/ns/edmx}"


class ODataError(RuntimeError):
    def __init__(self, msg: str, status: int | None = None):
        super().__init__(msg)
        self.status = status


def account_keys(ban: str) -> dict[str, str]:
    """Synthetic BAN → SAP keys convention used by the mock system: the
    10-digit BAN *is* the contract account (FI-CA VKONT) and the business
    partner is "1" + the BAN's last 9 digits (3100000008 → BP 1100000008,
    contract account 3100000008). Against a real system set
    SAP_BRIM_ACCOUNT_KEYS=passthrough and supply business_partner /
    contract_account on the case (e.g. from a Contract Account API lookup by
    BAN) — see docs/ai-engineer/sap-brim-integration.md."""
    digits = re.sub(r"\D", "", ban or "")
    if len(digits) != 10:
        return {}
    return {"business_partner": "1" + digits[1:], "contract_account": digits}


def _fmt(template: str, case: dict) -> str:
    class D(dict):
        def __missing__(self, k):
            return ""
    return template.format_map(D({k: ("" if v is None else v) for k, v in case.items()}))


def build_payload(case: dict, meta: dict[str, Any] | None = None) -> tuple[dict, list[str]]:
    fm = field_map()
    case = dict(case)
    import os
    if os.environ.get("SAP_BRIM_ACCOUNT_KEYS", "derive") == "derive":
        for k, v in account_keys(case.get("ban") or "").items():
            case.setdefault(k, v)
    out: dict[str, Any] = {}
    for f in fm["fields"]:
        if "template" in f:
            val = _fmt(f["template"], case).strip(": ")
        else:
            val = case.get(f.get("source", ""))
        if val in (None, "") and "default" in f:
            val = f["default"]
        if val in (None, ""):
            continue
        if f.get("type") == "decimal":
            val = str(Decimal(str(val)).quantize(Decimal("0.01")))
        if f.get("max_len"):
            val = str(val)[: f["max_len"]]
        out[f["target"]] = val
    for nav in (fm.get("navigation") or {}).values():
        items = []
        for it in nav["items"]:
            if not case.get(it["when"]):
                continue
            items.append({k: _fmt(v, case) for k, v in it.items() if k != "when"})
        if items:
            out[nav["target"]] = items
    dropped: list[str] = []
    if meta:
        allowed = set(meta["properties"]) | set(meta["navigation"])
        dropped = [k for k in out if k not in allowed]
        out = {k: v for k, v in out.items() if k in allowed}
    return out, dropped


def parse_metadata(xml: str, entity_set: str) -> dict[str, Any]:
    root = ET.fromstring(xml)
    types: dict[str, Any] = {}
    sets: dict[str, str] = {}
    for schema in root.iter(f"{EDM}Schema"):
        ns = schema.get("Namespace", "")
        for et in schema.findall(f"{EDM}EntityType"):
            types[f"{ns}.{et.get('Name')}"] = et
        for cont in schema.findall(f"{EDM}EntityContainer"):
            for es in cont.findall(f"{EDM}EntitySet"):
                sets[es.get("Name")] = es.get("EntityType")
    if entity_set not in sets:
        raise ODataError(f"$metadata has no entity set {entity_set!r} (has: {', '.join(sorted(sets))})")
    et = types.get(sets[entity_set])
    if et is None:
        raise ODataError(f"$metadata: entity type {sets[entity_set]} not found")
    return {"entity_type": sets[entity_set],
            "properties": [p.get("Name") for p in et.findall(f"{EDM}Property")],
            "navigation": [p.get("Name") for p in et.findall(f"{EDM}NavigationProperty")],
            "key": [k.get("Name") for k in et.iter(f"{EDM}PropertyRef")]}


class ODataClient:
    def __init__(self, base_url: str, user: str, password: str, *, client: str = "", verify: bool = True,
                 timeout: float = 30.0):
        fm = field_map()
        self.root = base_url.rstrip("/") + fm["service_path"]
        self.entity_set = fm["entity_set"]
        self.key = fm["key"]
        self.ext_ref = fm["external_reference"]
        self.params = {"sap-client": client} if client else {}
        self.http = httpx.Client(auth=(user, password) if user else None, verify=verify, timeout=timeout,
                                 headers={"Accept": "application/json"})
        self._token: str | None = None
        self._meta: dict | None = None
        self._lock = threading.Lock()

    @classmethod
    def from_env(cls) -> "ODataClient":
        c = cfg()
        if mode() == "api" and not (c.user and c.password):
            raise ODataError("SAP_BRIM_MODE=api needs SAP_BRIM_USER and SAP_BRIM_PASSWORD (Studio → Credentials)")
        return cls(c.base_url, c.user, c.password, client=c.client, verify=c.verify_tls, timeout=c.timeout_s)

    # ── plumbing ──────────────────────────────────────────────────────
    def _check(self, r: httpx.Response, what: str) -> httpx.Response:
        if r.status_code >= 400:
            msg = r.text[:400]
            try:
                msg = r.json()["error"]["message"]
                msg = msg.get("value", msg) if isinstance(msg, dict) else msg
            except Exception:
                pass
            raise ODataError(f"{what}: HTTP {r.status_code}: {msg}", r.status_code)
        return r

    def csrf(self, refresh: bool = False) -> str:
        with self._lock:
            if self._token and not refresh:
                return self._token
            r = self._check(self.http.get(self.root + "/", params=self.params, headers={"X-CSRF-Token": "Fetch"}),
                            "CSRF fetch")
            tok = r.headers.get("x-csrf-token")
            if not tok:
                raise ODataError("service did not return an X-CSRF-Token")
            self._token = tok
            return tok

    def metadata(self) -> dict:
        if self._meta is None:
            r = self._check(self.http.get(self.root + "/$metadata", params=self.params,
                                          headers={"Accept": "application/xml"}), "$metadata")
            self._meta = parse_metadata(r.text, self.entity_set)
        return self._meta

    def _modify(self, method: str, url: str, json: dict) -> httpx.Response:
        for attempt in (0, 1):
            r = self.http.request(method, url, params=self.params, json=json,
                                  headers={"X-CSRF-Token": self.csrf(refresh=attempt == 1),
                                           "Content-Type": "application/json"})
            if r.status_code == 403 and r.headers.get("x-csrf-token", "").lower() == "required" and attempt == 0:
                continue
            return r
        return r

    # ── API ───────────────────────────────────────────────────────────
    def find_by_external_reference(self, ref: str) -> dict | None:
        safe = ref.replace("'", "''")
        r = self._check(self.http.get(f"{self.root}/{self.entity_set}", params={
            **self.params, "$filter": f"{self.ext_ref} eq '{safe}'", "$top": "1"}), "search")
        rows = r.json().get("value", [])
        return self._result(rows[0], "exists") if rows else None

    def get(self, dispute_id: str) -> dict:
        safe = dispute_id.replace("'", "''")
        r = self._check(self.http.get(f"{self.root}/{self.entity_set}('{safe}')", params=self.params), "read")
        return r.json()

    def create(self, case: dict) -> dict:
        meta = self.metadata()
        if case.get("case_ref"):
            hit = self.find_by_external_reference(case["case_ref"])
            if hit:
                return hit
        payload, dropped = build_payload(case, meta)
        r = self._check(self._modify("POST", f"{self.root}/{self.entity_set}", payload), "create")
        res = self._result(r.json(), "created")
        res["payload"] = payload
        if dropped:
            res["dropped_fields"] = dropped
        return res

    def _result(self, row: dict, status: str) -> dict:
        did = str(row.get(self.key, ""))
        base = self.root.split("/sap/opu/")[0]
        return {"dispute_id": did, "status": status, "mode": mode(), "adapter": "odata",
                "url": f"{base}/ui/dispute/{did}" if mode() == "mock" else f"{self.root}/{self.entity_set}('{did}')",
                "record": row}
