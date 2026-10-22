"""Apache Superset 6.x runtime config — compose service `superset` (port 8088).

Served natively under SUPERSET_APP_ROOT=/superset: Superset's AppRootMiddleware
strips the prefix, sets SCRIPT_NAME and prefixes static assets / redirects, so
nginx proxies /superset/ through UNCHANGED (no rewrite). Health endpoints:
/superset/health (Superset's own) and /superset/healthz (added below, the
appliance convention). The shared UI kit is served at /superset/ui-kit/.

Secrets come only from the container environment (vault → Studio → compose):
SUPERSET_SECRET_KEY, PG_PASS. The metadata DB is the Postgres database
`superset` on the warehouse server (same `analyst` login). The "DataPlatform
warehouse" and "Trino (federated)" connections and the dashboards are
registered by bootstrap_dashboards.py. A missing secret is a startup error,
never a silently weak deployment.
"""

from __future__ import annotations

import copy
import os
import sys
from urllib.parse import quote


def _require(name: str) -> str:
    value = os.environ.get(name, "")
    if not value:
        raise RuntimeError(f"{name} is not set — start Superset from Studio so the vault injects it")
    return value


SUPERSET_HOME = os.environ.get("SUPERSET_HOME", "/data/superset")
DATA_DIR = SUPERSET_HOME
APP_NAME = os.environ.get("SUPERSET_APP_NAME", "DataPlatform BI")

# The default theme points the logo at /static/…, which ignores
# SUPERSET_APP_ROOT; re-point it under the app root.
_APP_ROOT = os.environ.get("SUPERSET_APP_ROOT", "").rstrip("/")
APP_ICON = f"{_APP_ROOT}/static/assets/images/superset-logo-horiz.png"
_base = sys.modules.get("superset.config")
if _base is not None and hasattr(_base, "THEME_DEFAULT"):
    THEME_DEFAULT = copy.deepcopy(_base.THEME_DEFAULT)
    THEME_DEFAULT.setdefault("token", {}).update(
        brandLogoUrl=APP_ICON, brandLogoHref=f"{_APP_ROOT}/", brandLogoAlt=APP_NAME,
        colorPrimary="#0B6E4F")

# ── secrets / metadata DB ────────────────────────────────────────────────
SECRET_KEY = _require("SUPERSET_SECRET_KEY")

if os.environ.get("SUPERSET_META_DB_URI"):
    SQLALCHEMY_DATABASE_URI = os.environ["SUPERSET_META_DB_URI"]
else:
    SQLALCHEMY_DATABASE_URI = "postgresql+psycopg2://{user}:{pw}@{host}:{port}/{db}".format(
        user=quote(os.environ.get("PG_USER", "analyst"), safe=""),
        pw=quote(_require("PG_PASS"), safe=""),
        host=os.environ.get("PG_HOST", "postgres"),
        port=os.environ.get("PG_PORT", "5432"),
        db=os.environ.get("SUPERSET_META_DB", "superset"),
    )
SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True, "pool_size": 5, "max_overflow": 10, "pool_recycle": 1800}

# ── behind the nginx facade (HTTPS terminated at nginx) ──────────────────
ENABLE_PROXY_FIX = True
# x_prefix stays 0: nginx forwards the full /superset/… path and the app root
# comes from SUPERSET_APP_ROOT; trusting X-Forwarded-Prefix too would
# double-prefix links.
PROXY_FIX_CONFIG = {"x_for": 1, "x_proto": 1, "x_host": 1, "x_port": 0, "x_prefix": 0}
PREFERRED_URL_SCHEME = "https"

SESSION_COOKIE_SECURE = os.environ.get("SUPERSET_COOKIE_SECURE", "true").lower() in ("1", "true", "yes")
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"

# CSRF stays ON. Scripted REST clients fetch /api/v1/security/csrf_token/
# with their JWT and send it back as X-CSRFToken with the session cookie.
WTF_CSRF_ENABLED = True
WTF_CSRF_TIME_LIMIT = None

# Talisman (security headers / CSP). TALISMAN_ENABLED=false to debug.
TALISMAN_ENABLED = os.environ.get("TALISMAN_ENABLED", "true").lower() in ("1", "true", "yes")
CONTENT_SECURITY_POLICY_WARNING = False

# ── features ─────────────────────────────────────────────────────────────
FEATURE_FLAGS = {
    "DASHBOARD_RBAC": True,
    "ENABLE_TEMPLATE_PROCESSING": True,
    "DRILL_TO_DETAIL": True,
    "DRILL_BY": True,
    "EMBEDDABLE_CHARTS": True,
    "ALERT_REPORTS": False,         # needs Celery workers + a browser
    "GLOBAL_ASYNC_QUERIES": False,  # needs Redis/Celery
    "THUMBNAILS": False,
}
SQLLAB_TIMEOUT = 300
SUPERSET_WEBSERVER_TIMEOUT = 300
ROW_LIMIT = 100000
SQL_MAX_ROW = 100000
PREVENT_UNSAFE_DB_CONNECTIONS = True

# ── caches: on-disk under the persisted SUPERSET_HOME (no Redis) ─────────
_CACHE_DIR = os.path.join(SUPERSET_HOME, "cache")
os.makedirs(_CACHE_DIR, exist_ok=True)


def _fs_cache(prefix: str, timeout: int) -> dict:
    return {
        "CACHE_TYPE": "FileSystemCache",
        "CACHE_DIR": os.path.join(_CACHE_DIR, prefix),
        "CACHE_DEFAULT_TIMEOUT": timeout,
        "CACHE_KEY_PREFIX": f"superset_{prefix}_",
        # 0 = no item cap; a cap makes cachelib keep a shared counter file
        # that concurrent gunicorn workers corrupt.
        "CACHE_THRESHOLD": 0,
    }


CACHE_CONFIG = _fs_cache("meta", 86400)
DATA_CACHE_CONFIG = _fs_cache("data", 3600)
FILTER_STATE_CACHE_CONFIG = _fs_cache("filter_state", 86400)
EXPLORE_FORM_DATA_CACHE_CONFIG = _fs_cache("explore", 86400)

# ── appliance conventions: <prefix>/healthz and <prefix>/ui-kit/ ─────────
UI_KIT_DIR = os.environ.get("UI_KIT_DIR", "/opt/ui-kit")


def FLASK_APP_MUTATOR(app):  # noqa: N802 — Superset config hook name
    from flask import send_from_directory

    @app.route("/healthz")
    def _bsp_healthz():  # noqa: ANN202
        return {"status": "ok", "service": "superset"}, 200

    @app.route("/ui-kit/<path:name>")
    def _bsp_ui_kit(name: str):  # noqa: ANN202
        return send_from_directory(UI_KIT_DIR, name)

    # health probes and the UI kit need no login / CSRF
    try:
        from superset.extensions import csrf

        csrf.exempt(_bsp_healthz)
    except Exception:  # noqa: BLE001
        pass


# ── Single sign-on from the appliance front door ─────────────────────────
# nginx authenticates every request with Studio and forwards the identity as
# X-Auth-User / X-Auth-Email / X-Auth-Name / X-Auth-Role (overwriting anything
# a client sent). A browser arriving that way is logged in as that user — no
# Superset password, no browser autofill confusion. The header is trusted ONLY
# when the TCP peer is the nginx container, so another workload on the
# internal network cannot forge it. Database login (the admin password in the
# vault) keeps working for scripts: dashboard import, notebooks, MCP tools.
SSO_ROLE_MAP = {"admin": "Admin", "modeler": "Alpha", "analyst": "Alpha", "viewer": "Gamma"}


def _facade_ips() -> set[str]:
    import socket

    try:
        return set(socket.gethostbyname_ex(os.environ.get("FACADE_HOST", "nginx"))[2])
    except OSError:
        return set()


def _install_gate_sso(app) -> None:  # noqa: ANN001
    import time

    from flask import request
    from flask_login import current_user, login_user

    cache = {"ips": set(), "at": 0.0}

    def from_facade() -> bool:
        # ProxyFix rewrites REMOTE_ADDR from X-Forwarded-For; use the real peer
        peer = request.environ.get("werkzeug.proxy_fix.orig", {}).get("REMOTE_ADDR") or request.remote_addr
        if time.time() - cache["at"] > 60:
            cache.update(ips=_facade_ips(), at=time.time())
        return peer in cache["ips"]

    @app.before_request
    def _bsp_gate_sso():  # noqa: ANN202
        name = (request.headers.get("X-Auth-User") or "").strip().lower()
        if not name or request.path.endswith(("/healthz", "/health")) or not from_facade():
            return None
        if current_user.is_authenticated and current_user.username == name:
            return None
        sm = app.appbuilder.sm
        role = sm.find_role(SSO_ROLE_MAP.get((request.headers.get("X-Auth-Role") or "").lower(), "Gamma"))
        user = sm.find_user(username=name)
        if user is None:
            display = (request.headers.get("X-Auth-Name") or name.split("@")[0]).strip()
            first, _, last = display.partition(" ")
            email = (request.headers.get("X-Auth-Email") or f"{name}@appliance.local").strip()
            user = sm.add_user(name, first or name, last or "-", email, role)
            if not user:
                return None
        elif role and role not in user.roles:
            user.roles = [role]            # Studio is the source of truth for the role
            sm.update_user(user)
        login_user(user, remember=False)
        return None


_base_mutator = FLASK_APP_MUTATOR


def FLASK_APP_MUTATOR(app):  # noqa: N802,F811 — extend the hook above
    _base_mutator(app)
    _install_gate_sso(app)
