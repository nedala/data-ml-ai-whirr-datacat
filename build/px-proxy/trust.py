"""Trust material for px-proxy.

* The appliance **egress CA** = mitmproxy's CA, persisted in the confdir volume
  (/home/analyst/.mitmproxy). Created once with a meaningful subject.
* ``/trust`` outputs (contract plan §2): egress-ca.pem, ca-bundle.pem,
  ready.
* Upstream verification bundle = certifi + every corporate root in
  CORPORATE_CA_DIR (a TLS-inspecting corporate proxy re-signs TLS).
"""

from __future__ import annotations

import datetime as dt
import hashlib
import logging
import os
import re
import tempfile
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization

log = logging.getLogger("px-proxy.trust")

CA_BASENAME = "mitmproxy"  # mitmproxy loads <confdir>/mitmproxy-ca.pem
CA_CN = os.environ.get("EGRESS_CA_CN", "DataPlatform Appliance Egress CA")
CA_ORG = os.environ.get("EGRESS_CA_ORG", "DataPlatform Data & AI Appliance")
P12_PASSWORD = "changeit"
_PEM_RE = re.compile(rb"-----BEGIN CERTIFICATE-----\s.+?-----END CERTIFICATE-----", re.S)


# ── helpers ──────────────────────────────────────────────────────────────
def atomic_write(path: Path, data: bytes, mode: int = 0o644) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except FileNotFoundError:
            pass
        raise


def split_pems(blob: bytes) -> list[bytes]:
    return [m.group(0) + b"\n" for m in _PEM_RE.finditer(blob)]


def cert_info(cert: x509.Certificate, source: str = "") -> dict:
    now = dt.datetime.now(dt.timezone.utc)
    not_after = cert.not_valid_after_utc
    try:
        bc = cert.extensions.get_extension_for_class(x509.BasicConstraints).value.ca
    except x509.ExtensionNotFound:
        bc = None
    return {
        "subject": cert.subject.rfc4514_string(),
        "cn": _cn(cert.subject),
        "issuer": cert.issuer.rfc4514_string(),
        "not_before": cert.not_valid_before_utc.isoformat(),
        "not_after": not_after.isoformat(),
        "days_left": (not_after - now).days,
        "expired": not_after < now,
        "is_ca": bc,
        "self_signed": cert.issuer == cert.subject,
        "sha256": cert.fingerprint(hashes.SHA256()).hex(":").upper(),
        "source": source,
    }


def _cn(name: x509.Name) -> str:
    try:
        return name.get_attributes_for_oid(x509.NameOID.COMMON_NAME)[0].value  # type: ignore[return-value]
    except IndexError:
        return name.rfc4514_string()


def load_certs(blob: bytes) -> list[x509.Certificate]:
    out = []
    for pem in split_pems(blob):
        try:
            out.append(x509.load_pem_x509_certificate(pem))
        except ValueError as e:
            log.warning("skipping unparsable certificate: %s", e)
    return out


# ── egress CA ────────────────────────────────────────────────────────────
def ensure_egress_ca(confdir: Path) -> x509.Certificate:
    """Create the egress CA once (mitmproxy format) with a proper subject."""
    from mitmproxy.certs import CertStore

    confdir.mkdir(parents=True, exist_ok=True)
    if not os.access(confdir, os.W_OK):
        raise PermissionError(f"{confdir} is not writable by uid {os.getuid()} — "
                              "the CA volume must be owned by 1000:1000")
    ca_file = confdir / f"{CA_BASENAME}-ca.pem"
    ext_cert, ext_key = os.environ.get("EGRESS_CA_CERT", ""), os.environ.get("EGRESS_CA_KEY", "")
    if ext_cert and ext_key and Path(ext_cert).is_file() and Path(ext_key).is_file():
        # AKS: CA supplied from Key Vault / a kubernetes.io/tls Secret, so every
        # replica (and every workload's trust init) shares one CA.
        cert_pem = Path(ext_cert).read_bytes()
        key_pem = Path(ext_key).read_bytes()
        c = load_certs(cert_pem)
        if not c:
            raise RuntimeError(f"{ext_cert} has no certificate")
        serialization.load_pem_private_key(key_pem, None)  # fail fast on a bad key
        with CertStore.umask_secret():
            atomic_write(ca_file, key_pem.rstrip(b"\n") + b"\n" + c[0].public_bytes(serialization.Encoding.PEM), 0o600)
        atomic_write(confdir / f"{CA_BASENAME}-ca-cert.pem", c[0].public_bytes(serialization.Encoding.PEM))
        log.info("using egress CA supplied via EGRESS_CA_CERT/EGRESS_CA_KEY (%s)", _cn(c[0].subject))
    elif not ca_file.exists():
        log.info("creating egress CA %r in %s", CA_CN, confdir)
        CertStore.create_store(confdir, CA_BASENAME, 2048, organization=CA_ORG, cn=CA_CN)
    os.chmod(ca_file, 0o600)
    certs = load_certs(ca_file.read_bytes())
    if not certs:
        raise RuntimeError(f"{ca_file} has no certificate")
    return certs[0]


def egress_ca_pem(confdir: Path) -> bytes:
    return (confdir / f"{CA_BASENAME}-ca-cert.pem").read_bytes()


# ── corporate roots ──────────────────────────────────────────────────────
def corporate_cas(ca_dir: Path | None) -> list[tuple[x509.Certificate, bytes, str]]:
    """Every certificate in *.pem / *.crt under CORPORATE_CA_DIR, deduped."""
    out: list[tuple[x509.Certificate, bytes, str]] = []
    seen: set[bytes] = set()
    if not ca_dir or not ca_dir.is_dir():
        return out
    for f in sorted(list(ca_dir.glob("*.pem")) + list(ca_dir.glob("*.crt"))):
        try:
            blob = f.read_bytes()
        except OSError as e:
            log.warning("cannot read %s: %s", f, e)
            continue
        for pem in split_pems(blob):
            try:
                c = x509.load_pem_x509_certificate(pem)
            except ValueError:
                log.warning("%s: unparsable certificate skipped", f.name)
                continue
            fp = c.fingerprint(hashes.SHA256())
            if fp in seen:
                continue
            seen.add(fp)
            out.append((c, c.public_bytes(serialization.Encoding.PEM), f.name))
    return out


def corporate_fingerprint(ca_dir: Path | None) -> str:
    h = hashlib.sha256()
    for c, pem, name in corporate_cas(ca_dir):
        h.update(pem)
    return h.hexdigest()[:16]


def certifi_bundle() -> bytes:
    try:
        import certifi

        return Path(certifi.where()).read_bytes()
    except Exception:  # pragma: no cover
        for p in ("/etc/ssl/certs/ca-certificates.crt", "/etc/pki/tls/certs/ca-bundle.crt"):
            if Path(p).exists():
                return Path(p).read_bytes()
        return b""


def write_upstream_bundle(confdir: Path, ca_dir: Path | None) -> tuple[Path, int]:
    """certifi + corporate roots → <confdir>/upstream-ca-<hash>.pem.

    The file NAME changes with the content so mitmproxy's cached SSL contexts
    (keyed by the option value) are rebuilt when the option is updated."""
    corp = corporate_cas(ca_dir)
    parts = [certifi_bundle().rstrip(b"\n") + b"\n"]
    for c, pem, name in corp:
        parts.append(f"\n# corporate root from {name}: {_cn(c.subject)}\n".encode() + pem)
    data = b"".join(parts)
    digest = hashlib.sha256(data).hexdigest()[:12]
    path = confdir / f"upstream-ca-{digest}.pem"
    if not path.exists():
        atomic_write(path, data)
    for old in confdir.glob("upstream-ca-*.pem"):
        if old != path:
            try:
                old.unlink()
            except OSError:
                pass
    return path, len(corp)


# ── /trust outputs ───────────────────────────────────────────────────────
def write_trust_dir(trust_dir: Path, confdir: Path | None = None, ca_pem: bytes | None = None) -> dict:
    """Write egress-ca.pem, ca-bundle.pem (removes a stale
    `ready`; call mark_ready() once the proxy is listening)."""
    if not trust_dir.exists():
        trust_dir.mkdir(parents=True, exist_ok=True)
    if not os.access(trust_dir, os.W_OK):
        raise PermissionError(f"{trust_dir} is not writable by uid {os.getuid()} — "
                              "the trust volume must be owned by 1000:1000")
    ready = trust_dir / "ready"
    try:
        ready.unlink()
    except FileNotFoundError:
        pass
    if ca_pem is None:
        assert confdir is not None
        ca_pem = egress_ca_pem(confdir)
    if not load_certs(ca_pem):
        raise RuntimeError("egress CA PEM has no certificate")
    atomic_write(trust_dir / "egress-ca.pem", ca_pem)
    bundle = certifi_bundle().rstrip(b"\n") + b"\n\n# DataPlatform Appliance Egress CA (px-proxy)\n" + ca_pem
    atomic_write(trust_dir / "ca-bundle.pem", bundle)
    (trust_dir / "cacerts.p12").unlink(missing_ok=True)   # retired Java truststore
    return cert_info(load_certs(ca_pem)[0], "egress")


def mark_ready(trust_dir: Path, info: dict) -> None:
    atomic_write(trust_dir / "ready", (info["sha256"] + "\n").encode())


