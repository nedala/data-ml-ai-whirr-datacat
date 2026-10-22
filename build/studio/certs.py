"""Facade TLS. dev: an appliance root CA (cert data/tls/appliance-ca.pem, key
data/vault/ca-key.pem 0600) issues data/tls/server.crt|key for nginx.
preprod/prod: a corporate certificate is uploaded (validated) or TLS is
terminated upstream (AKS ingress / cert-manager)."""

from __future__ import annotations

import datetime as dt
import hashlib
import ipaddress
from pathlib import Path
from typing import Any

from cryptography import x509
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

import settings
from fsutil import write_file

SERVER_DAYS = 397       # Apple/Chrome cap trusted TLS leaf validity at 398 days
CA_DAYS = 3650
NGINX_UID = 1000


class CertError(ValueError):
    pass


def tls_dir() -> Path:
    return settings.cfg.data / "tls"


def paths() -> dict[str, Path]:
    return {"crt": tls_dir() / "server.crt", "key": tls_dir() / "server.key",
            "ca": tls_dir() / "appliance-ca.pem", "ca_key": settings.cfg.vault_dir / "ca-key.pem"}


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def _pem_key(key: Any) -> bytes:
    return key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                             serialization.NoEncryption())


def default_hostnames(extra_host: str = "") -> list[str]:
    names = ["localhost", "127.0.0.1", settings.cfg.facade_host]
    if extra_host and extra_host not in ("docker-desktop",):
        names.append(extra_host)
    return list(dict.fromkeys(n.strip().lower() for n in names if n and n.strip()))


def _ensure_ca() -> tuple[x509.Certificate, Any]:
    p = paths()
    if p["ca"].is_file() and p["ca_key"].is_file():
        cert = x509.load_pem_x509_certificate(p["ca"].read_bytes())
        key = serialization.load_pem_private_key(p["ca_key"].read_bytes(), None)
        if cert.not_valid_after_utc > _now() + dt.timedelta(days=30):
            return cert, key
    key = ec.generate_private_key(ec.SECP256R1())
    name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "DataPlatform"),
                      x509.NameAttribute(NameOID.ORGANIZATIONAL_UNIT_NAME, "Data & AI Appliance"),
                      x509.NameAttribute(NameOID.COMMON_NAME, f"DataPlatform Appliance Local CA {_now():%Y-%m-%d}")])
    ski = x509.SubjectKeyIdentifier.from_public_key(key.public_key())
    cert = (x509.CertificateBuilder().subject_name(name).issuer_name(name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_now() - dt.timedelta(minutes=5)).not_valid_after(_now() + dt.timedelta(days=CA_DAYS))
            .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
            .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                         content_commitment=False, key_encipherment=False, data_encipherment=False,
                                         key_agreement=False, encipher_only=False, decipher_only=False), critical=True)
            .add_extension(ski, critical=False)
            .sign(key, hashes.SHA256()))
    write_file(p["ca_key"], _pem_key(key), mode=0o600, uid=0, gid=0)
    write_file(p["ca"], cert.public_bytes(serialization.Encoding.PEM), mode=0o644)
    return cert, key


def _san(names: list[str]) -> x509.SubjectAlternativeName:
    entries: list[x509.GeneralName] = []
    for n in names:
        try:
            entries.append(x509.IPAddress(ipaddress.ip_address(n)))
        except ValueError:
            entries.append(x509.DNSName(n))
    return x509.SubjectAlternativeName(entries)


def issue_server_cert(hostnames: list[str]) -> dict[str, Any]:
    names = [h.strip().lower() for h in hostnames if h and h.strip()]
    if not names:
        raise CertError("At least one hostname is required")
    ca_cert, ca_key = _ensure_ca()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    dns = next((n for n in names if not _is_ip(n)), names[0])
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "DataPlatform"),
                                     x509.NameAttribute(NameOID.COMMON_NAME, dns)]))
            .issuer_name(ca_cert.subject).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(_now() - dt.timedelta(minutes=5)).not_valid_after(_now() + dt.timedelta(days=SERVER_DAYS))
            .add_extension(_san(names), critical=False)
            .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    chain = cert.public_bytes(serialization.Encoding.PEM) + ca_cert.public_bytes(serialization.Encoding.PEM)
    _install(chain, _pem_key(key))
    return describe()


def _is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def _install(chain_pem: bytes, key_pem: bytes) -> None:
    p = paths()
    write_file(p["key"], key_pem, mode=0o600, uid=NGINX_UID, gid=NGINX_UID)
    write_file(p["crt"], chain_pem, mode=0o644)


def ensure_dev_cert(host_name: str = "") -> bool:
    """On dev start-up: issue a facade cert if none exists."""
    if settings.cfg.appliance_env != "dev" or paths()["crt"].is_file():
        return False
    issue_server_cert(default_hostnames(host_name))
    return True


def _cert_info(cert: x509.Certificate) -> dict[str, Any]:
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        names = [str(v) for v in san.get_values_for_type(x509.DNSName)] + \
                [str(v) for v in san.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        names = []
    left = cert.not_valid_after_utc - _now()
    return {"subject": cert.subject.rfc4514_string(), "issuer": cert.issuer.rfc4514_string(), "sans": names,
            "not_before": cert.not_valid_before_utc.isoformat(), "not_after": cert.not_valid_after_utc.isoformat(),
            "days_left": left.days, "serial": format(cert.serial_number, "x"),
            "sha256": hashlib.sha256(cert.public_bytes(serialization.Encoding.DER)).hexdigest()}


def describe() -> dict[str, Any]:
    p = paths()
    out: dict[str, Any] = {"env": settings.cfg.appliance_env, "termination": settings.cfg.tls_termination,
                           "server": None, "ca": None, "issued_by_appliance_ca": False, "chain": []}
    if p["crt"].is_file():
        chain = x509.load_pem_x509_certificates(p["crt"].read_bytes())
        out["server"] = _cert_info(chain[0])
        out["chain"] = [_cert_info(c) for c in chain[1:]]
    if p["ca"].is_file():
        ca = x509.load_pem_x509_certificate(p["ca"].read_bytes())
        out["ca"] = _cert_info(ca)
        if out["server"]:
            leaf = x509.load_pem_x509_certificates(p["crt"].read_bytes())[0]
            out["issued_by_appliance_ca"] = _signed_by(leaf, ca)
    return out


def _signed_by(cert: x509.Certificate, issuer: x509.Certificate) -> bool:
    try:
        cert.verify_directly_issued_by(issuer)
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


def validate_upload(cert_pem: bytes, key_pem: bytes) -> list[str]:
    """Raise CertError for fatal problems; return warnings."""
    try:
        chain = x509.load_pem_x509_certificates(cert_pem)
    except ValueError as exc:
        raise CertError(f"Certificate is not valid PEM: {exc}") from exc
    try:
        key = serialization.load_pem_private_key(key_pem, None)
    except (ValueError, TypeError) as exc:
        raise CertError("Private key is not an unencrypted PEM key") from exc
    leaf = chain[0]
    pub = serialization.PublicFormat.SubjectPublicKeyInfo
    if key.public_key().public_bytes(serialization.Encoding.PEM, pub) != \
            leaf.public_key().public_bytes(serialization.Encoding.PEM, pub):
        raise CertError("The private key does not match the certificate")
    if leaf.not_valid_after_utc <= _now():
        raise CertError(f"Certificate expired on {leaf.not_valid_after_utc:%Y-%m-%d}")
    if leaf.not_valid_before_utc > _now():
        raise CertError("Certificate is not valid yet")
    for child, parent in zip(chain, chain[1:], strict=False):
        if not _signed_by(child, parent):
            raise CertError(f"Chain is broken: {child.subject.rfc4514_string()} is not signed by the next certificate")
    warnings = []
    info = _cert_info(leaf)
    if settings.cfg.facade_host.lower() not in [n.lower() for n in info["sans"]]:
        warnings.append(f"{settings.cfg.facade_host} is not in the certificate's SANs")
    if len(chain) == 1 and leaf.issuer != leaf.subject:
        warnings.append("No intermediate certificates included — browsers may not build the chain")
    if info["days_left"] < 30:
        warnings.append(f"Certificate expires in {info['days_left']} days")
    return warnings


def install_upload(cert_pem: bytes, key_pem: bytes) -> list[str]:
    warnings = validate_upload(cert_pem, key_pem)
    _install(cert_pem, key_pem)
    return warnings
