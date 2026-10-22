"""Client for the model gateway (scoring + admin API).

    from bsp_mlops import deploy
    deploy.set_mode("shadow")                       # champion answers, challenger logged
    deploy.set_mode("canary", canary_weight=0.2)    # 20 % sticky split to challenger
    deploy.set_mode("blue_green", blue_version="1", green_version="2")
    deploy.switch()                                 # instant cut-over blue ↔ green
    deploy.rollback()                               # undo the last change
    deploy.reconcile("shadow")                      # routing re-derived from the registry aliases

Authentication: every call carries the appliance service token
(bsp_mlops.auth, `<data root>/mlops/secrets`). Retries: connection errors and
502 / 503 / 504 are retried up to 3 times with exponential back-off (0.5 s, 1 s,
2 s); scoring is idempotent per request_id, so a retried request is logged once
per attempt with the SAME request_id and can be de-duplicated downstream.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from . import config

RETRY_STATUS = (502, 503, 504)
RETRIES = 3


def headers() -> dict:
    from .auth import client_headers

    return client_headers()


def _url(path: str) -> str:
    return config.GATEWAY_URL.rstrip("/") + path


def _req(method: str, path: str, retries: int = RETRIES, **kw) -> Any:
    timeout = kw.pop("timeout", 60)
    delay, last = 0.5, None
    for attempt in range(retries + 1):
        try:
            r = httpx.request(method, _url(path), headers=headers(), timeout=timeout, **kw)
        except (httpx.ConnectError, httpx.ReadError, httpx.RemoteProtocolError, httpx.ConnectTimeout) as e:
            last = e
        else:
            if r.status_code not in RETRY_STATUS:
                if r.status_code >= 400:
                    raise RuntimeError(f"gateway {method} {path} → {r.status_code}: {r.text[:300]}")
                return r.json()
            last = RuntimeError(f"gateway {method} {path} → {r.status_code}: {r.text[:200]}")
        if attempt < retries:
            time.sleep(delay)
            delay *= 2
    raise RuntimeError(f"gateway {method} {path} failed after {retries + 1} attempts: {last}")


def wait_ready(timeout: float = 120) -> dict:
    t0 = time.time()
    last = None
    while time.time() - t0 < timeout:
        try:
            return _req("GET", "/healthz", timeout=5)
        except Exception as e:
            last = e
            time.sleep(2)
    raise RuntimeError(f"gateway not ready: {last}")


def status(deployment: str = config.DEPLOYMENT) -> dict:
    return _req("GET", f"/admin/deployments/{deployment}")


def set_mode(mode: str, deployment: str = config.DEPLOYMENT, actor: str | None = None, **kw) -> dict:
    body = {"mode": mode, "actor": actor or config.user(), **{k: v for k, v in kw.items() if v is not None}}
    return _req("PUT", f"/admin/deployments/{deployment}", json=body)


def configure(deployment: str = config.DEPLOYMENT, actor: str | None = None, **kw) -> dict:
    body = {"actor": actor or config.user(), **{k: v for k, v in kw.items() if v is not None}}
    return _req("PUT", f"/admin/deployments/{deployment}", json=body)


def switch(deployment: str = config.DEPLOYMENT, actor: str | None = None) -> dict:
    return _req("POST", f"/admin/deployments/{deployment}/switch", params={"actor": actor or config.user()})


def rollback(deployment: str = config.DEPLOYMENT, actor: str | None = None) -> dict:
    return _req("POST", f"/admin/deployments/{deployment}/rollback", params={"actor": actor or config.user()})


def reconcile(policy: str = "shadow", deployment: str = config.DEPLOYMENT, actor: str | None = None,
              canary_weight: float | None = None, reason: str = "") -> dict:
    return _req("POST", f"/admin/deployments/{deployment}/reconcile",
                json={"policy": policy, "actor": actor or config.user(), "canary_weight": canary_weight, "reason": reason})


def smoke(version: str, n: int = 50) -> dict:
    return _req("POST", f"/admin/smoke/{version}", params={"n": n}, timeout=180)


def slo(minutes: int = 60) -> dict:
    return _req("GET", "/admin/slo", params={"minutes": minutes})


def reload() -> dict:
    return _req("POST", "/admin/reload")


def flush() -> dict:
    return _req("POST", "/admin/flush", timeout=60)


def models() -> dict:
    return _req("GET", "/v1/models")


def predict(ban: str, features: dict | None = None, snapshot_month: str | None = None, request_id: str | None = None) -> dict:
    return _req("POST", "/v1/predict", json={"ban": ban, "features": features,
                                             "snapshot_month": snapshot_month, "request_id": request_id})


def predict_batch(instances: list[dict], source: str = "api", deployment: str = config.DEPLOYMENT) -> list[dict]:
    return _req("POST", "/v1/predict/batch", json={"instances": instances, "source": source,
                                                   "deployment": deployment}, timeout=120)["predictions"]


def wait_alias(alias: str, version: str, timeout: float = 60) -> bool:
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            if str(reload()["aliases"].get(alias)) == str(version):
                return True
        except Exception:
            pass
        time.sleep(1)
    return False
