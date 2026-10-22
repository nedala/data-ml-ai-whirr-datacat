"""Runtime configuration, read from the environment (see the `studio` service
in docker-compose.yml). `reload()` re-reads it — tests point Studio at a temp
project directory that way."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent

ROLES = ("viewer", "analyst", "modeler", "admin")
ROLE_RANK = {r: i + 1 for i, r in enumerate(ROLES)}
SESSION_TTL = 8 * 3600
STEP_UP_TTL = 5 * 60


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, "").strip() or default


@dataclass(frozen=True)
class Config:
    project: Path               # repo root as seen by Studio
    data: Path
    vault_dir: Path
    studio_data: Path
    catalog: Path
    ui_kit: Path
    session_secret_file: Path
    appliance_env: str          # dev | preprod | prod
    host_project_root: str      # host path of `project` ("" = derive)
    compose_project: str
    facade_host: str
    facade_port: str
    sso_provider: str
    oidc_tenant: str
    oidc_client_id: str
    oidc_client_secret: str
    oidc_redirect_uri: str
    oidc_admin_group: str
    oidc_admin_users: frozenset[str]   # UPNs / e-mails that are admin on SSO sign-in
    oidc_graph_scopes: str             # delegated Microsoft Graph scopes requested at SSO ("" = identity only)
    break_glass: bool
    tls_termination: str        # nginx | ingress
    workbench_url: str
    appliance: str              # data | ml | ai ("" = plain .env)
    appliance_persona: str      # data-pro | ml-scientist | ai-engineer
    data_root: str              # DATA_ROOT as compose sees it (host-relative or absolute)
    env_files: tuple[Path, ...]  # compose --env-file list (as seen by Studio), later wins

    @property
    def local_login_allowed(self) -> bool:
        return self.appliance_env == "dev" or self.break_glass

    @property
    def oidc_enabled(self) -> bool:
        return self.sso_provider == "oidc"


APPLIANCE_NAMES = {"data-pro": "Data Professional", "ml-scientist": "ML Scientist", "ai-engineer": "AI Engineer"}


def _env_files(project: Path, appliance: str) -> tuple[Path, ...]:
    files = [project / ".env"]
    if appliance:
        files.append(project / "appliances" / f"{appliance}.env")
    return tuple(files)   # existence is checked at use time (see env_files())


def _load() -> Config:
    project = Path(_env("PROJECT_DIR", "/host-project"))
    appliance = _env("APPLIANCE")
    data_root = _env("DATA_ROOT", "./data")
    # DATA_DIR = the data root as mounted into Studio; default derives it
    # from DATA_ROOT relative to the project.
    default_data = Path(data_root) if data_root.startswith("/") else project / data_root
    data = Path(_env("DATA_DIR", str(default_data)))
    vault_dir = Path(_env("VAULT_DIR", str(data / "vault")))
    ui_kit = Path(_env("UI_KIT_DIR", "/opt/ui-kit"))
    if not ui_kit.is_dir():
        ui_kit = HERE.parent / "ui-kit"   # repo layout: build/ui-kit beside build/studio
    env = _env("APPLIANCE_ENV", "dev").lower()
    return Config(
        project=project,
        data=data,
        vault_dir=vault_dir,
        studio_data=data / "studio",
        catalog=Path(_env("STUDIO_CATALOG", str(HERE / "catalog.yaml"))),
        ui_kit=ui_kit,
        session_secret_file=Path(_env("STUDIO_SESSION_SECRET_FILE", str(vault_dir / "session.key"))),
        appliance_env=env if env in ("dev", "preprod", "prod") else "dev",
        host_project_root=_env("HOST_PROJECT_ROOT"),
        compose_project=_env("COMPOSE_PROJECT_NAME", "bsp_data"),
        facade_host=_env("PUBLIC_FACADE_HOST", "localhost"),
        facade_port=_env("FACADE_PORT", "31201"),
        sso_provider=_env("SSO_PROVIDER").lower(),
        oidc_tenant=_env("OIDC_TENANT_ID"),
        oidc_client_id=_env("OIDC_CLIENT_ID"),
        oidc_client_secret=_env("OIDC_CLIENT_SECRET"),
        oidc_redirect_uri=_env("OIDC_REDIRECT_URI"),
        oidc_admin_group=_env("OIDC_ADMIN_GROUP"),
        oidc_admin_users=frozenset(u.strip().lower() for u in _env("OIDC_ADMIN_USERS").split(",") if u.strip()),
        oidc_graph_scopes=" ".join(_env("OIDC_GRAPH_SCOPES").replace(",", " ").split()),
        break_glass=_env("STUDIO_BREAK_GLASS") == "1",
        tls_termination=_env("TLS_TERMINATION", "nginx").lower(),
        workbench_url=_env("WORKBENCH_URL", "http://appliance:8002"),
        appliance=appliance,
        appliance_persona=_env("APPLIANCE_PERSONA", "data-pro"),
        data_root=data_root,
        env_files=_env_files(project, appliance),
    )


cfg: Config = _load()


def reload() -> Config:
    global cfg
    cfg = _load()
    return cfg


def dotenv() -> dict[str, str]:
    """Non-secret settings from the project's .env plus the appliance overlay
    (appliances/<APPLIANCE>.env wins), as compose reads them."""
    out: dict[str, str] = {}
    for path in env_files():
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            out[key.strip().removeprefix("export ").strip()] = value.strip().strip('"').strip("'")
    return out


def env_files() -> list[Path]:
    """The compose --env-file list that exists right now (later wins)."""
    return [f for f in cfg.env_files if f.is_file()]


def appliance_persona() -> str:
    return cfg.appliance_persona


def appliance_name() -> str:
    return APPLIANCE_NAMES.get(cfg.appliance_persona, "Data & AI")


def user_handle() -> str:
    return _env("USER_HANDLE") or dotenv().get("USER_HANDLE") or "analyst"


def facade_url() -> str:
    port = "" if cfg.facade_port == "443" else f":{cfg.facade_port}"
    return f"https://{cfg.facade_host}{port}"
