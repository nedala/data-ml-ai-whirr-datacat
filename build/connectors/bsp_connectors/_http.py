"""HTTP for the live clients: Entra ID (msal client-credentials) tokens,
retry on throttling (429 / 503 / 504 honouring Retry-After), and OData
paging (@odata.nextLink)."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Callable, Iterator

import httpx

from ._common import ConnectorError, NotFound, env

log = logging.getLogger("bsp_connectors.http")
RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_RETRIES = int(env("CONNECTORS_MAX_RETRIES", "6"))
TIMEOUT = float(env("CONNECTORS_TIMEOUT", "60"))

GRAPH_SCOPE = "https://graph.microsoft.com/.default"
POWERBI_SCOPE = "https://analysis.windows.net/powerbi/api/.default"

_apps: dict[tuple, Any] = {}
_lock = threading.Lock()
_client: httpx.Client | None = None


def client() -> httpx.Client:
    global _client
    if _client is None:
        # trust_env: picks up HTTPS_PROXY (px-proxy) and the corporate CA bundle
        _client = httpx.Client(timeout=TIMEOUT, follow_redirects=True, trust_env=True)
    return _client


def entra_token(scope: str, force: bool = False) -> str:
    """App-only token for the configured Entra app (M365_TENANT_ID / M365_CLIENT_ID /
    M365_CLIENT_SECRET). msal caches tokens until shortly before expiry."""
    tenant, cid, secret = env("M365_TENANT_ID"), env("M365_CLIENT_ID"), env("M365_CLIENT_SECRET")
    if not (tenant and cid and secret):
        raise ConnectorError("Microsoft Entra app not configured (M365_TENANT_ID / M365_CLIENT_ID / "
                             "M365_CLIENT_SECRET)", system="m365")
    import msal

    key = (tenant, cid)
    with _lock:
        if force:
            _apps.pop(key, None)  # a fresh app has an empty token cache
        app = _apps.get(key)
        if app is None:
            authority = env("M365_AUTHORITY_HOST", "https://login.microsoftonline.com").rstrip("/") + "/" + tenant
            app = msal.ConfidentialClientApplication(cid, authority=authority, client_credential=secret)
            _apps[key] = app
    res = app.acquire_token_for_client(scopes=[scope])
    if "access_token" not in res:
        raise ConnectorError(f"token request failed: {res.get('error')}: {res.get('error_description', '')[:300]}",
                             status=401)
    return res["access_token"]


def request(method: str, url: str, *, token: Callable[[bool], str] | None = None, params: dict | None = None,
            json: Any = None, content: bytes | None = None, headers: dict | None = None,
            raw: bool = False, system: str = "") -> Any:
    """One API call with auth, throttling back-off and a single token refresh on 401.
    Returns parsed JSON (or None for empty bodies), or the httpx.Response when raw."""
    refreshed = False
    for attempt in range(MAX_RETRIES + 1):
        h = dict(headers or {})
        if token is not None:
            h["Authorization"] = f"Bearer {token(False)}"
        try:
            r = client().request(method, url, params=params, json=json, content=content, headers=h)
        except httpx.TransportError as e:
            if attempt >= MAX_RETRIES:
                raise ConnectorError(f"{method} {url}: {e}", system=system) from e
            time.sleep(min(2 ** attempt, 30))
            continue
        if r.status_code == 401 and token is not None and not refreshed:
            refreshed = True
            token(True)
            continue
        if r.status_code in RETRY_STATUS and attempt < MAX_RETRIES:
            ra = r.headers.get("Retry-After", "")
            wait = float(ra) if ra.replace(".", "", 1).isdigit() else min(2 ** attempt, 60)
            log.info("%s %s → %s, retrying in %.1fs", method, url.split("?")[0], r.status_code, wait)
            time.sleep(min(wait, 120))
            continue
        if r.status_code >= 400:
            body = r.text[:500]
            cls = NotFound if r.status_code == 404 else ConnectorError
            raise cls(f"{method} {url.split('?')[0]} → {r.status_code}: {body}", status=r.status_code, system=system)
        if raw:
            return r
        if not r.content:
            return None
        ct = r.headers.get("content-type", "")
        return r.json() if "json" in ct else r.content
    raise ConnectorError(f"{method} {url}: retries exhausted", system=system)


def paged(url: str, *, token: Callable[[bool], str] | None = None, params: dict | None = None,
          limit: int | None = None, system: str = "") -> Iterator[dict]:
    """Yield `value` items across @odata.nextLink pages."""
    n = 0
    while url:
        data = request("GET", url, token=token, params=params, system=system) or {}
        for item in data.get("value", []):
            yield item
            n += 1
            if limit is not None and n >= limit:
                return
        url, params = data.get("@odata.nextLink"), None
