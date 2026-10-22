"""GCP credentials for BigQuery, Cloud Storage, Vertex AI and workspace tools.

Studio → Cloud identities assigns a named identity per consumer (registry:
build/studio/gcp_consumers.yaml) and injects it as e.g. GCP_BQ_JSON (+ GCP_BQ_PROJECT).
GCP_IDENTITIES lists every identity by NAME with metadata only (no key).

Principal switch: act as another named identity for a signed-in user —

    with gcp.acting(user, session=<studio_session cookie> | grant=... | caller_ts=..., caller_sig=...):
        creds = gcp.credentials("bigquery_read", identity="data-analyst")

Studio checks the identity's access list for that user and returns a
short-lived OAuth access token (POST /studio/internal/gcp-token, this
service's own token: CANVAS_STUDIO_TOKEN / WB_STUDIO_TOKEN / MCP_TOKEN). The
credentials refresh through Studio, re-checking access at least every
LOCAL_TTL seconds. The key itself never leaves Studio.

    credentials()             GCP_SA_JSON → GOOGLE_APPLICATION_CREDENTIALS / ADC   (unchanged)
    credentials("bigquery_read")   GCP_BQ_JSON → GCP_SA_JSON → ADC   ("bigquery" is an alias)
    credentials("bigquery_write")  GCP_BQ_WRITE_JSON → GCP_BQ_JSON → GCP_SA_JSON → ADC
    credentials(<consumer>)        vertex, trino_bigquery, gcs, mlflow_artifacts,
                                   artifact_registry, cloud_run, workspace likewise
    credentials(p, identity="x", user="u")  act as identity x for user u (through Studio)
    identity("my sign-in")    the named identity (legacy key in env, else through Studio)
    project("bigquery")       the DATA project (assignment / identity) → GCP_PROJECT_ID
    exec_project("bigquery")  the EXECUTION (job / billing) project → the data project
    data_projects("bigquery") every data project (GCP_BQ_DATA_PROJECTS) → [project]

Both key types work: service_account keys and authorized_user sign-ins
(`gcloud auth application-default login`). Keys stay in memory — never
written to disk. Returns None when nothing is configured: callers fall back
to mock mode."""

from __future__ import annotations

import base64
import contextlib
import contextvars
import datetime as _dt
import hashlib
import json
import os
import threading
import time
from typing import Any

SCOPES = ["https://www.googleapis.com/auth/cloud-platform"]
# consumer key → (env var, fallback consumer). Mirror of
# build/studio/gcp_consumers.yaml (a Studio unit test keeps them in sync).
# Studio already injects each consumer's EFFECTIVE identity, so the fallback
# chain here only matters outside Studio-started containers.
CONSUMERS: dict[str, tuple[str, str]] = {
    "vertex": ("GCP_VERTEX_JSON", ""),
    "bigquery_read": ("GCP_BQ_JSON", ""),
    "bigquery_write": ("GCP_BQ_WRITE_JSON", "bigquery_read"),
    "trino_bigquery": ("GCP_TRINO_JSON", "bigquery_read"),
    "gcs": ("GCP_GCS_JSON", ""),
    "mlflow_artifacts": ("GCP_MLFLOW_JSON", "gcs"),
    "artifact_registry": ("GCP_ARTIFACT_JSON", ""),
    "cloud_run": ("GCP_CLOUDRUN_JSON", ""),
    "workspace": ("GCP_WORKSPACE_JSON", ""),
}
ALIASES = {"bigquery": "bigquery_read", "bq": "bigquery_read", "trino": "trino_bigquery", "mlflow": "mlflow_artifacts"}
PURPOSES = tuple(CONSUMERS)


def _parse(raw: str | dict | None) -> dict[str, Any] | None:
    if isinstance(raw, dict):
        return raw or None
    raw = (raw or "").strip()
    if not raw:
        return None
    if not raw.startswith("{"):
        raw = base64.b64decode(raw).decode()
    return json.loads(raw)


def _key(purpose: str) -> str:
    key = ALIASES.get(purpose, purpose)
    if key not in CONSUMERS:
        raise ValueError(f"purpose must be one of {', '.join(CONSUMERS)}")
    return key


def _chain(purpose: str | None) -> list[str]:
    """Env vars to try for `purpose`: its own, then its fallback consumers'."""
    out: list[str] = []
    key = _key(purpose) if purpose is not None else ""
    while key and CONSUMERS[key][0] not in out:
        out.append(CONSUMERS[key][0])
        key = CONSUMERS[key][1]
    return out


def info(purpose: str | None = None) -> dict[str, Any] | None:
    """The credential JSON for `purpose` (its assigned identity, its fallback
    consumer's, else GCP_SA_JSON), or None."""
    for var in _chain(purpose):
        if os.environ.get(var, "").strip():
            return _parse(os.environ[var])
    return _parse(os.environ.get("GCP_SA_JSON", ""))


def sa_info(purpose: str | None = None) -> dict[str, Any] | None:
    """Backward compatible: GCP_SA_JSON parsed (no purpose). With a purpose,
    only a service-account key is returned (None for a personal sign-in) —
    for tools that need a key file (Trino, dlt's BigQuery destination)."""
    if purpose is None:
        return _parse(os.environ.get("GCP_SA_JSON", ""))
    i = info(purpose)
    return i if i and i.get("type") == "service_account" else None


def _identities_env() -> dict[str, Any]:
    try:
        data = json.loads(os.environ.get("GCP_IDENTITIES") or "{}")
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def _has_key(v: Any) -> bool:
    return isinstance(v, (dict, str)) and ("private_key" in str(v) or "refresh_token" in str(v)
                                           or (isinstance(v, str) and not v.lstrip().startswith("{")))


def identities() -> dict[str, dict[str, Any]]:
    """{name: credential JSON} of identities whose KEY is in this environment
    (legacy / outside Studio). Studio-started services get names only."""
    out = {}
    for name, v in _identities_env().items():
        if not _has_key(v):
            continue
        try:
            parsed = _parse(v)
        except ValueError:
            continue
        if parsed:
            out[str(name)] = parsed
    return out


def identity_names() -> list[str]:
    return sorted(str(n) for n in _identities_env())


def identity_meta(name: str) -> dict[str, Any]:
    """{id, name, type, principal, project} of a named identity (by name or id), no secrets; {} if unknown."""
    for n, v in _identities_env().items():
        if isinstance(v, dict) and (n == name or v.get("id") == name):
            if _has_key(v):
                return {"id": n, "name": n, "type": v.get("type", ""), "principal": v.get("client_email", ""),
                        "project": v.get("project_id") or v.get("quota_project_id") or ""}
            return {"name": n, **{k: v.get(k, "") for k in ("id", "type", "principal", "project")}}
    return {}


def from_info(i: dict[str, Any], scopes: list[str] | None = None):
    """google-auth Credentials for a service_account or authorized_user JSON."""
    kind = i.get("type")
    if kind == "authorized_user":
        from google.oauth2.credentials import Credentials

        return Credentials.from_authorized_user_info(i, scopes=scopes or SCOPES)
    if kind in (None, "service_account"):
        from google.oauth2 import service_account

        return service_account.Credentials.from_service_account_info(i, scopes=scopes or SCOPES)
    raise ValueError(f"unsupported Google credential type {kind!r}")


def identity(name: str, scopes: list[str] | None = None, user: str | None = None):
    """Credentials of the named identity: its key when this environment holds
    one (legacy), else a token from Studio for the acting user."""
    ids = identities()
    if name in ids:
        return from_info(ids[name], scopes)
    return studio_credentials(name, user=user, scopes=scopes)


def identity_info(name: str) -> dict[str, Any]:
    """Credential JSON of a named identity — only when its key is in this
    environment (legacy). Studio-started services use credentials(identity=)."""
    ids = identities()
    if name not in ids:
        raise KeyError(f"no key for cloud identity {name!r} in this environment — act as it through Studio "
                       "(gcp.credentials(..., identity=...))")
    return ids[name]


# ── principal switch through Studio ──────────────────────────────────────
class StudioTokenError(PermissionError):
    """Studio refused (not on the access list, no proof, Google refused …)."""

    def __init__(self, message: str, status: int = 403):
        super().__init__(message)
        self.status = status


LOCAL_TTL = 600            # re-ask Studio (and so re-check the access list) at least this often
_acting: contextvars.ContextVar = contextvars.ContextVar("bsp_gcp_acting", default=None)
_studio_cache: dict[tuple, dict[str, Any]] = {}
_studio_lock = threading.Lock()
TOKEN_ENVS = ("CANVAS_STUDIO_TOKEN", "WB_STUDIO_TOKEN", "MCP_TOKEN")


@contextlib.contextmanager
def acting(user: str, session: str = "", grant: str = "", caller_ts: str = "", caller_sig: str = ""):
    """Run with `user` (proven by a Studio session cookie, a Studio grant or
    the harness caller signature) as the principal for identity switches."""
    tok = _acting.set({"user": user or "", "session": session or "", "grant": grant or "",
                       "caller_ts": caller_ts or "", "caller_sig": caller_sig or ""})
    try:
        yield
    finally:
        _acting.reset(tok)


def acting_user() -> str:
    a = _acting.get()
    return (a or {}).get("user", "")


def _service_token() -> str:
    for name in TOKEN_ENVS:
        v = os.environ.get(name, "").strip()
        if v:
            return v
    return ""


def studio_token(identity: str, user: str | None = None, scopes: list[str] | None = None,
                 consumer: str = "") -> dict[str, Any]:
    """{access_token, expires_in, identity, principal_email, default_project, ...}
    from Studio for (identity, user). Cached per proof for at most LOCAL_TTL s."""
    a = dict(_acting.get() or {})
    if user and user != a.get("user"):
        a = {"user": user}            # a different user than the acting one: no proof carried over
    user = a.get("user", "")
    if not user:
        raise StudioTokenError("a named Google identity needs a signed-in user (none in this context)")
    proof = {k: a.get(k, "") for k in ("session", "grant", "caller_ts", "caller_sig") if a.get(k)}
    key = (identity, user.lower(), tuple(scopes or ()), hashlib.sha256(json.dumps(proof, sort_keys=True).encode()).hexdigest())
    with _studio_lock:
        hit = _studio_cache.get(key)
        if hit and hit["until"] > time.time():
            return {**hit["out"], "expires_in": int(hit["until"] - time.time())}
    svc = _service_token()
    if not svc:
        raise StudioTokenError("this service has no Studio token (restart it from Studio)", 503)
    import requests

    url = os.environ.get("STUDIO_INTERNAL_URL", "http://studio:8010").rstrip("/") + "/studio/internal/gcp-token"
    body = {"identity": identity, "user": user, "consumer": consumer, **proof}
    if scopes:
        body["scopes"] = list(scopes)
    try:
        s = requests.Session()
        s.trust_env = False                   # in-network: never via the egress proxy
        r = s.post(url, json=body, headers={"Authorization": f"Bearer {svc}"}, timeout=30)
    except requests.RequestException as exc:
        raise StudioTokenError(f"Studio is not reachable for a Google token: {type(exc).__name__}", 503) from None
    if r.status_code != 200:
        try:
            msg = r.json().get("detail") or r.text[:200]
        except ValueError:
            msg = r.text[:200]
        raise StudioTokenError(str(msg), r.status_code)
    out = r.json()
    until = time.time() + min(int(out.get("expires_in") or 0) - 60, LOCAL_TTL)
    with _studio_lock:
        _studio_cache[key] = {"out": out, "until": until}
    return {**out, "expires_in": int(until - time.time())}


def studio_credentials(identity: str, user: str | None = None, scopes: list[str] | None = None,
                       consumer: str = ""):
    """google.oauth2.credentials.Credentials whose token comes from Studio and
    refreshes through Studio (the proof in effect now is kept for refreshes)."""
    from google.oauth2.credentials import Credentials

    snap = dict(_acting.get() or {})
    if user and user != snap.get("user"):
        snap = {"user": user}

    def fetch() -> dict[str, Any]:
        tok = _acting.set(snap)
        try:
            return studio_token(identity, None, scopes, consumer)
        finally:
            _acting.reset(tok)

    def handler(request, scopes=None):  # noqa: ARG001 — google-auth refresh_handler signature
        d = fetch()
        return d["access_token"], _expiry(d)

    first = fetch()
    creds = Credentials(token=first["access_token"], expiry=_expiry(first), refresh_handler=handler,
                        quota_project_id=first.get("quota_project") or None)
    creds.bsp_principal = first.get("principal_email", "")          # for "Acting as …" hints
    creds.bsp_default_project = first.get("default_project", "")
    creds.bsp_identity = first.get("identity", identity)
    return creds


def _expiry(d: dict[str, Any]) -> _dt.datetime:
    # google-auth compares against naive UTC
    return _dt.datetime.utcnow() + _dt.timedelta(seconds=max(int(d.get("expires_in") or 0), 1))


def credentials(purpose: str | None = None, scopes: list[str] | None = None, identity: str | None = None,
                user: str | None = None):
    """Credentials for `purpose`. identity=None (default): exactly as before —
    the consumer's assigned identity from the environment. identity=<id or
    name>: act as that identity for `user` (default: the acting user) through
    Studio; StudioTokenError when Studio refuses."""
    if identity:
        return studio_credentials(identity, user=user, scopes=scopes, consumer=_key(purpose) if purpose else "")
    i = info(purpose)
    if i:
        return from_info(i, scopes)
    try:
        import google.auth

        creds, _ = google.auth.default(scopes=scopes or SCOPES)
        return creds
    except Exception:
        return None


def project(purpose: str | None = None, default: str = "to-be-named-soon") -> str:
    """The effective identity's project for `purpose`, else GCP_PROJECT_ID."""
    for var in _chain(purpose):
        p = os.environ.get(var.removesuffix("_JSON") + "_PROJECT", "").strip()
        if p:
            return p
    return os.environ.get("GCP_PROJECT_ID", "").strip() or default


def exec_project(purpose: str | None = None, default: str = "to-be-named-soon") -> str:
    """Execution (job / billing) project for BigQuery consumers
    (GCP_BQ_EXEC_PROJECT, GCP_BQ_WRITE_EXEC_PROJECT, …), else the data project."""
    for var in _chain(purpose):
        p = os.environ.get(var.removesuffix("_JSON") + "_EXEC_PROJECT", "").strip()
        if p:
            return p
    return project(purpose, default)


def data_projects(purpose: str | None = None, default: str = "to-be-named-soon") -> list[str]:
    """All data projects (GCP_BQ_DATA_PROJECTS for bigquery_read), else [project(purpose)]."""
    for var in _chain(purpose):
        raw = os.environ.get(var.removesuffix("_JSON") + "_DATA_PROJECTS", "").strip()
        if raw:
            return [p.strip() for p in raw.split(",") if p.strip()]
    return [project(purpose, default)]


def assigned_name(purpose: str) -> str:
    """Name of the cloud identity `purpose` uses ('' = GCP_SA_JSON / ADC) — for UI hints."""
    try:
        names = json.loads(os.environ.get("GCP_ASSIGNMENTS") or "{}")
    except ValueError:
        return ""
    return str(names.get(_key(purpose)) or "") if isinstance(names, dict) else ""


def principal(purpose: str | None = None) -> str:
    """Who calls Google: the service account's e-mail ('' for a personal sign-in)."""
    try:
        i = info(purpose) or {}
    except ValueError:
        return ""
    return str(i.get("client_email") or "")


def configured(purpose: str | None = None) -> bool:
    return bool(any(os.environ.get(v, "").strip() for v in _chain(purpose)) or os.environ.get("GCP_SA_JSON")
                or os.environ.get("GOOGLE_APPLICATION_CREDENTIALS"))


def sa_base64(purpose: str | None = None) -> str:
    """Base64 key for tools that take it inline (Trino `bigquery.credentials-key`)."""
    i = sa_info(purpose)
    return base64.b64encode(json.dumps(i).encode()).decode() if i else ""


def studio_grant(identity: str) -> dict[str, Any]:
    """Ask Studio for a grant (user, identity, this service) — for unattended
    use such as scheduled canvas runs. Needs the acting user's Studio
    session; Studio checks the access list now and again on every use."""
    a = _acting.get() or {}
    if not a.get("user") or not a.get("session"):
        raise StudioTokenError("saving a Google identity on a connection needs a signed-in user (Studio session)")
    svc = _service_token()
    if not svc:
        raise StudioTokenError("this service has no Studio token (restart it from Studio)", 503)
    import requests

    url = os.environ.get("STUDIO_INTERNAL_URL", "http://studio:8010").rstrip("/") + "/studio/internal/gcp-grant"
    try:
        s = requests.Session()
        s.trust_env = False
        r = s.post(url, json={"identity": identity, "user": a["user"], "session": a["session"]},
                   headers={"Authorization": f"Bearer {svc}"}, timeout=30)
    except requests.RequestException as exc:
        raise StudioTokenError(f"Studio is not reachable: {type(exc).__name__}", 503) from None
    if r.status_code != 200:
        try:
            msg = r.json().get("detail") or r.text[:200]
        except ValueError:
            msg = r.text[:200]
        raise StudioTokenError(str(msg), r.status_code)
    return r.json()


def bigquery_identity(project: str = "", connection: str = "") -> dict[str, str]:
    """Configured Studio identity + execution project for a BigQuery table.

    Notebooks use this when an Atlan definition points at a BigQuery table:

        from bsp_common import gcp
        who = gcp.bigquery_identity(project="da-tsa-dv")
        creds = gcp.credentials("bigquery_read", identity=who["identity"])

    The identity is a Studio cloud identity name/id; the signed-in user must be
    on its access list. Configure it in config/catalog/atlan.yaml or with
    ATLAN_BQ_IDENTITY / ATLAN_BQ_EXEC_PROJECT.
    """
    from . import atlan_config  # noqa: PLC0415

    return atlan_config.lookup(connection=connection, project=project)
