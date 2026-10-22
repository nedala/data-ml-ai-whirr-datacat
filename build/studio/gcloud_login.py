"""“Sign in with Google”: runs `gcloud auth application-default login
--no-launch-browser` in a short-lived container and turns the result into an
authorized_user cloud identity.

Flow: start → the container prints Google's consent URL (PKCE state lives in
that gcloud process) → the admin signs in in a popup that ends on
sdk.cloud.google.com showing a verification code → code() writes it to the
process' stdin → gcloud exchanges it and writes application_default_
credentials.json into CLOUDSDK_CONFIG, a tmpfs inside the container; the same
shell then prints it to stdout between markers, Studio parses it from the pipe
and imports it. The credential never touches a disk; the container is --rm,
non-root, memory-limited and killed after SESSION_TTL. The code and the JSON
are never logged.
"""

from __future__ import annotations

import json
import logging
import os
import re
import secrets
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any

import dockerops
import settings

log = logging.getLogger("studio.gcloud_login")

DEFAULT_IMAGE = "gcr.io/google.com/cloudsdktool/google-cloud-cli:slim"
SESSION_TTL = 600            # hard limit for one sign-in (seconds)
URL_WAIT = 45
EXCHANGE_WAIT = 60
BEGIN, END = "<<<ADC-JSON-BEGIN>>>", "<<<ADC-JSON-END>>>"
SCOPES = ("https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/userinfo.email,openid")
URL_RE = re.compile(r"https://accounts\.google\.com/o/oauth2/auth\?\S+")
NO_PROXY = ("localhost,127.0.0.1,.internal,nginx,studio,px-proxy,docs,postgres,appliance,code,superset,trino,"
            "canvas,connectors,mlflow,mlops,harness,mcp,ollama,disputes,ccai,browser,host.docker.internal")
MAX_SESSIONS = 3


class LoginError(ValueError):
    pass


def image() -> str:
    return os.environ.get("GCLOUD_IMAGE", "").strip() or settings.dotenv().get("GCLOUD_IMAGE", "").strip() or DEFAULT_IMAGE


def network() -> str:
    return os.environ.get("APPLIANCE_NETWORK", "").strip() or settings.dotenv().get("APPLIANCE_NETWORK", "").strip() \
        or "bsp-data-net"


def trust_dir_on_host() -> str:
    """${DATA_ROOT}/trust as the Docker daemon sees it."""
    root = settings.cfg.data_root
    if not root.startswith("/"):
        root = f"{dockerops.host_project_root().rstrip('/')}/{root.removeprefix('./')}"
    return f"{root.rstrip('/')}/trust"


def argv(name: str) -> list[str]:
    script = (f"timeout {SESSION_TTL} gcloud auth application-default login --no-launch-browser "
              f"--scopes={SCOPES} && echo '{BEGIN}' && cat \"$CLOUDSDK_CONFIG/application_default_credentials.json\" "
              f"&& echo && echo '{END}'")
    return ["docker", "run", "--rm", "-i", "--name", name, "--label", "bsp.studio=gcloud-login",
            "--network", network(), "--user", "1000:1000", "--memory", "512m", "--pids-limit", "256",
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges",
            "--tmpfs", "/gcloud:rw,uid=1000,gid=1000,mode=0700,size=16m",
            "-v", f"{trust_dir_on_host()}:/trust:ro",
            "-e", "CLOUDSDK_CONFIG=/gcloud", "-e", "HOME=/gcloud",
            "-e", "HTTPS_PROXY=http://px-proxy:3128", "-e", "HTTP_PROXY=http://px-proxy:3128",
            "-e", "https_proxy=http://px-proxy:3128", "-e", "http_proxy=http://px-proxy:3128",
            "-e", f"NO_PROXY={NO_PROXY}", "-e", f"no_proxy={NO_PROXY}",
            "-e", "CLOUDSDK_CORE_CUSTOM_CA_CERTS_FILE=/trust/ca-bundle.pem",
            # Google's granular consent lets the user untick scopes; oauthlib would then
            # abort with "Scope has changed" — accept the narrower grant (Test shows gaps)
            "-e", "OAUTHLIB_RELAX_TOKEN_SCOPE=1",
            "-e", "CLOUDSDK_CORE_DISABLE_USAGE_REPORTING=true", "-e", "CLOUDSDK_COMPONENT_MANAGER_DISABLE_UPDATE_CHECK=true",
            image(), "sh", "-c", script]


@dataclass
class Session:
    id: str
    user: str
    container: str
    proc: subprocess.Popen
    started: float = field(default_factory=time.time)
    out: list[str] = field(default_factory=list)
    url: str = ""
    done: threading.Event = field(default_factory=threading.Event)
    lock: threading.Lock = field(default_factory=threading.Lock)
    used: bool = False


_sessions: dict[str, Session] = {}
_lock = threading.Lock()


def _reader(s: Session) -> None:
    assert s.proc.stdout is not None
    for line in s.proc.stdout:
        with s.lock:
            s.out.append(line)
            if not s.url and (m := URL_RE.search(line)):
                s.url = m.group(0)
    s.proc.wait()
    s.done.set()


def _kill(s: Session) -> None:
    try:
        if s.proc.poll() is None:
            subprocess.run(["docker", "kill", s.container], capture_output=True, timeout=20)
            try:
                s.proc.wait(timeout=10)
            except subprocess.TimeoutExpired:
                s.proc.kill()
    except (OSError, subprocess.SubprocessError) as exc:
        log.warning("gcloud login %s: cleanup failed: %s", s.id, exc)
    finally:
        with s.lock:
            s.out.clear()          # may hold the credential JSON — drop it
        with _lock:
            _sessions.pop(s.id, None)


def _reap() -> None:
    now = time.time()
    for s in list(_sessions.values()):
        if now - s.started > SESSION_TTL or s.done.is_set() and not s.used and now - s.started > 30:
            _kill(s)


def _errors(s: Session) -> str:
    with s.lock:
        lines = [ln.strip() for ln in s.out if ln.strip().startswith("ERROR")]
    text = " ".join(lines)
    if "Malformed auth code" in text or "invalid_grant" in text:
        return "Google rejected the verification code (invalid or expired verification code) — start the sign-in again"
    if "Scope has changed" in text:
        return ("Google granted fewer permissions than requested — start again and, on the consent screen, "
                "tick every box (especially “See, edit, configure and delete your Google Cloud data”)")
    if "access_denied" in text:
        return "Sign-in was cancelled or blocked (your Google Workspace admin may block gcloud for third-party access)"
    if re.search(r"ProxyError|Connection|SSL|certificate", text):
        return ("gcloud could not reach Google through the egress proxy — allow oauth2.googleapis.com, "
                "accounts.google.com and sdk.cloud.google.com (Studio → Egress)")
    return (lines[-1][:300] if lines else "gcloud exited without a credential")


def start(user: str) -> dict[str, Any]:
    with _lock:
        _reap()
        if len(_sessions) >= MAX_SESSIONS:
            raise LoginError("Too many sign-ins in progress — cancel one or wait a few minutes")
        sid = secrets.token_urlsafe(12)
    name = f"bsp-gcloud-login-{secrets.token_hex(4)}"
    try:
        proc = subprocess.Popen(argv(name), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                text=True, bufsize=1)
    except OSError as exc:
        raise LoginError(f"Could not start gcloud ({exc.strerror}) — Studio needs the docker CLI") from None
    s = Session(sid, user, name, proc)
    with _lock:
        _sessions[sid] = s
    threading.Thread(target=_reader, args=(s,), daemon=True, name=f"gcloud-{sid}").start()
    timer = threading.Timer(SESSION_TTL, lambda: _kill(s))
    timer.daemon = True
    timer.start()
    deadline = time.time() + URL_WAIT
    while time.time() < deadline and not s.url and not s.done.is_set():
        time.sleep(0.2)
    if not s.url:
        with s.lock:
            tail = " ".join(ln.strip() for ln in s.out[-5:])[:300]
        _kill(s)
        if "Unable to find image" in tail or "pull access denied" in tail or "No such image" in tail:
            raise LoginError(f"The gcloud image {image()} is not on this host — docker pull it (or set GCLOUD_IMAGE)")
        raise LoginError("gcloud did not produce a sign-in link" + (f": {tail}" if tail else ""))
    return {"session": sid, "auth_url": s.url, "expires_in": SESSION_TTL}


def get(sid: str, user: str) -> Session:
    s = _sessions.get(sid)
    if not s or s.user != user:
        raise KeyError(sid)
    return s


def exchange(sid: str, user: str, code: str) -> str:
    """Send the verification code; return the authorized_user JSON text."""
    s = get(sid, user)
    code = (code or "").strip()
    if not code or len(code) > 512 or any(c.isspace() for c in code):
        raise LoginError("Paste the verification code exactly as Google shows it")
    with s.lock:
        if s.used:
            raise LoginError("This sign-in was already used — start again")
        s.used = True
    try:
        if s.done.is_set():
            raise LoginError(_errors(s) or "The sign-in expired — start again")
        try:
            assert s.proc.stdin is not None
            s.proc.stdin.write(code + "\n")
            s.proc.stdin.flush()
            s.proc.stdin.close()
        except (OSError, ValueError):
            raise LoginError("The sign-in expired — start again") from None
        if not s.done.wait(EXCHANGE_WAIT):
            raise LoginError("gcloud did not finish within a minute — start again")
        with s.lock:
            text = "".join(s.out)
        if BEGIN in text and END in text:
            blob = text.split(BEGIN, 1)[1].split(END, 1)[0].strip()
            try:
                info = json.loads(blob)
            except ValueError:
                raise LoginError("gcloud returned an unreadable credential") from None
            if info.get("type") != "authorized_user":
                raise LoginError("gcloud returned an unexpected credential type")
            return json.dumps(info)
        raise LoginError(_errors(s))
    finally:
        _kill(s)


def cancel(sid: str, user: str) -> None:
    _kill(get(sid, user))
