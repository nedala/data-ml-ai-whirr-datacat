"""Cloud identities: named Google Cloud credentials kept in the vault, tested
per capability and assigned per consumer (Vertex AI, BigQuery read / write,
Trino, Cloud Storage, MLflow, Artifact Registry, Cloud Run, workspace tools). Both key types gcloud / Google produce are accepted:

  * service_account  — an IAM service-account key file;
  * authorized_user  — a personal sign-in (`gcloud auth application-default
    login` → application_default_credentials.json, or Studio's own "Sign in
    with Google").

Consumers (who uses which identity) are declared in gcp_consumers.yaml.

Vault layout::

    gcp_identities: {<id>: {name, type, principal, project, json, added_by,
                            added_at, last_test: {...}, known_projects,
                            access: {mode: owner|list|everyone, users}}}
    gcp_assignments: {<consumer key>: <id> | ""}   ("" = use the fallback consumer)
    meta.gcp_applied: {<ENV_NAME>: sha256 prefix of the value last applied}

The JSON never leaves Studio except through the process environment of a
compose start for each consumer's ASSIGNED identity (see env_vars()); every
API response carries metadata only. Acting as any other identity (the
principal switch) goes through mint() + the per-identity access list.
"""

from __future__ import annotations

import base64
import binascii
import datetime as dt
import hashlib
import json
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import credentials as _credentials  # noqa: F401 — its vault hook (seed GCP_SA_JSON from .env) must run first
import catalog
import settings
from vault import store

CONSUMERS_FILE = Path(__file__).resolve().parent / "gcp_consumers.yaml"
ALIASES = {"bigquery": "bigquery_read", "bq": "bigquery_read", "trino": "trino_bigquery", "mlflow": "mlflow_artifacts"}
_registry_cache: dict[str, Any] = {}


def registry() -> dict[str, Any]:
    """gcp_consumers.yaml: {groups: [...], consumers: [...]} (cached by mtime)."""
    import yaml

    stamp = CONSUMERS_FILE.stat().st_mtime
    if _registry_cache.get("stamp") != stamp:
        data = yaml.safe_load(CONSUMERS_FILE.read_text(encoding="utf-8")) or {}
        consumers = [dict(c) for c in data.get("consumers") or []]
        for c in consumers:
            c.setdefault("fallback", "")
            c.setdefault("checks", [])
            stem = c["env"].removesuffix("_JSON")
            c["project_env"] = stem + "_PROJECT"
            c["exec_env"] = stem + "_EXEC_PROJECT" if c.get("execution_project") else ""
            c["data_projects_env"] = stem + "_DATA_PROJECTS" if c.get("multi_project") else ""
        _registry_cache.update(stamp=stamp, groups=data.get("groups") or [], consumers=consumers)
    return {"groups": _registry_cache["groups"], "consumers": _registry_cache["consumers"]}


def consumers() -> dict[str, dict[str, Any]]:
    return {c["key"]: c for c in registry()["consumers"]}


def consumer_keys() -> list[str]:
    return [c["key"] for c in registry()["consumers"]]


TYPES = ("service_account", "authorized_user")
REQUIRED = {
    "service_account": ("client_email", "private_key", "private_key_id", "client_id"),
    "authorized_user": ("client_id", "client_secret", "refresh_token"),
}
TOKEN_HOSTS = ("oauth2.googleapis.com", "accounts.google.com", "www.googleapis.com")
DEFAULT_TOKEN_URI = "https://oauth2.googleapis.com/token"
CLOUD_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
MAX_JSON = 64 * 1024
CHECK_TIMEOUT = 10
MIGRATED_NAME = "default service account"
VERTEX_MODEL = "gemini-2.5-flash"


class IdentityError(ValueError):
    """Bad input (mapped to HTTP 400)."""


def _now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")


def setting(name: str, default: str = "") -> str:
    """Non-secret appliance settings (GCP_PROJECT_ID, GCP_REGION): Studio's
    env, then the project .env."""
    return os.environ.get(name, "").strip() or settings.dotenv().get(name, "").strip() or default


# ── parsing / validation ─────────────────────────────────────────────────
def parse_json(text: str) -> dict[str, Any]:
    """Validate a key file (raw JSON or its base64). Returns the parsed dict."""
    raw = (text or "").strip()
    if not raw:
        raise IdentityError("Paste the JSON key file (or upload it)")
    if len(raw) > MAX_JSON:
        raise IdentityError("That is not a Google credential file (larger than 64 KB)")
    if not raw.startswith(("{", "[")):
        try:
            raw = base64.b64decode(raw, validate=False).decode("utf-8").strip()
        except (binascii.Error, UnicodeDecodeError, ValueError):
            raise IdentityError("Not JSON (and not base64 of JSON)") from None
    try:
        info = json.loads(raw)
    except ValueError as exc:
        raise IdentityError(f"Not valid JSON: {exc.msg} (line {exc.lineno})") from None
    if not isinstance(info, dict):
        raise IdentityError("The JSON must be an object")
    kind = info.get("type")
    if kind not in TYPES:
        hint = {"external_account": "workload identity federation configs are not supported",
                "impersonated_service_account": "impersonation configs are not supported"}.get(str(kind), "")
        raise IdentityError(f"Unsupported credential type {kind!r} — use a service-account key "
                            f"(type service_account) or a gcloud sign-in (type authorized_user)"
                            + (f"; {hint}" if hint else ""))
    missing = [k for k in REQUIRED[kind] if not str(info.get(k) or "").strip()]
    if missing:
        raise IdentityError(f"{kind} JSON is missing {', '.join(missing)}")
    if kind == "service_account" and "PRIVATE KEY-----" not in str(info["private_key"]):
        raise IdentityError("private_key is not a PEM private key")
    uri = str(info.get("token_uri") or DEFAULT_TOKEN_URI)
    m = re.fullmatch(r"https://([a-z0-9.-]+)(/[\w/.-]*)?", uri)
    if not m or m.group(1) not in TOKEN_HOSTS:
        raise IdentityError("token_uri must be a Google OAuth endpoint (https://oauth2.googleapis.com/token)")
    return info


def default_project(info: dict[str, Any]) -> str:
    return str(info.get("project_id") or info.get("quota_project_id") or setting("GCP_PROJECT_ID"))


def _slug(name: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")[:40]
    return s or "identity"


def _clean_name(name: str) -> str:
    name = " ".join((name or "").split())
    if not name:
        raise IdentityError("Give the identity a name")
    if len(name) > 64 or not re.fullmatch(r"[\w .@()+\-]+", name):
        raise IdentityError("Name: up to 64 letters, digits, spaces and . @ ( ) + - _")
    return name


def _clean_project(project: str) -> str:
    project = (project or "").strip()
    if project and not re.fullmatch(r"[a-z][a-z0-9.:-]{3,62}", project):
        raise IdentityError("Project: a Google Cloud project id such as my-project-123")
    return project


# ── metadata views ───────────────────────────────────────────────────────
PUBLIC_FIELDS = ("name", "type", "principal", "project", "added_by", "added_at", "last_test", "source", "known_projects")


def identities() -> dict[str, dict[str, Any]]:
    return store.get("gcp_identities", {}) or {}


def _norm(v: Any) -> dict[str, Any]:
    if isinstance(v, dict):
        dp = v.get("data_projects") or []
        return {"identity": str(v.get("identity") or ""), "project": str(v.get("project") or ""),
                "exec_project": str(v.get("exec_project") or ""),
                "data_projects": [str(x) for x in dp if str(x).strip()] if isinstance(dp, list) else []}
    return {"identity": str(v or ""), "project": "", "exec_project": "", "data_projects": []}


def assignment_details(data: dict[str, Any] | None = None) -> dict[str, dict[str, Any]]:
    """{consumer: {identity, project, exec_project, data_projects}} ("" = default)."""
    raw = (data.get("gcp_assignments") if data is not None else store.get("gcp_assignments")) or {}
    return {k: _norm(raw.get(k)) for k in consumer_keys()}


def assignments(data: dict[str, Any] | None = None) -> dict[str, str]:
    return {k: v["identity"] for k, v in assignment_details(data).items()}


def effective_projects(key: str, data: dict[str, Any]) -> dict[str, Any]:
    """{project, exec_project, data_projects} the consumer really uses: the
    projects set on the assignment it resolves through (own or inherited),
    else the identity's default project."""
    ident_id, via = effective(key, data)
    rec = (data.get("gcp_identities", {}) or {}).get(ident_id) or {}
    det = assignment_details(data).get(via) or _norm("")
    c = consumers()[key]
    data_projects = det["data_projects"] if c.get("multi_project") else []
    project = (data_projects[0] if data_projects else "") or det["project"] or rec.get("project") or ""
    if c.get("multi_project"):
        data_projects = data_projects or ([project] if project else [])
    exec_project = (det["exec_project"] or project) if c.get("execution_project") else ""
    return {"project": project, "exec_project": exec_project, "data_projects": data_projects}


def effective(key: str, data: dict[str, Any]) -> tuple[str, str]:
    """(identity id, consumer key it came from) after the fallback chain;
    ("", "") = none → GCP_SA_JSON / ADC. Service-account-only consumers skip
    personal sign-ins."""
    cons, ids, asg = consumers(), data.get("gcp_identities", {}) or {}, assignments(data)
    sa_only = bool(cons[key].get("service_account_only"))
    seen: set[str] = set()
    k = key
    while k and k not in seen:
        seen.add(k)
        rec = ids.get(asg.get(k, ""))
        if rec and (not sa_only or rec.get("type") == "service_account"):
            return asg[k], k
        k = cons.get(k, {}).get("fallback", "")
    return "", ""


def public(ident_id: str, rec: dict[str, Any], asg: dict[str, str]) -> dict[str, Any]:
    out = {k: rec.get(k) for k in PUBLIC_FIELDS}
    out["id"] = ident_id
    out["access"] = access_of(rec)
    out["assigned"] = [k for k in consumer_keys() if asg.get(k) == ident_id]
    return out


def consumer_view(data: dict[str, Any]) -> list[dict[str, Any]]:
    ids, asg = data.get("gcp_identities", {}) or {}, assignments(data)
    out = []
    det = assignment_details(data)
    for c in registry()["consumers"]:
        eff, via = effective(c["key"], data)
        out.append({k: c.get(k) for k in ("key", "group", "title", "description", "env", "fallback", "services",
                                           "checks", "future")}
                   | {"service_account_only": bool(c.get("service_account_only")), "assigned": asg[c["key"]],
                      "execution_project": bool(c.get("execution_project")), "multi_project": bool(c.get("multi_project")),
                      "settings": det[c["key"]], "projects": effective_projects(c["key"], data),
                      "effective": eff, "effective_name": (ids.get(eff) or {}).get("name", ""), "via": via})
    return out


def listing() -> dict[str, Any]:
    migrate(store)
    data = store.snapshot()
    asg = assignments(data)
    ids = data.get("gcp_identities", {}) or {}
    shared = ((data.get("services", {}) or {}).get(catalog.SHARED, {}) or {}).get("GCP_SA_JSON") or ""
    return {"identities": sorted((public(i, r, asg) for i, r in ids.items()), key=lambda r: r["name"].lower()),
            "assignments": asg, "groups": registry()["groups"], "consumers": consumer_view(data),
            "shared_key_set": bool(shared.strip()), "checks": [{"id": c[0], "capability": c[1], "label": c[2]} for c in CHECKS],
            "region": setting("GCP_REGION", "us-central1"), "default_project": setting("GCP_PROJECT_ID")}


# ── CRUD ─────────────────────────────────────────────────────────────────
def add(name: str, text: str, user: str, project: str = "", source: str = "upload",
        probe: bool = True) -> dict[str, Any]:
    name = _clean_name(name)
    info = parse_json(text)
    project = _clean_project(project) or default_project(info)
    principal = info.get("client_email", "") if info["type"] == "service_account" else ""
    if not principal and probe:
        try:
            principal = _token(info)[1]
        except CheckFailed:
            principal = ""          # unknown until a successful Test
    rec = {"name": name, "type": info["type"], "principal": principal, "project": project,
           "json": json.dumps(info), "added_by": user, "added_at": _now(), "last_test": None, "source": source}
    holder: dict[str, str] = {}

    def write(data: dict[str, Any]) -> None:
        ids = data.setdefault("gcp_identities", {})
        if any(r.get("name", "").lower() == name.lower() for r in ids.values()):
            raise IdentityError(f"An identity named {name!r} already exists")
        base = _slug(name)
        ident_id, n = base, 2
        while ident_id in ids:
            ident_id, n = f"{base}-{n}", n + 1
        ids[ident_id] = rec
        holder["id"] = ident_id

    store.mutate(write)
    return public(holder["id"], rec, assignments())


def delete(ident_id: str, force: bool = False) -> dict[str, Any]:
    ids = identities()
    if ident_id not in ids:
        raise KeyError(ident_id)
    used = [k for k, v in assignments().items() if v == ident_id]
    if used and not force:
        titles = consumers()
        raise IdentityError("Assigned to " + ", ".join(titles[k]["title"] for k in used)
                            + " — change the assignments first (or delete with force)")

    def write(data: dict[str, Any]) -> None:
        data.get("gcp_identities", {}).pop(ident_id, None)
        asg = data.setdefault("gcp_assignments", {})
        for k in used:
            asg[k] = ""

    store.mutate(write)
    return {"id": ident_id, "name": ids[ident_id]["name"], "unassigned": used}


def set_assignments(values: dict[str, Any]) -> dict[str, str]:
    """values: {consumer: identity id} or {consumer: {identity, project,
    exec_project, data_projects}}."""
    ids, cons = identities(), consumers()
    values = {ALIASES.get(k, k): v for k, v in values.items()}
    unknown = set(values) - set(cons)
    if unknown:
        raise IdentityError(f"Unknown consumer(s): {', '.join(sorted(unknown))}")
    details: dict[str, dict[str, Any]] = {}
    for k, v in values.items():
        d = _norm(v)
        if isinstance(v, dict) and isinstance(v.get("data_projects"), str):
            d["data_projects"] = [x.strip() for x in v["data_projects"].replace(";", ",").split(",") if x.strip()]
        d["project"] = _clean_project(d["project"])
        d["exec_project"] = _clean_project(d["exec_project"]) if cons[k].get("execution_project") else ""
        d["data_projects"] = list(dict.fromkeys(_clean_project(x) for x in d["data_projects"])) if cons[k].get("multi_project") else []
        if len(d["data_projects"]) > 20:
            raise IdentityError("At most 20 data projects")
        details[k] = d
    for k, v in ((k, d["identity"]) for k, d in details.items()):
        if v and v not in ids:
            raise IdentityError(f"{cons[k]['title']}: no identity {v!r}")
        if v and cons[k].get("service_account_only") and ids[v].get("type") != "service_account":
            raise IdentityError(f"{cons[k]['title']} needs a service-account identity — "
                                f"{ids[v]['name']} is a personal sign-in")

    def write(data: dict[str, Any]) -> None:
        asg = data.setdefault("gcp_assignments", {})
        for k, d in details.items():
            asg[k] = d if (d["project"] or d["exec_project"] or d["data_projects"]) else d["identity"]

    store.mutate(write)
    return assignments()


def _clean_known(items: Any) -> list[dict[str, str]]:
    """Known projects: [{id, name}] from a list of ids / {id, name} / "id  label" lines."""
    if isinstance(items, str):
        items = [x for x in items.replace(",", "\n").splitlines() if x.strip()]
    out: dict[str, dict[str, str]] = {}
    for it in items or []:
        if isinstance(it, dict):
            pid, label = str(it.get("id") or "").strip(), str(it.get("name") or "").strip()
        else:
            parts = str(it).split(None, 1)
            pid, label = (parts[0] if parts else ""), (parts[1].strip(" -—()") if len(parts) > 1 else "")
        if not pid:
            continue
        out[_clean_project(pid)] = {"id": pid, "name": label[:80] or pid}
    if len(out) > 100:
        raise IdentityError("At most 100 known projects")
    return list(out.values())


def update_identity(ident_id: str, project: str | None = None, name: str | None = None,
                    known_projects: Any = None) -> dict[str, Any]:
    if ident_id not in identities():
        raise KeyError(ident_id)
    proj = _clean_project(project) if project is not None else None
    new_name = _clean_name(name) if name is not None else None
    known = _clean_known(known_projects) if known_projects is not None else None

    def write(data: dict[str, Any]) -> None:
        ids = data["gcp_identities"]
        if new_name and any(i != ident_id and r.get("name", "").lower() == new_name.lower() for i, r in ids.items()):
            raise IdentityError(f"An identity named {new_name!r} already exists")
        rec = ids[ident_id]
        if proj is not None:
            rec["project"] = proj or default_project(json.loads(rec["json"]))
        if new_name:
            rec["name"] = new_name
        if known is not None:
            rec["known_projects"] = known

    store.mutate(write)
    return public(ident_id, identities()[ident_id], assignments())


def list_projects(ident_id: str) -> dict[str, Any]:
    """Projects the identity can see (Resource Manager v3 projects:search).
    {available: False, reason} when the API is disabled / denied."""
    rec = identities().get(ident_id)
    if not rec:
        raise KeyError(ident_id)
    res = _search_projects(rec)
    known = rec.get("known_projects") or []
    seen = {p["id"] for p in res["projects"]}
    res["projects"] = res["projects"] + [dict(p, known=True) for p in known if p["id"] not in seen]
    res["known_projects"] = known
    return res


def _search_projects(rec: dict[str, Any]) -> dict[str, Any]:
    info = json.loads(rec["json"])
    try:
        token, _who = _token(info)
    except CheckFailed as exc:
        return {"available": False, "reason": exc.detail, "projects": []}
    hdr = {"Authorization": f"Bearer {token}"}
    out: list[dict[str, str]] = []
    page = ""
    try:
        for _ in range(5):
            st, body = _call("GET", "https://cloudresourcemanager.googleapis.com/v3/projects:search", headers=hdr,
                             params={"pageSize": 300, **({"pageToken": page} if page else {})})
            if st != 200:
                msg, reason, _ = _error_parts(body)
                why = ("the Cloud Resource Manager API is disabled for this identity's project"
                       if "SERVICE_DISABLED" in reason or "has not been used" in msg else f"HTTP {st}: {msg[:160]}")
                return {"available": False, "reason": why, "projects": out}
            for p in (body or {}).get("projects") or []:
                if p.get("state", "ACTIVE") == "ACTIVE" and p.get("projectId"):
                    out.append({"id": p["projectId"], "name": p.get("displayName") or p["projectId"]})
            page = (body or {}).get("nextPageToken") or ""
            if not page:
                break
    except CheckFailed as exc:
        return {"available": False, "reason": exc.detail, "projects": out}
    return {"available": True, "projects": sorted(out, key=lambda p: p["id"])}


# ── migration of the legacy single key ───────────────────────────────────
def migrate(vault: Any) -> list[str]:
    """Shared GCP_SA_JSON set and no identities yet → import it once as
    "default service account", assigned to every consumer (nothing changes
    until an admin picks another identity)."""
    data = vault.snapshot()
    meta = data.get("meta", {}) or {}
    if meta.get("gcp_identities_migrated"):
        return []
    if data.get("gcp_identities"):
        vault.mutate(lambda d: d.setdefault("meta", {}).__setitem__("gcp_identities_migrated", _now()))
        return []
    raw = ((data.get("services", {}) or {}).get(catalog.SHARED, {}) or {}).get("GCP_SA_JSON") or ""
    if not raw.strip():
        return []
    try:
        info = parse_json(raw)
    except IdentityError as exc:
        return [f"GCP_SA_JSON was not imported as a cloud identity: {exc}"]
    rec = {"name": MIGRATED_NAME, "type": info["type"], "principal": info.get("client_email", ""),
           "project": default_project(info), "json": json.dumps(info), "added_by": "studio",
           "added_at": _now(), "last_test": None, "source": "GCP_SA_JSON"}

    def write(d: dict[str, Any]) -> None:
        if d.get("gcp_identities"):
            return
        d["gcp_identities"] = {"default-service-account": rec}
        d["gcp_assignments"] = {k: "default-service-account" for k in consumer_keys()}
        d.setdefault("meta", {})["gcp_identities_migrated"] = _now()

    vault.mutate(write)
    import audit
    audit.record("gcp_identity.migrate", f"{MIGRATED_NAME} ({rec['principal']}) from GCP_SA_JSON", user="studio")
    return ["The GCP_SA_JSON key is now the cloud identity “default service account” (Studio → Cloud identities)"]


store.on_open.append(migrate)


# ── injection ────────────────────────────────────────────────────────────
def env_names() -> list[str]:
    out = []
    for c in registry()["consumers"]:
        out += [c["env"], c["project_env"]] + [e for e in (c["exec_env"], c["data_projects_env"]) if e]
    return out + ["GCP_IDENTITIES", "GCP_ASSIGNMENTS", "GCP_SA_JSON"]


def env_vars(data: dict[str, Any], shared_sa: str) -> dict[str, str]:
    """Process-env additions for every compose start (secrets: never in .env).
    Each consumer's env carries its EFFECTIVE identity (fallback resolved);
    GCP_ASSIGNMENTS {consumer: identity name} is non-secret, for "Google
    identity: <name>" hints in the apps."""
    ids = data.get("gcp_identities", {}) or {}
    out: dict[str, str] = {}
    names: dict[str, str] = {}
    for c in registry()["consumers"]:
        eff, _via = effective(c["key"], data)
        rec = ids.get(eff)
        pr = effective_projects(c["key"], data) if rec else {"project": "", "exec_project": "", "data_projects": []}
        out[c["env"]] = rec["json"] if rec else ""
        out[c["project_env"]] = pr["project"]
        if c["exec_env"]:
            out[c["exec_env"]] = pr["exec_project"]
        if c["data_projects_env"]:
            out[c["data_projects_env"]] = ",".join(pr["data_projects"])
        names[c["key"]] = rec["name"] if rec else ""
    # names + metadata only: workloads switch principal through Studio's token
    # endpoint (access list checked there) and never hold another identity's key
    out["GCP_IDENTITIES"] = json.dumps({r["name"]: {"id": i, "type": r.get("type", ""), "principal": r.get("principal", ""),
                                                    "project": r.get("project", "")} for i, r in ids.items()}) if ids else ""
    out["GCP_ASSIGNMENTS"] = json.dumps(names) if ids else ""
    ws = ids.get(effective("workspace", data)[0]) if "workspace" in consumers() else None
    out["GCP_SA_JSON"] = ws["json"] if ws and ws.get("type") == "service_account" else (shared_sa or "")
    return out


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:16] if value else ""


def fingerprints(env: dict[str, str]) -> dict[str, str]:
    return {k: _digest(env.get(k, "")) for k in env_names()}


def changed_vars(env: dict[str, str]) -> list[str]:
    applied = (store.get("meta.gcp_applied") or {})
    return [k for k, v in fingerprints(env).items() if applied.get(k, "") != v]


def mark_applied(env: dict[str, str]) -> None:
    store.mutate(lambda d: d.setdefault("meta", {}).__setitem__("gcp_applied", fingerprints(env)))


# ── live tests ───────────────────────────────────────────────────────────
class CheckFailed(Exception):
    def __init__(self, status: int | None, detail: str, hint: str = "", permission: str = ""):
        super().__init__(detail)
        self.status, self.detail, self.hint, self.permission = status, detail, hint, permission


def _http(method: str, url: str, *, headers: dict | None = None, data: Any = None, json_body: Any = None,
          params: dict | None = None, timeout: float = CHECK_TIMEOUT, raw: bytes | None = None) -> tuple[int, Any]:
    """One HTTPS call through the egress proxy (requests honours HTTPS_PROXY
    and REQUESTS_CA_BUNDLE). Returns (status, parsed JSON or text). Tests
    replace this function."""
    import requests

    r = requests.request(method, url, headers=headers, data=raw if raw is not None else data, json=json_body,
                         params=params, timeout=timeout)
    try:
        return r.status_code, r.json()
    except ValueError:
        return r.status_code, r.text[:500]


def _call(method: str, url: str, **kw) -> tuple[int, Any]:
    try:
        return _http(method, url, **kw)
    except Exception as exc:  # noqa: BLE001 — timeouts, proxy refusals, TLS
        name = type(exc).__name__
        if "Timeout" in name:
            raise CheckFailed(None, f"no answer within {CHECK_TIMEOUT} s",
                              "Check that *.googleapis.com is allowed by the egress policy (Studio → Egress)") from None
        raise CheckFailed(None, f"{name}: {str(exc)[:160]}",
                          "Check the egress policy (Studio → Egress) allows *.googleapis.com") from None


def _error_parts(body: Any) -> tuple[str, str, str]:
    """(message, reason/status, permission) from a Google error body."""
    if isinstance(body, dict):
        err = body.get("error")
        if isinstance(err, dict):
            msg = str(err.get("message") or "")
            reasons = [str(d.get("reason")) for d in err.get("details") or [] if isinstance(d, dict) and d.get("reason")]
            reasons += [str(e.get("reason")) for e in err.get("errors") or [] if isinstance(e, dict) and e.get("reason")]
            reason = ",".join(reasons) or str(err.get("status") or "")
        else:
            msg, reason = str(body.get("error_description") or err or ""), str(err or "")
    else:
        msg, reason = str(body or "")[:300], ""
    perm = ""
    if m := re.search(r"[Pp]ermissions?\s+'?([a-z]+\.[a-zA-Z]+\.[a-zA-Z]+)'?", msg):
        perm = m.group(1)
    elif m := re.search(r"\b([a-z]+\.(?:datasets|jobs|buckets|objects|tables|repositories|services)\.[a-zA-Z]+)\b", msg):
        perm = m.group(1)
    return msg[:400], reason, perm


def _token(info: dict[str, Any]) -> tuple[str, str]:
    """(access token, principal email)."""
    uri = str(info.get("token_uri") or DEFAULT_TOKEN_URI)
    if info["type"] == "service_account":
        from google.auth import crypt, jwt as gjwt

        try:
            signer = crypt.RSASigner.from_service_account_info(info)
        except Exception as exc:  # noqa: BLE001
            raise CheckFailed(None, f"the private key cannot be loaded ({type(exc).__name__})",
                              "The key file is damaged — download a new key") from None
        now = int(time.time())
        assertion = gjwt.encode(signer, {"iss": info["client_email"], "scope": CLOUD_SCOPE, "aud": uri,
                                         "iat": now, "exp": now + 3600})
        status, body = _call("POST", uri, data={"grant_type": "urn:ietf:params:oauth:grant-type:jwt-bearer",
                                                "assertion": assertion.decode() if isinstance(assertion, bytes) else assertion})
    else:
        status, body = _call("POST", uri, data={"grant_type": "refresh_token", "client_id": info["client_id"],
                                                "client_secret": info["client_secret"],
                                                "refresh_token": info["refresh_token"]})
    token = body.get("access_token") if isinstance(body, dict) else None
    if status != 200 or not token:
        msg, reason, _ = _error_parts(body)
        if "invalid_grant" in (reason + msg):
            hint = ("The sign-in expired or was revoked (password change, session-length policy or an admin "
                    "block) — sign in with Google again and replace this identity"
                    if info["type"] == "authorized_user" else
                    "The key was deleted or disabled in IAM (or the clock is wrong) — create a new key")
        elif "invalid_client" in (reason + msg) or "unauthorized_client" in (reason + msg):
            hint = "client_id / client_secret were rejected — produce the file again with gcloud"
        else:
            hint = "Google refused the credential"
        raise CheckFailed(status, f"token refresh failed: {msg or reason or status}", hint)
    if info["type"] == "service_account":
        return token, str(info["client_email"])
    who = ""
    try:
        st, ti = _call("GET", "https://oauth2.googleapis.com/tokeninfo", params={"access_token": token})
        if st == 200 and isinstance(ti, dict):
            who = str(ti.get("email") or "")
        if not who:
            st, ui = _call("GET", "https://openidconnect.googleapis.com/v1/userinfo",
                           headers={"Authorization": f"Bearer {token}"})
            if st == 200 and isinstance(ui, dict):
                who = str(ui.get("email") or "")
    except CheckFailed:
        pass
    return token, who


# check id → (capability chip, label, role hint for a 403)
CHECKS = [
    ("bq_list", "bigquery", "BigQuery · list datasets"),
    ("bq_query", "bigquery", "BigQuery · dry-run query"),
    ("bq_write", "bq_write", "BigQuery · write"),
    ("gcs_buckets", "gcs", "Cloud Storage · list buckets"),
    ("gcs_objects", "gcs", "Cloud Storage · read bucket"),
    ("gcs_write", "gcs_write", "Cloud Storage · write"),
    ("vertex", "vertex", "Vertex AI · generate"),
    ("artifact_registry", "artifact_registry", "Artifact Registry · list repositories"),
    ("cloud_run", "cloud_run", "Cloud Run · list services"),
]
CHECK_INFO = {c[0]: c for c in CHECKS}
CAPABILITY_TITLE = {"token": "Sign-in", "bigquery": "BigQuery", "bq_write": "BQ write", "gcs": "Cloud Storage",
                    "gcs_write": "GCS write", "vertex": "Vertex AI", "artifact_registry": "Artifact Registry",
                    "cloud_run": "Cloud Run"}
ROLE_HINT = {
    "bq_list": ("roles/bigquery.dataViewer", "roles/bigquery.dataViewer (or roles/bigquery.metadataViewer) on project {project} or the datasets"),
    "bq_query": ("roles/bigquery.jobUser", "roles/bigquery.jobUser on the execution project {exec_project} (to run query "
                 "jobs) — or pick an execution project where it already has it"),
    "bq_write": ("roles/bigquery.dataEditor", "roles/bigquery.dataEditor on {dataset_or_project} (create / write tables)"),
    "gcs_buckets": ("roles/storage.bucketViewer", "storage.buckets.list on project {project} (e.g. roles/storage.bucketViewer) — "
                    "reading data only needs roles/storage.objectViewer on a bucket: enter a bucket name and test again"),
    "gcs_objects": ("roles/storage.objectViewer", "roles/storage.objectViewer on gs://{bucket}"),
    "gcs_write": ("roles/storage.objectCreator", "roles/storage.objectCreator (write) and roles/storage.objectViewer on {bucket_or_project}"),
    "vertex": ("roles/aiplatform.user", "roles/aiplatform.user on project {project}"),
    "artifact_registry": ("roles/artifactregistry.reader", "roles/artifactregistry.reader on project {project} "
                          "(pushing images / packages needs roles/artifactregistry.writer)"),
    "cloud_run": ("roles/run.viewer", "roles/run.viewer on project {project} (deploying needs roles/run.developer "
                  "and roles/iam.serviceAccountUser on the runtime service account)"),
}
API_NAME = {"bq_list": "BigQuery API", "bq_query": "BigQuery API", "bq_write": "BigQuery API",
            "gcs_buckets": "Cloud Storage API", "gcs_objects": "Cloud Storage API", "gcs_write": "Cloud Storage API",
            "vertex": "Vertex AI API (aiplatform.googleapis.com)",
            "artifact_registry": "Artifact Registry API (artifactregistry.googleapis.com)",
            "cloud_run": "Cloud Run Admin API (run.googleapis.com)", "iam_test": "Cloud Resource Manager API"}


def _api_error(check: str, status: int, body: Any, ctx: dict[str, str]) -> CheckFailed:
    msg, reason, perm = _error_parts(body)
    blob = f"{reason} {msg}"
    fmt = ctx | {"exec_project": ctx.get("exec_project") or ctx["project"], "dataset_or_project": f"dataset {ctx['project']}.{ctx['dataset']}" if ctx["dataset"] else f"project {ctx['project']}",
                 "bucket_or_project": f"gs://{ctx['bucket']}" if ctx["bucket"] else f"project {ctx['project']}"}
    if status == 403 and ("SERVICE_DISABLED" in blob or "has not been used" in blob or "accessNotConfigured" in blob):
        hint = f"Enable the {API_NAME.get(check, 'API')} in project {ctx['project']}"
    elif status == 403 and ("USER_PROJECT_DENIED" in blob or "serviceusage" in blob):
        hint = f"The quota project needs roles/serviceusage.serviceUsageConsumer for {ctx['principal'] or 'this identity'}"
    elif status == 403:
        role, text = ROLE_HINT[check]
        hint = f"Grant {ctx['principal'] or 'this identity'} " + text.format(**fmt)
        perm = perm or role
    elif status == 401:
        hint = "The access token was rejected — Test again; if it persists, replace the identity"
    elif status == 404:
        hint = {"vertex": f"{VERTEX_MODEL} is not available in {ctx['region']} for project {ctx['project']} (or the project id is wrong)",
                "gcs_objects": f"Bucket gs://{ctx['bucket']} does not exist (or is not visible)",
                "gcs_write": f"Bucket gs://{ctx['bucket']} does not exist (or is not visible)",
                "bq_write": f"Dataset {ctx['project']}.{ctx['dataset']} does not exist"}.get(
            check, f"Project {ctx['project']} was not found (or is not visible to this identity)")
    elif status == 429:
        hint = "Quota exceeded / rate limited — try again in a minute"
    elif status == 400 and "billing" in msg.lower():
        hint = f"Enable billing on project {ctx['project']}"
    else:
        hint = ""
    return CheckFailed(status, msg or reason or f"HTTP {status}", hint, perm)


def _project_permissions(hdr: dict, project: str, perms: list[str], check: str, ctx: dict) -> list[str]:
    """Missing project-level permissions (Resource Manager testIamPermissions)."""
    st, body = _call("POST", f"https://cloudresourcemanager.googleapis.com/v1/projects/{project}:testIamPermissions",
                     headers=hdr, json_body={"permissions": perms})
    if st != 200:
        exc = _api_error(check, st, body, ctx)
        if st == 403 and ("SERVICE_DISABLED" in exc.detail or "has not been used" in exc.detail):
            exc.hint = (f"Enable the Cloud Resource Manager API (used for this permission check) — or set a "
                        f"{'sandbox dataset' if check == 'bq_write' else 'bucket'} to test a real write")
        raise exc
    granted = set((body or {}).get("permissions") or [])
    return [p for p in perms if p not in granted]


def _run_check(check: str, token: str, info: dict[str, Any], ctx: dict[str, str]) -> str:
    hdr = {"Authorization": f"Bearer {token}"}
    if info.get("quota_project_id"):
        hdr["x-goog-user-project"] = str(info["quota_project_id"])
    project, region, bucket, dataset = ctx["project"], ctx["region"], ctx["bucket"], ctx["dataset"]
    exec_project = ctx.get("exec_project") or project
    bq = f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}"
    if check == "bq_list":
        parts = []
        for dp in ctx.get("data_projects") or [project]:
            st, body = _call("GET", f"https://bigquery.googleapis.com/bigquery/v2/projects/{dp}/datasets",
                             headers=hdr, params={"maxResults": 50})
            if st != 200:
                raise _api_error(check, st, body, ctx | {"project": dp})
            n = len((body or {}).get("datasets") or [])
            parts.append(f"{n}{'+' if (body or {}).get('nextPageToken') else ''} in {dp}")
        return "dataset(s) visible: " + ", ".join(parts)
    elif check == "bq_query":
        st, body = _call("POST", f"https://bigquery.googleapis.com/bigquery/v2/projects/{exec_project}/jobs", headers=hdr,
                         json_body={"configuration": {"dryRun": True, "query": {"query": "select 1", "useLegacySql": False}}})
        if st == 200:
            return f"dry-run of select 1 accepted in execution project {exec_project} (query jobs allowed)"
    elif check == "bq_write":
        if dataset:
            table = f"bsp_write_probe_{int(time.time())}"
            st, body = _call("POST", f"{bq}/datasets/{dataset}/tables", headers=hdr, json_body={
                "tableReference": {"projectId": project, "datasetId": dataset, "tableId": table},
                "schema": {"fields": [{"name": "ok", "type": "BOOL"}]},
                "expirationTime": str(int((time.time() + 3600) * 1000))})
            if st == 200:
                _call("DELETE", f"{bq}/datasets/{dataset}/tables/{table}", headers=hdr)
                return f"created and dropped a probe table in {project}.{dataset}"
        else:
            missing = [f"{p} on {project}" for p in _project_permissions(
                hdr, project, ["bigquery.tables.create", "bigquery.tables.updateData"], check, ctx)]
            missing += [f"{p} on {exec_project}" for p in _project_permissions(
                hdr, exec_project, ["bigquery.jobs.create"], check, ctx)]
            if missing:
                raise CheckFailed(403, f"missing: {', '.join(missing)}",
                                  f"Grant {ctx['principal'] or 'this identity'} roles/bigquery.dataEditor on {project} (tables) "
                                  f"and roles/bigquery.jobUser on {exec_project} (jobs) — or a dataset-level grant: set "
                                  "a sandbox dataset and test again for a real write", missing[0].split(" ")[0])
            return f"tables.create / updateData on {project}, jobs.create on {exec_project} — granted"
    elif check == "gcs_buckets":
        st, body = _call("GET", "https://storage.googleapis.com/storage/v1/b", headers=hdr,
                         params={"project": project, "maxResults": 50, "fields": "items/name,nextPageToken"})
        if st == 200:
            names = [b.get("name") for b in (body or {}).get("items") or []]
            shown = ", ".join(names[:3]) + ("…" if len(names) > 3 else "")
            return f"{len(names)} bucket(s) visible in {project}" + (f": {shown}" if names else "")
    elif check == "gcs_objects":
        st, body = _call("GET", f"https://storage.googleapis.com/storage/v1/b/{bucket}/o", headers=hdr,
                         params={"maxResults": 5, "fields": "items/name"})
        if st == 200:
            return f"gs://{bucket} readable ({len((body or {}).get('items') or [])} object(s) sampled)"
    elif check == "gcs_write":
        if bucket:
            name = f".bsp-write-probe-{int(time.time())}"
            st, body = _call("POST", f"https://storage.googleapis.com/upload/storage/v1/b/{bucket}/o",
                             headers=hdr | {"Content-Type": "text/plain"}, params={"uploadType": "media", "name": name},
                             raw=b"ok")
            if st == 200:
                _call("DELETE", f"https://storage.googleapis.com/storage/v1/b/{bucket}/o/{name}", headers=hdr)
                return f"wrote and deleted a probe object in gs://{bucket}"
        else:
            missing = _project_permissions(hdr, project, ["storage.objects.create", "storage.objects.get"], check, ctx)
            if missing:
                raise CheckFailed(403, f"missing on project {project}: {', '.join(missing)}",
                                  f"Grant {ctx['principal'] or 'this identity'} roles/storage.objectCreator + "
                                  "roles/storage.objectViewer (bucket-level grants: enter the bucket and test again)",
                                  missing[0])
            return f"storage.objects.create / get granted on project {project}"
    elif check == "vertex":
        url = (f"https://{region}-aiplatform.googleapis.com/v1/projects/{project}/locations/{region}"
               f"/publishers/google/models/{VERTEX_MODEL}:generateContent")
        st, body = _call("POST", url, headers=hdr, json_body={
            "contents": [{"role": "user", "parts": [{"text": "Reply with exactly: OK"}]}],
            "generationConfig": {"maxOutputTokens": 16, "temperature": 0, "thinkingConfig": {"thinkingBudget": 0}}})
        if st == 200:
            try:
                text = "".join(p.get("text", "") for p in body["candidates"][0]["content"]["parts"]).strip()
            except (KeyError, IndexError, TypeError):
                text = ""
            return f"{VERTEX_MODEL} in {region} answered “{text[:20] or '(empty)'}”"
    elif check == "artifact_registry":
        st, body = _call("GET", f"https://artifactregistry.googleapis.com/v1/projects/{project}/locations/{region}/repositories",
                         headers=hdr, params={"pageSize": 50})
        if st == 200:
            repos = [r.get("name", "").rsplit("/", 1)[-1] for r in (body or {}).get("repositories") or []]
            return f"{len(repos)} repositor{'y' if len(repos) == 1 else 'ies'} in {region}" + (f": {', '.join(repos[:3])}" if repos else "")
    elif check == "cloud_run":
        st, body = _call("GET", f"https://run.googleapis.com/v2/projects/{project}/locations/{region}/services",
                         headers=hdr, params={"pageSize": 50})
        if st == 200:
            svcs = [r.get("name", "").rsplit("/", 1)[-1] for r in (body or {}).get("services") or []]
            return f"{len(svcs)} Cloud Run service(s) in {region}" + (f": {', '.join(svcs[:3])}" if svcs else "")
    else:
        raise ValueError(check)
    raise _api_error(check, st, body, ctx)


def _result(check: str, cap: str, label: str, ok: bool, detail: str, status: int | None = None,
            hint: str = "", permission: str = "", ms: int = 0) -> dict[str, Any]:
    return {"id": check, "capability": cap, "label": label, "ok": ok, "status": status, "detail": detail,
            "hint": hint, "permission": permission, "ms": ms}


def checks_for(keys: list[str] | None, bucket: str) -> list[str]:
    cons = consumers()
    keys = [ALIASES.get(k, k) for k in (keys or list(cons))]
    unknown = [k for k in keys if k not in cons]
    if unknown:
        raise IdentityError(f"Unknown consumer(s): {', '.join(unknown)}")
    wanted = {c for k in keys for c in cons[k]["checks"]}
    if not keys or set(keys) == set(cons):
        wanted |= {"bq_list", "bq_query", "gcs_buckets", "vertex"}
    if not bucket:
        wanted.discard("gcs_objects")
    return [c[0] for c in CHECKS if c[0] in wanted]


def run_tests(ident_id: str, bucket: str = "", user: str = "", consumer_keys_: list[str] | None = None,
              dataset: str = "", projects: dict[str, Any] | None = None) -> dict[str, Any]:
    rec = identities().get(ident_id)
    if not rec:
        raise KeyError(ident_id)
    bucket = (bucket or "").strip().removeprefix("gs://").strip("/")
    if bucket and not re.fullmatch(r"[a-z0-9][a-z0-9._-]{1,221}[a-z0-9]", bucket):
        raise IdentityError("Bucket: a bucket name such as my-bucket (no gs:// path)")
    dataset = (dataset or setting("BQ_SANDBOX_DATASET")).strip()
    if dataset and not re.fullmatch(r"\w{1,1024}", dataset):
        raise IdentityError("Dataset: letters, digits and _ (the dataset id, no project)")
    keys = [ALIASES.get(k, k) for k in (consumer_keys_ or consumer_keys())]
    check_ids = checks_for(keys, bucket)
    info = json.loads(rec["json"])
    projects = projects or {}
    project = projects.get("project") or rec.get("project") or default_project(info)
    ctx = {"project": project, "region": setting("GCP_REGION", "us-central1"),
           "exec_project": projects.get("exec_project") or project,
           "data_projects": projects.get("data_projects") or [project],
           "bucket": bucket, "dataset": dataset, "principal": rec.get("principal") or ""}
    results: list[dict[str, Any]] = []
    t0 = time.monotonic()
    try:
        token, who = _token(info)
        ctx["principal"] = who or ctx["principal"]
        results.append(_result("token", "token", "Sign-in · token refresh", True,
                               f"access token issued for {ctx['principal'] or 'this identity'}",
                               200, ms=int((time.monotonic() - t0) * 1000)))
    except CheckFailed as exc:
        results.append(_result("token", "token", "Sign-in · token refresh", False, exc.detail, exc.status, exc.hint))
        token = ""
    checks = [CHECK_INFO[c] for c in check_ids]
    if token and checks:
        def one(c: tuple[str, str, str]) -> dict[str, Any]:
            start = time.monotonic()
            try:
                detail = _run_check(c[0], token, info, ctx)
                return _result(*c, True, detail, 200, ms=int((time.monotonic() - start) * 1000))
            except CheckFailed as exc:
                return _result(*c, False, exc.detail, exc.status, exc.hint, exc.permission,
                               int((time.monotonic() - start) * 1000))
        with ThreadPoolExecutor(max_workers=len(checks)) as pool:
            results += list(pool.map(one, checks))
    elif not token:
        results += [_result(*c, False, "skipped — no access token", None) for c in checks]

    by_id = {r["id"]: r for r in results}

    def verdict(rows: list[dict[str, Any]]) -> str:
        if not rows:
            return "untested"
        return "ok" if all(r["ok"] for r in rows) else "partial" if any(r["ok"] for r in rows) else "failed"

    caps: dict[str, str] = {"token": "ok" if results[0]["ok"] else "failed"}
    for cap in dict.fromkeys(c[1] for c in checks):
        rows = [r for r in results if r["capability"] == cap]
        if cap == "gcs" and bucket:
            rows = [r for r in rows if r["id"] == "gcs_objects"] or rows
        caps[cap] = verdict(rows)
    cons = consumers()
    per_consumer: dict[str, dict[str, str]] = {}
    for k in keys:
        if cons[k].get("service_account_only") and info["type"] != "service_account":
            per_consumer[k] = {"verdict": "unsupported", "why": "needs a service-account identity"}
            continue
        rows = [by_id[c] for c in cons[k]["checks"] if c in by_id] or [by_id["token"]]
        if k == "gcs" and bucket and "gcs_objects" in by_id:
            rows = [by_id["gcs_objects"]]
        per_consumer[k] = {"verdict": verdict(rows)}
    last = {"at": _now(), "by": user, "principal": ctx["principal"], "project": ctx["project"], "bucket": bucket,
            "exec_project": ctx["exec_project"], "data_projects": ctx["data_projects"],
            "dataset": dataset, "region": ctx["region"], "capabilities": caps, "consumers": per_consumer,
            "results": results}

    def write(data: dict[str, Any]) -> None:
        r = (data.get("gcp_identities") or {}).get(ident_id)
        if r is not None:
            prev = r.get("last_test") or {}
            if prev and set(keys) != set(cons):     # a consumer-scoped test: keep the other results
                merged_caps = (prev.get("capabilities") or {}) | caps
                merged_cons = (prev.get("consumers") or {}) | per_consumer
                keep = [x for x in prev.get("results") or [] if x["id"] not in by_id]
                r["last_test"] = last | {"capabilities": merged_caps, "consumers": merged_cons,
                                         "results": [results[0]] + keep + results[1:]}
            else:
                r["last_test"] = last
            if ctx["principal"] and not r.get("principal"):
                r["principal"] = ctx["principal"]

    store.mutate(write)
    return last


# ── project probe: where can this identity read BigQuery / run jobs? ─────
def probe_projects(ident_id: str, project_ids: list[str] | None = None) -> dict[str, Any]:
    """For each candidate project: datasets visible (count + first names) and
    whether bigquery.jobs.create is granted; plus a PROPOSED bigquery_read
    setting (data projects = readable ones, execution project = the first
    where jobs may run). Nothing is applied."""
    rec = identities().get(ident_id)
    if not rec:
        raise KeyError(ident_id)
    if project_ids:
        cands = [{"id": _clean_project(p), "name": p} for p in project_ids if p.strip()]
    else:
        cands = list_projects(ident_id)["projects"]
    names = {p["id"]: p.get("name") or p["id"] for p in (list_projects(ident_id)["projects"] if project_ids else cands)}
    cands = cands[:25]
    info = json.loads(rec["json"])
    try:
        token, who = _token(info)
    except CheckFailed as exc:
        raise IdentityError(f"Sign-in failed: {exc.detail}") from None
    hdr = {"Authorization": f"Bearer {token}"}
    if info.get("quota_project_id"):
        hdr["x-goog-user-project"] = str(info["quota_project_id"])

    def one(pid: str) -> dict[str, Any]:
        row: dict[str, Any] = {"id": pid, "name": names.get(pid, pid), "datasets": None, "dataset_names": [],
                               "datasets_error": "", "jobs_create": None, "jobs_error": ""}
        try:
            st, body = _call("GET", f"https://bigquery.googleapis.com/bigquery/v2/projects/{pid}/datasets",
                             headers=hdr, params={"maxResults": 1000})
            if st == 200:
                ds = [d.get("datasetReference", {}).get("datasetId", "") for d in (body or {}).get("datasets") or []]
                row["datasets"], row["dataset_names"] = len(ds), ds[:8]
            else:
                row["datasets_error"] = f"HTTP {st}: {_error_parts(body)[0][:140]}"
        except CheckFailed as exc:
            row["datasets_error"] = exc.detail
        try:   # dry-run with this project as the job project = exactly "can jobs run here?"
            st, body = _call("POST", f"https://bigquery.googleapis.com/bigquery/v2/projects/{pid}/jobs", headers=hdr,
                             json_body={"configuration": {"dryRun": True, "query": {"query": "select 1", "useLegacySql": False}}})
            row["jobs_create"] = st == 200
            if st != 200:
                row["jobs_error"] = f"HTTP {st}: {_error_parts(body)[0][:140]}"
        except CheckFailed as exc:
            row["jobs_error"] = exc.detail
        return row

    with ThreadPoolExecutor(max_workers=min(8, max(1, len(cands)))) as pool:
        rows = list(pool.map(one, [c["id"] for c in cands]))
    readable = [r["id"] for r in rows if r["datasets"]]
    runnable = [r["id"] for r in rows if r["jobs_create"]]
    exec_project = next((p for p in readable if p in runnable), None) or (runnable[0] if runnable else "")
    return {"identity": ident_id, "principal": who, "projects": rows,
            "proposal": {"identity": ident_id, "data_projects": readable, "exec_project": exec_project,
                         "project": readable[0] if readable else ""}}


# ── principal switch: who may act as an identity · short-lived tokens ────
# Every identity carries an access list (vault: gcp_identities.<id>.access):
#   {"mode": "owner"}                      the user who added it + Studio admins (default)
#   {"mode": "list", "users": [...]}       those users / e-mails, plus the owner and admins
#   {"mode": "everyone"}                   anyone signed in to this appliance
# Services never get the key: they ask POST /studio/internal/gcp-token for a
# short-lived OAuth access token for (identity, user) and Studio checks this
# list on every request (deny by default).
ACCESS_MODES = ("owner", "list", "everyone")
SCOPE_PREFIX = "https://www.googleapis.com/auth/"
ALLOWED_SCOPES = {SCOPE_PREFIX + s for s in (
    "cloud-platform", "cloud-platform.read-only", "bigquery", "bigquery.readonly", "devstorage.read_only",
    "devstorage.read_write", "devstorage.full_control")}
TOKEN_MARGIN = 120                    # renew Studio's cached token this long before Google's expiry
_tok_cache: dict[tuple, dict[str, Any]] = {}
_tok_lock = __import__("threading").Lock()


def access_of(rec: dict[str, Any]) -> dict[str, Any]:
    a = rec.get("access") if isinstance(rec.get("access"), dict) else {}
    mode = a.get("mode") if a.get("mode") in ACCESS_MODES else "owner"
    users = [str(u) for u in (a.get("users") or []) if str(u).strip()] if mode == "list" else []
    out = {"mode": mode, "users": users, "owner": str(rec.get("added_by") or "")}
    if a.get("updated_by"):
        out |= {"updated_by": a["updated_by"], "updated_at": a.get("updated_at", "")}
    return out


def _clean_users(users: Any) -> list[str]:
    if isinstance(users, str):
        users = users.replace(";", ",").replace("\n", ",").split(",")
    out: list[str] = []
    for u in users or []:
        u = str(u).strip().lower()
        if not u:
            continue
        if len(u) > 254 or not re.fullmatch(r"[a-z0-9._%+@\-]+", u):
            raise IdentityError(f"Not a user name or e-mail: {u[:60]!r}")
        if u not in out:
            out.append(u)
    if len(out) > 200:
        raise IdentityError("At most 200 users per access list")
    return out


def set_access(ident_id: str, mode: str, users: Any, by: str) -> dict[str, Any]:
    if ident_id not in identities():
        raise KeyError(ident_id)
    if mode not in ACCESS_MODES:
        raise IdentityError(f"Access must be one of {', '.join(ACCESS_MODES)}")
    clean = _clean_users(users) if mode == "list" else []
    if mode == "list" and not clean:
        raise IdentityError("Add at least one user or e-mail (or choose owner and admins only)")

    def write(data: dict[str, Any]) -> None:
        data["gcp_identities"][ident_id]["access"] = {"mode": mode, "users": clean, "updated_by": by,
                                                      "updated_at": _now()}

    store.mutate(write)
    _forget_tokens(ident_id)
    return public(ident_id, identities()[ident_id], assignments())


def user_role(user: str) -> str:
    """Role of a user named by a service (no session at hand): the stored
    Studio user, else admin for OIDC_ADMIN_USERS, else none."""
    import users as _users

    rec = _users.get(user) or _users.get(user.lower()) or {}
    if rec.get("role"):
        return str(rec["role"])
    return "admin" if user.lower() in settings.cfg.oidc_admin_users else ""


def may_act(rec: dict[str, Any], user: str, role: str = "", email: str = "") -> bool:
    """Deny by default: owner, admins, then the identity's access list."""
    who = {x.strip().lower() for x in (user, email) if x and x.strip()}
    if not who:
        return False
    if settings.ROLE_RANK.get(role or "", 0) >= settings.ROLE_RANK.get("admin", 99):
        return True
    a = access_of(rec)
    if a["owner"] and a["owner"].lower() in who:
        return True
    if a["mode"] == "everyone":
        return settings.ROLE_RANK.get(role or "", 0) > 0 or bool(user)
    return a["mode"] == "list" and bool(who & {u.lower() for u in a["users"]})


def _known(rec: dict[str, Any]) -> list[dict[str, str]]:
    out = {p["id"]: p for p in (rec.get("known_projects") or []) if isinstance(p, dict) and p.get("id")}
    if rec.get("project") and rec["project"] not in out:
        out = {rec["project"]: {"id": rec["project"], "name": rec["project"]}} | out
    return list(out.values())


TOOL_CONSUMERS = ("bigquery_read", "bigquery_write", "gcs", "trino_bigquery")


def my_identities(user: str, role: str, email: str = "") -> dict[str, Any]:
    """What the principal pickers show this user: identities they may act as
    (metadata only) and each data consumer's assigned default."""
    data = store.snapshot()
    ids = data.get("gcp_identities", {}) or {}
    defaults: dict[str, Any] = {}
    for key in TOOL_CONSUMERS:
        if key not in consumers():
            continue
        eff, _via = effective(key, data)
        rec = ids.get(eff) or {}
        pr = effective_projects(key, data) if rec else {"project": "", "exec_project": "", "data_projects": []}
        defaults[key] = {"identity": eff, "name": rec.get("name", ""), "principal": rec.get("principal", ""),
                         "type": rec.get("type", ""), **pr}
    bq = defaults.get("bigquery_read") or {}
    out = []
    for ident_id, rec in sorted(ids.items(), key=lambda kv: kv[1].get("name", "").lower()):
        if not may_act(rec, user, role, email):
            continue
        projects = _known(rec)
        exec_project = rec.get("project", "")
        if bq.get("identity") == ident_id:        # same identity as BigQuery · read: offer its projects too
            seen = {p["id"] for p in projects}
            projects = [{"id": p, "name": p} for p in bq.get("data_projects", []) if p not in seen] + projects
            exec_project = bq.get("exec_project") or exec_project
        out.append({"id": ident_id, "name": rec.get("name", ""), "principal": rec.get("principal", ""),
                    "type": rec.get("type", ""), "default_project": rec.get("project", ""),
                    "exec_project": exec_project, "projects": projects, "access": access_of(rec)["mode"]})
    return {"user": user, "identities": out, "defaults": defaults,
            "trino_note": "Trino uses one identity per catalog, shared by everyone (Studio → Cloud identities → Trino)."}


def _clean_scopes(scopes: Any) -> list[str]:
    if not scopes:
        return [CLOUD_SCOPE]
    if isinstance(scopes, str):
        scopes = scopes.split()
    out = []
    for s in scopes:
        s = str(s).strip()
        if not s.startswith("https://"):
            s = SCOPE_PREFIX + s
        if s not in ALLOWED_SCOPES:
            raise IdentityError(f"Scope not allowed: {s}")
        if s not in out:
            out.append(s)
    return out


def _refresh(creds: Any) -> None:
    """Exchange with Google (requests honours HTTPS_PROXY). Tests replace this."""
    import google.auth.transport.requests

    creds.refresh(google.auth.transport.requests.Request())


def _forget_tokens(ident_id: str) -> None:
    with _tok_lock:
        for k in [k for k in _tok_cache if k[0] == ident_id]:
            _tok_cache.pop(k, None)


def mint(ident_id: str, scopes: Any = None) -> dict[str, Any]:
    """A short-lived OAuth access token for the identity, cached until shortly
    before it expires. Service accounts honour the scopes asked for;
    personal sign-ins keep the scopes of their refresh token (Google does not
    narrow those). Raises KeyError / IdentityError / PermissionError."""
    rec = identities().get(ident_id)
    if not rec:
        raise KeyError(ident_id)
    sc = _clean_scopes(scopes)
    key = (ident_id, _digest(rec["json"]), tuple(sc))
    with _tok_lock:
        hit = _tok_cache.get(key)
        if hit and hit["expiry"] - TOKEN_MARGIN > time.time():
            return {**hit["out"], "expires_in": int(hit["expiry"] - time.time())}
    info = json.loads(rec["json"])
    if info["type"] == "service_account":
        from google.oauth2 import service_account

        creds = service_account.Credentials.from_service_account_info(info, scopes=sc)
    else:
        from google.oauth2.credentials import Credentials

        creds = Credentials.from_authorized_user_info(info)
    try:
        _refresh(creds)
    except Exception as exc:  # noqa: BLE001 — RefreshError, transport errors (message carries no secret)
        msg = str(exc).split("\n")[0][:200]
        hint = ("the sign-in expired or was revoked — sign in with Google again" if "invalid_grant" in msg
                else "check the identity with Studio → Cloud identities → Test")
        raise PermissionError(f"Google refused to issue a token for {rec.get('name', ident_id)}: {msg} ({hint})") from None
    exp = creds.expiry.replace(tzinfo=dt.timezone.utc).timestamp() if creds.expiry else time.time() + 3000
    out = {"access_token": creds.token, "identity": ident_id, "name": rec.get("name", ""),
           "principal_email": rec.get("principal") or str(info.get("client_email") or ""),
           "default_project": rec.get("project", ""), "quota_project": str(info.get("quota_project_id") or ""),
           "type": info["type"], "scopes": sc if info["type"] == "service_account" else [CLOUD_SCOPE]}
    with _tok_lock:
        _tok_cache[key] = {"out": out, "expiry": exp}
    return {**out, "expires_in": int(exp - time.time())}


# grants: a Studio-signed (user, identity, service) record a service keeps for
# unattended work (scheduled canvas runs). It carries no Google secret and is
# useless without that service's own token; the access list is re-checked on
# every use, so taking a user off the list revokes it.
def issue_grant(user: str, ident_id: str, service: str) -> str:
    import auth

    return auth.serializer("gcp-grant").dumps({"u": user.lower(), "i": ident_id, "s": service})


def read_grant(grant: str, service: str) -> tuple[str, str] | None:
    import auth
    from itsdangerous import BadSignature

    try:
        d = auth.serializer("gcp-grant").loads(grant, max_age=400 * 86400)
    except BadSignature:
        return None
    if not isinstance(d, dict) or d.get("s") != service or not d.get("u") or not d.get("i"):
        return None
    return str(d["u"]), str(d["i"])
