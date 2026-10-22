"""Generate a fake corporate (TLS-inspection-like) root + a server cert it signs."""

import datetime as dt
import sys
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID

out = Path(sys.argv[1])
out.mkdir(parents=True, exist_ok=True)
now = dt.datetime.now(dt.timezone.utc)


def key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


ca_key = key()
ca_name = x509.Name([x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Corporate Proxy Inc. (TEST FAKE)"),
                     x509.NameAttribute(NameOID.COMMON_NAME, "Fake Corporate Root CA (egtest)")])
ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
      .serial_number(x509.random_serial_number()).not_valid_before(now - dt.timedelta(days=1))
      .not_valid_after(now + dt.timedelta(days=30))
      .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
      .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True, content_commitment=False,
                                   key_encipherment=False, data_encipherment=False, key_agreement=False,
                                   encipher_only=False, decipher_only=False), critical=True)
      .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()), critical=False)
      .sign(ca_key, hashes.SHA256()))

srv_key = key()
names = ["pypi.org", "files.pythonhosted.org", "evil.example.com", "notallowed.example.net", "api.dataplatform.com"]
srv = (x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, names[0])]))
       .issuer_name(ca_name).public_key(srv_key.public_key()).serial_number(x509.random_serial_number())
       .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=30))
       .add_extension(x509.SubjectAlternativeName([x509.DNSName(n) for n in names]), critical=False)
       .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
       .add_extension(x509.ExtendedKeyUsage([x509.oid.ExtendedKeyUsageOID.SERVER_AUTH]), critical=False)
       .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(ca_key.public_key()), critical=False)
       .sign(ca_key, hashes.SHA256()))

(out / "fake-corporate-root.pem").write_bytes(ca.public_bytes(serialization.Encoding.PEM))
(out / "server.pem").write_bytes(srv.public_bytes(serialization.Encoding.PEM))
(out / "server.key").write_bytes(srv_key.private_bytes(serialization.Encoding.PEM,
                                                       serialization.PrivateFormat.TraditionalOpenSSL,
                                                       serialization.NoEncryption()))
print("certs written to", out)
