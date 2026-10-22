#!/usr/bin/env python3
"""px-proxy — the appliance egress gateway (plan/v2-architecture.md §2).

Runs as uid 1000. `run.py --trust-only` = init-container mode (writes /trust
from the egress CA public cert only). Otherwise one process, one asyncio loop:
  * mitmproxy (explicit proxy) on :3128 with the EgressAddon allowlist
    (regular mode, or `upstream:<EGRESS_UPSTREAM_PROXY>` when chaining to a
    host proxy such as genotrance/px on a TLS-inspecting corporate network)
  * admin API on :8081 (Studio)
  * TCP tunnels from the allowlist's tcp_tunnels
  * watchers: allowlist mtime (hot reload) and CORPORATE_CA_DIR (re-trust
    upstream without a restart)

Environment
  EGRESS_UPSTREAM_PROXY   e.g. http://host.docker.internal:3128 (empty = direct)
  EGRESS_UPSTREAM_AUTH    user:pass for the upstream proxy (optional)
  EGRESS_MODE             enforce|audit — only seeds a NEW allowlist file
  EGRESS_ALLOW_AUDIT      false in prod: audit mode is refused
  EGRESS_TLS_INSPECT      true (default): decrypt allowed HTTPS for L7 logging.
                          false: pure CONNECT/SNI allowlisting, no MITM — clients
                          need no custom CA (typical without a TLS-inspecting proxy)
  EGRESS_POLICY_READONLY  true: allowlist is GitOps-managed (ConfigMap); admin
                          writes are refused with 409
  EGRESS_DEFAULT_ALLOWLIST seed file (default: allowlist.default.yaml here)
  EGRESS_DECISIONS_STDOUT also print each decision as JSON to stdout (Log Analytics)
  EGRESS_CA_CERT/_KEY     use a supplied CA (Key Vault / TLS Secret) instead of a generated one
  EGRESS_ALLOWLIST        /data/egress/allowlist.yaml
  EGRESS_DECISIONS        /data/egress/decisions.jsonl
  EGRESS_DECISIONS_MAX_MB 20 (rotated, 3 backups)
  EGRESS_ADMIN_TOKEN      admin API bearer token (else <allowlist dir>/.admin-token)
  EGRESS_TRUST_DIR        /trust
  EGRESS_PROXY_PORT       3128 · EGRESS_ADMIN_PORT 8081
  CORPORATE_CA_DIR        /certs/corporate (TLS-inspecting proxy roots; upstream verification only)
  EGRESS_CORP_RESCAN_S    30 — how often CORPORATE_CA_DIR is re-read
  MITMPROXY_CONFDIR       ~/.mitmproxy (egress CA private key lives here only)
"""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
import signal
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import trust  # noqa: E402
from decisions import DecisionLog  # noqa: E402
from store import PolicyStore  # noqa: E402

logging.basicConfig(level=os.environ.get("EGRESS_LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", stream=sys.stdout)
log = logging.getLogger("px-proxy")


def _patch_bcrypt_for_passlib() -> None:
    """mitmproxy imports passlib (proxyauth addon) at import time; passlib 1.7
    self-tests bcrypt with a >72-byte secret, which bcrypt>=4.1/5 rejects with
    ValueError — so `import mitmproxy.tools.dump` crashes in this image (the
    same breaks the stock `mitmdump`). px-proxy never uses proxy auth; make
    the self-test pass by truncating like bcrypt<4.1 did."""
    try:
        import bcrypt
    except ImportError:
        return
    if not hasattr(bcrypt, "__about__"):
        bcrypt.__about__ = type("about", (), {"__version__": getattr(bcrypt, "__version__", "")})  # type: ignore[attr-defined]
    orig = bcrypt.hashpw
    if getattr(orig, "_px_patched", False):
        return

    def hashpw(password: bytes, salt: bytes) -> bytes:
        return orig(password[:72], salt)

    hashpw._px_patched = True  # type: ignore[attr-defined]
    bcrypt.hashpw = hashpw


def env(name: str, default: str = "") -> str:
    v = os.environ.get(name)
    return v if v not in (None, "") else default


def truthy(v: str) -> bool:
    return v.strip().lower() in ("1", "true", "yes", "on")


def redact(url: str) -> str:
    from urllib.parse import urlsplit

    u = urlsplit(url)
    if u.password:
        return url.replace(f":{u.password}@", ":***@")
    return url


def load_token(data_dir: Path) -> str:
    tok = env("EGRESS_ADMIN_TOKEN")
    if tok:
        return tok
    f = data_dir / ".admin-token"
    if f.exists() and f.read_text().strip():
        return f.read_text().strip()
    tok = secrets.token_urlsafe(32)
    trust.atomic_write(f, (tok + "\n").encode(), 0o600)
    log.info("created admin token %s (Studio reads it)", f)
    return tok


def trust_only(argv: list[str]) -> int:
    """`run.py --trust-only [--ca-cert FILE] [--trust-dir DIR]`

    Workload-pod init container on AKS: write /trust (egress-ca.pem,
    ca-bundle.pem, ready) from the egress CA PUBLIC cert (mounted
    from the px-proxy CA Secret's tls.crt) — the private key never leaves the
    px-proxy pod."""
    import argparse

    ap = argparse.ArgumentParser(prog="run.py --trust-only")
    ap.add_argument("--trust-only", action="store_true")
    ap.add_argument("--ca-cert", default=env("EGRESS_CA_CERT", "/etc/egress-ca/tls.crt"))
    ap.add_argument("--trust-dir", default=env("EGRESS_TRUST_DIR", "/trust"))
    a = ap.parse_args(argv)
    info = trust.write_trust_dir(Path(a.trust_dir), ca_pem=Path(a.ca_cert).read_bytes())
    trust.mark_ready(Path(a.trust_dir), info)
    log.info("wrote %s for %s (expires %s)", a.trust_dir, info["cn"], info["not_after"][:10])
    return 0


async def main() -> int:
    if os.getuid() == 0 and not truthy(env("EGRESS_ALLOW_ROOT", "false")):
        log.error("refusing to run as root — run px-proxy as uid 1000")
        return 2

    confdir = Path(env("MITMPROXY_CONFDIR", "~/.mitmproxy")).expanduser()
    trust_dir = Path(env("EGRESS_TRUST_DIR", "/trust"))
    corp_dir = Path(env("CORPORATE_CA_DIR", "/certs/corporate"))
    allowlist = Path(env("EGRESS_ALLOWLIST", "/data/egress/allowlist.yaml"))
    decisions_path = Path(env("EGRESS_DECISIONS", "/data/egress/decisions.jsonl"))
    upstream = env("EGRESS_UPSTREAM_PROXY")
    proxy_port = int(env("EGRESS_PROXY_PORT", "3128"))
    admin_port = int(env("EGRESS_ADMIN_PORT", "8081"))
    allowlist.parent.mkdir(parents=True, exist_ok=True)

    # 1. trust: egress CA + /trust outputs + upstream bundle (certifi + corporate)
    t0 = time.time()
    trust.ensure_egress_ca(confdir)
    ca_info = trust.write_trust_dir(trust_dir, confdir)
    upstream_bundle, corp_count = trust.write_upstream_bundle(confdir, corp_dir)
    log.info("egress CA %s (expires %s, sha256 %s…); /trust written in %.1fs; %d corporate root(s) from %s",
             ca_info["cn"], ca_info["not_after"][:10], ca_info["sha256"][:23], time.time() - t0, corp_count, corp_dir)
    if corp_count == 0 and upstream:
        log.warning("no corporate roots in %s but chaining to %s — if that network re-signs TLS "
                    "upstream verification will fail; run `make corporate-ca` on the host", corp_dir, redact(upstream))

    # 2. policy + decision log
    store = PolicyStore(allowlist, Path(env("EGRESS_DEFAULT_ALLOWLIST", str(HERE / "allowlist.default.yaml"))),
                        env_mode=env("EGRESS_MODE"), allow_audit=truthy(env("EGRESS_ALLOW_AUDIT", "true")),
                        readonly=truthy(env("EGRESS_POLICY_READONLY", "false")))
    dlog = DecisionLog(decisions_path, max_bytes=int(float(env("EGRESS_DECISIONS_MAX_MB", "20")) * 1024 * 1024),
                       stdout=truthy(env("EGRESS_DECISIONS_STDOUT", "false")))
    tls_inspect = truthy(env("EGRESS_TLS_INSPECT", "true"))
    token = load_token(allowlist.parent)

    # 3. mitmproxy
    _patch_bcrypt_for_passlib()
    from mitmproxy import options
    from mitmproxy.tools.dump import DumpMaster

    from egress_addon import EgressAddon
    from tunnels import TunnelManager

    mode = f"upstream:{upstream}" if upstream else "regular"
    opts = options.Options(confdir=str(confdir), listen_host="0.0.0.0", listen_port=proxy_port, mode=[mode],
                           ssl_verify_upstream_trusted_ca=str(upstream_bundle), http3=False)
    master = DumpMaster(opts, with_termlog=True, with_dumper=False)
    extra = {"connection_strategy": "lazy", "termlog_verbosity": env("EGRESS_MITM_LOG", "warn"),
             "stream_large_bodies": "1m"}
    if env("EGRESS_UPSTREAM_AUTH"):
        extra["upstream_auth"] = env("EGRESS_UPSTREAM_AUTH")
    for k, v in extra.items():
        try:
            master.options.update(**{k: v})
        except Exception as e:  # noqa: BLE001
            log.warning("mitmproxy option %s not applied: %s", k, e)

    state = {"corp_fp": trust.corporate_fingerprint(corp_dir), "corp_count": corp_count,
             "upstream_bundle": str(upstream_bundle), "corp_reloaded": None}
    meta = {"started": time.time(), "trust_dir": str(trust_dir), "tls_inspect": tls_inspect,
            "upstream_display": redact(upstream) if upstream else None}

    class Ready:
        """Marks /trust/ready once mitmproxy's listeners are up."""

        def running(self):
            trust.mark_ready(trust_dir, ca_info)
            log.info("px-proxy ready: proxy :%d (%s), admin :%d, mode=%s, %d rules, tls=%s%s",
                     proxy_port, "upstream " + redact(upstream) if upstream else "direct", admin_port,
                     store.policy.mode, len(store.policy.rules), "inspect" if tls_inspect else "passthrough",
                     ", policy read-only (GitOps)" if store.readonly else "")

    master.addons.add(EgressAddon(store, dlog, tls_inspect=tls_inspect), Ready())
    tunnels = TunnelManager(store, dlog, upstream or None, upstream_auth=env("EGRESS_UPSTREAM_AUTH") or None)

    def trust_info() -> dict:
        corp = []
        for c, pem, name in trust.corporate_cas(corp_dir):
            corp.append(trust.cert_info(c, name))
        files = {}
        for n in ("egress-ca.pem", "ca-bundle.pem", "ready"):
            p = trust_dir / n
            files[n] = {"exists": p.exists(), "bytes": p.stat().st_size if p.exists() else 0,
                        "mtime": p.stat().st_mtime if p.exists() else None}
        warnings = []
        if not corp and upstream:
            warnings.append("No corporate root CAs loaded while chaining to an upstream proxy. Behind a TLS-inspecting corporate proxy "
                            "host run `make corporate-ca` (build/scripts/extract-corporate-ca.sh).")
        for ci in corp:
            if ci["expired"]:
                warnings.append(f"Corporate root {ci['cn']} has EXPIRED — re-extract it.")
            elif ci["days_left"] < 30:
                warnings.append(f"Corporate root {ci['cn']} expires in {ci['days_left']} days.")
        if ca_info["days_left"] < 60:
            warnings.append("Egress CA expires soon — delete the px-proxy-ca volume to rotate it.")
        return {"egress_ca": ca_info, "corporate": corp, "corporate_dir": str(corp_dir), "trust_dir": str(trust_dir),
                "files": files, "upstream_proxy": meta["upstream_display"], "upstream_bundle": state["upstream_bundle"],
                "corporate_reloaded_at": state["corp_reloaded"], "warnings": warnings,
                "env": env("APPLIANCE_ENV", "dev"), "tls_inspect": tls_inspect,
                "chain": {"clients_trust": "egress CA (/trust/ca-bundle.pem)",
                          "px_proxy_trusts": "certifi/Mozilla roots + corporate roots"}}

    # 4. admin API
    from aiohttp import web

    from admin import build_app

    app = build_app(store=store, dlog=dlog, token=token, trust_info=trust_info, tunnels_status=tunnels.status,
                    ca_pem=lambda: trust.egress_ca_pem(confdir), meta=meta)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, env("EGRESS_ADMIN_BIND", "0.0.0.0"), admin_port).start()

    # 5. watchers
    rescan_every = max(1, int(float(env("EGRESS_CORP_RESCAN_S", "30")) // 2))

    async def watch():
        n = 0
        while True:
            await asyncio.sleep(2)
            n += 1
            try:
                store.reload()
            except Exception:  # noqa: BLE001
                log.exception("allowlist reload failed")
            if n % rescan_every == 0:
                try:
                    fp = trust.corporate_fingerprint(corp_dir)
                    if fp != state["corp_fp"]:
                        bundle, cnt = trust.write_upstream_bundle(confdir, corp_dir)
                        master.options.update(ssl_verify_upstream_trusted_ca=str(bundle))
                        state.update(corp_fp=fp, corp_count=cnt, upstream_bundle=str(bundle),
                                     corp_reloaded=time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
                        log.info("corporate roots changed: %d root(s) now trusted upstream (no restart needed)", cnt)
                except Exception:  # noqa: BLE001
                    log.exception("corporate CA rescan failed")

    await tunnels.start()
    watcher = asyncio.ensure_future(watch())

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, master.shutdown)
        except NotImplementedError:  # pragma: no cover
            pass
    try:
        await master.run()
    finally:
        watcher.cancel()
        await runner.cleanup()
    return 0


if __name__ == "__main__":
    try:
        if "--trust-only" in sys.argv[1:]:
            sys.exit(trust_only(sys.argv[1:]))
        sys.exit(asyncio.run(main()))
    except PermissionError as e:
        log.error("%s", e)
        sys.exit(3)
